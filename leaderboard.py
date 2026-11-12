"""Student channel leaderboard: tracks YouTube channels you add and ranks them by view gains."""
import os
import re
import sqlite3
import time

import aiohttp
import discord
from discord.ext import commands, tasks

YT_KEY = os.environ["YOUTUBE_API_KEY"]
DB_PATH = os.environ.get("LEADERBOARD_DB_PATH", os.environ.get("DB_PATH", "leaderboard.db"))
PER_PAGE = 8
REFRESH_MINUTES = 60          # how often channel stats are re-checked
MAX_ADD_AT_ONCE = 25
KEEP_DAYS = 35                # old snapshots are deleted after this

ID_RE = re.compile(r"(UC[A-Za-z0-9_-]{22})")
HANDLE_RE = re.compile(r"@([A-Za-z0-9._-]+)")
PERIOD_RE = re.compile(r"^(\d{1,3})([hd])$", re.I)


def period_to_hours(text: str) -> int:
    m = PERIOD_RE.match(text.strip())
    if not m:
        return 24
    n, unit = int(m.group(1)), m.group(2).lower()
    hours = n if unit == "h" else n * 24
    return max(1, min(hours, 30 * 24))


def period_label(hours: int) -> str:
    if hours % 24 == 0 and hours != 24:
        return f"{hours // 24} Days"
    return f"{hours} Hours"


def ago(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class LeaderboardView(discord.ui.View):
    def __init__(self, rows, hours, history_note, updated_ago):
        super().__init__(timeout=900)
        self.rows, self.hours = rows, hours
        self.history_note, self.updated_ago = history_note, updated_ago
        self.page = 0
        self.pages = max(1, -(-len(rows) // PER_PAGE))
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        start = self.page * PER_PAGE
        chunk = self.rows[start : start + PER_PAGE]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, r in enumerate(chunk, start=start + 1):
            rank = medals.get(i, f"{i}.")
            name = r["title"].replace("[", "(").replace("]", ")")
            subs = f"{r['subs']:,} subs" if r["subs"] is not None else "Hidden subs"
            lines.append(
                f"{rank} [{name}]({r['url']})\n"
                f"📊 {r['gain']:+,} views\n"
                f"🎯 {r['views']:,} total\n"
                f"👥 {subs}"
            )
        e = discord.Embed(
            title=f"⏰ Top Channels - {period_label(self.hours)}",
            description=f"View count gains for the last {period_label(self.hours).lower()}\n\n"
            + "\n".join(lines),
            color=0x2B7FDB,
        )
        if chunk and chunk[0]["thumb"]:
            e.set_thumbnail(url=chunk[0]["thumb"])
        footer = f"Page {self.page + 1}/{self.pages} • Total: {len(self.rows)} channels • Stats updated {self.updated_ago}"
        if self.history_note:
            footer += f" • {self.history_note}"
        e.set_footer(text=footer)
        return e

    @discord.ui.button(label="Previous", emoji="⬅️", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS channels ("
            "guild_id INTEGER, channel_id TEXT, title TEXT, handle TEXT, thumb TEXT, "
            "PRIMARY KEY (guild_id, channel_id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "channel_id TEXT, ts INTEGER, views INTEGER, subs INTEGER)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_snap ON snapshots (channel_id, ts)")
        self.db.commit()

    async def cog_load(self):
        self.refresh_loop.start()

    async def cog_unload(self):
        self.refresh_loop.cancel()

    # ---------- YouTube helpers ----------
    async def fetch_channels(self, **params):
        params.update({"part": "snippet,statistics", "key": YT_KEY})
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.googleapis.com/youtube/v3/channels", params=params) as r:
                data = await r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "YouTube API error"))
        return data.get("items", [])

    async def resolve(self, text: str):
        m = ID_RE.search(text)
        if m:
            items = await self.fetch_channels(id=m.group(1))
        else:
            h = HANDLE_RE.search(text)
            handle = h.group(1) if h else text.strip().lstrip("@")
            items = await self.fetch_channels(forHandle="@" + handle)
        return items[0] if items else None

    def save_snapshot(self, item, now: int):
        st = item["statistics"]
        views = int(st.get("viewCount", 0))
        subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
        self.db.execute(
            "INSERT INTO snapshots (channel_id, ts, views, subs) VALUES (?, ?, ?, ?)",
            (item["id"], now, views, subs),
        )

    @staticmethod
    def thumb_of(item):
        thumbs = item["snippet"].get("thumbnails", {})
        return (thumbs.get("medium") or thumbs.get("default") or {}).get("url")

    # ---------- background refresh ----------
    @tasks.loop(minutes=REFRESH_MINUTES)
    async def refresh_loop(self):
        try:
            await self.refresh_all()
        except Exception as e:  # never let the loop die
            print(f"Leaderboard refresh failed: {e}")

    @refresh_loop.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()

    async def refresh_all(self):
        ids = [r[0] for r in self.db.execute("SELECT DISTINCT channel_id FROM channels")]
        now = int(time.time())
        for i in range(0, len(ids), 50):  # API allows 50 channels per call
            for item in await self.fetch_channels(id=",".join(ids[i : i + 50])):
                self.save_snapshot(item, now)
                self.db.execute(
                    "UPDATE channels SET title = ?, thumb = ? WHERE channel_id = ?",
                    (item["snippet"]["title"], self.thumb_of(item), item["id"]),
                )
        self.db.execute("DELETE FROM snapshots WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        self.db.commit()

    # ---------- leaderboard data ----------
    def build_rows(self, guild_id: int, hours: int):
        now = int(time.time())
        cutoff = now - hours * 3600
        rows, newest, shortest_history = [], 0, None
        for cid, title, handle, thumb in self.db.execute(
            "SELECT channel_id, title, handle, thumb FROM channels WHERE guild_id = ?", (guild_id,)
        ).fetchall():
            latest = self.db.execute(
                "SELECT ts, views, subs FROM snapshots WHERE channel_id = ? ORDER BY ts DESC LIMIT 1",
                (cid,),
            ).fetchone()
            if not latest:
                continue
            base = self.db.execute(
                "SELECT ts, views FROM snapshots WHERE channel_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (cid, cutoff),
            ).fetchone()
            if base is None:  # not enough history yet -> use the oldest we have
                base = self.db.execute(
                    "SELECT ts, views FROM snapshots WHERE channel_id = ? ORDER BY ts ASC LIMIT 1",
                    (cid,),
                ).fetchone()
            history = latest[0] - base[0]
            shortest_history = history if shortest_history is None else min(shortest_history, history)
            newest = max(newest, latest[0])
            rows.append(
                {
                    "title": title or cid,
                    "url": f"https://youtube.com/{handle}" if handle and handle.startswith("@") else f"https://youtube.com/channel/{cid}",
                    "thumb": thumb,
                    "views": latest[1],
                    "subs": latest[2],
                    "gain": latest[1] - base[1],
                }
            )
        rows.sort(key=lambda r: r["gain"], reverse=True)
        note = None
        if shortest_history is not None and shortest_history < hours * 3600 * 0.9:
            note = f"tracking for {max(1, shortest_history // 3600)}h so far"
        updated = ago(now - newest) if newest else "never"
        return rows, note, updated

    # ---------- commands ----------
    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, period: str = "24h"):
        """!leaderboard   or   !leaderboard 7d"""
        hours = period_to_hours(period)
        rows, note, updated = self.build_rows(ctx.guild.id, hours)
        if not rows:
            return await ctx.reply("No channels yet. Add some with `!addchannel @handle`.")
        view = LeaderboardView(rows, hours, note, updated)
        await ctx.reply(embed=view.embed(), view=view)

    @commands.command(name="addchannel", aliases=["add"])
    @commands.has_permissions(manage_guild=True)
    async def addchannel(self, ctx: commands.Context, *, channels: str):
        """!addchannel @handle https://youtube.com/@other ..."""
        targets = [t for t in re.split(r"[\s,]+", channels.strip()) if t][:MAX_ADD_AT_ONCE]
        added, failed = [], []
        now = int(time.time())
        async with ctx.typing():
            for t in targets:
                try:
                    item = await self.resolve(t)
                except Exception as e:
                    return await ctx.reply(f"❌ YouTube error: {e}")
                if not item:
                    failed.append(t)
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO channels (guild_id, channel_id, title, handle, thumb) VALUES (?, ?, ?, ?, ?)",
                    (
                        ctx.guild.id,
                        item["id"],
                        item["snippet"]["title"],
                        item["snippet"].get("customUrl"),
                        self.thumb_of(item),
                    ),
                )
                self.save_snapshot(item, now)
                added.append(item["snippet"]["title"])
            self.db.commit()
        msg = ""
        if added:
            msg += "✅ Added: " + ", ".join(added) + "\n"
        if failed:
            msg += "⚠️ Not found: " + ", ".join(failed)
        await ctx.reply(msg or "Nothing to add.")

    @commands.command(name="removechannel", aliases=["remove"])
    @commands.has_permissions(manage_guild=True)
    async def removechannel(self, ctx: commands.Context, *, channel: str):
        """!removechannel @handle"""
        m = ID_RE.search(channel)
        if m:
            cur = self.db.execute(
                "DELETE FROM channels WHERE guild_id = ? AND channel_id = ?", (ctx.guild.id, m.group(1))
            )
        else:
            h = HANDLE_RE.search(channel)
            handle = "@" + (h.group(1) if h else channel.strip().lstrip("@"))
            cur = self.db.execute(
                "DELETE FROM channels WHERE guild_id = ? AND LOWER(handle) = LOWER(?)",
                (ctx.guild.id, handle),
            )
        self.db.commit()
        await ctx.reply("🗑️ Removed." if cur.rowcount else "Channel not found in the leaderboard.")

    @commands.command(name="channels")
    async def channels_cmd(self, ctx: commands.Context):
        n = self.db.execute("SELECT COUNT(*) FROM channels WHERE guild_id = ?", (ctx.guild.id,)).fetchone()[0]
        await ctx.reply(f"📋 {n} channels are on the leaderboard.")

    async def cog_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You need the **Manage Server** permission to do that.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Please add a channel, e.g. `!addchannel @channelhandle`.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
