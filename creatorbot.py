import os
import re
import sqlite3
import time
import datetime as dt

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
YT_KEY = os.environ["YOUTUBE_API_KEY"]

PER_PAGE = 10          # videos per page (Discord max is 10 embeds per message)
MAX_SHORT_SECONDS = 180  # YouTube Shorts can be up to 3 minutes
DEFAULT_HOURS = 24

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------- helpers ----------
def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def parse_duration(iso: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def fmt_duration(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_age(published: dt.datetime) -> str:
    hrs = (dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600
    if hrs < 1:
        return f"{int(hrs * 60)}m ago"
    if hrs < 48:
        return f"{int(hrs)}h ago"
    return f"{int(hrs // 24)}d ago"


async def fetch_trending(query: str, hours: int):
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "videoDuration": "short",
                "order": "viewCount",
                "publishedAfter": after,
                "maxResults": 50,
                "key": YT_KEY,
            },
        ) as r:
            data = await r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "YouTube API error"))
        ids = [i["id"]["videoId"] for i in data.get("items", [])]
        if not ids:
            return []
        async with s.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(ids),
                "key": YT_KEY,
            },
        ) as r:
            vdata = await r.json()

    videos = []
    for v in vdata.get("items", []):
        dur = parse_duration(v["contentDetails"]["duration"])
        if dur == 0 or dur > MAX_SHORT_SECONDS:
            continue
        published = dt.datetime.fromisoformat(
            v["snippet"]["publishedAt"].replace("Z", "+00:00")
        )
        views = int(v["statistics"].get("viewCount", 0))
        age_hrs = max((dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600, 1)
        likes = v["statistics"].get("likeCount")
        videos.append(
            {
                "id": v["id"],
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "thumb": v["snippet"]["thumbnails"].get("medium", {}).get("url"),
                "views": views,
                "likes": fmt_num(int(likes)) if likes is not None else "Hidden",
                "comments": v["statistics"].get("commentCount", "0"),
                "published": published,
                "duration": dur,
                "vph": views / age_hrs,
            }
        )
    videos.sort(key=lambda x: x["vph"], reverse=True)  # fastest-growing first
    return videos


# ---------- pagination ----------
class Pager(discord.ui.View):
    def __init__(self, videos, query, hours):
        super().__init__(timeout=900)
        self.videos, self.query, self.hours = videos, query, hours
        self.page = 0
        self.pages = max(1, -(-len(videos) // PER_PAGE))
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def header(self):
        return f"🎬 **Top {len(self.videos)} Trending Shorts: {self.query}** (Page {self.page + 1}/{self.pages})"

    def embeds(self):
        start = self.page * PER_PAGE
        out = []
        for i, v in enumerate(self.videos[start : start + PER_PAGE], start=start + 1):
            e = discord.Embed(
                title=f"#{i} - {v['title']}"[:256],
                url=f"https://www.youtube.com/shorts/{v['id']}",
                description=(
                    f"{v['channel']}\n\n"
                    f"👁️ **Views**\n{fmt_num(v['views'])}\n"
                    f"👍 **Likes**\n{v['likes']}\n"
                    f"🕒 **Posted**\n{fmt_age(v['published'])}\n"
                    f"💬 **Comments**\n{v['comments']}\n"
                    f"⏱️ **Duration**\n{fmt_duration(v['duration'])}\n"
                    f"📈 **VPH**\n{fmt_num(int(v['vph']))}"
                ),
                color=0x2B7FDB,
            )
            if v["thumb"]:
                e.set_thumbnail(url=v["thumb"])
            e.set_footer(text=f'"{self.query}" • last {self.hours} hours • {i}/{len(self.videos)}')
            out.append(e)
        return out

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(content=self.header(), embeds=self.embeds(), view=self)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(content=self.header(), embeds=self.embeds(), view=self)


# ---------- commands ----------
@bot.command(name="trending")
async def trending(ctx: commands.Context, *, query: str = "shorts"):
    """!trending <topic>   or   !trending <topic> 48h   (custom time window)"""
    hours = DEFAULT_HOURS
    m = re.search(r"\s+(\d{1,3})h$", query)
    if m:
        hours = min(int(m.group(1)), 168)
        query = query[: m.start()].strip() or "shorts"

    async with ctx.typing():
        try:
            videos = await fetch_trending(query, hours)
        except Exception as e:
            return await ctx.reply(f"❌ Could not fetch videos: {e}")

    if not videos:
        return await ctx.reply(f'No Shorts found for "{query}" in the last {hours} hours.')

    view = Pager(videos, query, hours)
    await ctx.reply(view.header(), embeds=view.embeds(), view=view)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.reply(
        "**Commands**\n"
        "`!trending <topic>` – top Shorts from the last 24 hours\n"
        "`!trending <topic> 48h` – choose your own time window (max 168h)\n"
        "Example: `!trending steal an egg`\n"
        "`!messages` – see your progress toward unlocking images & GIFs\n"
        "`!leaderboard` (or `!lb 7d`) – top student channels by view gains\n"
        "`!lb views` / `!lb subs` – rank by total views or subscribers\n"
        "`!submit @yourhandle` – add your own YouTube channel to the leaderboard\n"
        "`!addchannel @handle` / `!removechannel @handle` – manage the leaderboard (Manage Server only)"
    )


# ---------- message counter -> unlock image/GIF role ----------
REWARD_ROLE_NAME = os.environ.get("REWARD_ROLE_NAME", "Image Perms")  # role that allows images/GIFs
MESSAGE_GOAL = int(os.environ.get("MESSAGE_GOAL", "50"))              # messages needed
COUNT_COOLDOWN = int(os.environ.get("COUNT_COOLDOWN", "10"))          # seconds between counted messages (anti-spam)

db = sqlite3.connect(os.environ.get("DB_PATH", "counts.db"))
db.execute(
    "CREATE TABLE IF NOT EXISTS counts ("
    "guild_id INTEGER, user_id INTEGER, count INTEGER, "
    "PRIMARY KEY (guild_id, user_id))"
)
db.commit()
last_counted = {}


def add_message(guild_id: int, user_id: int) -> int:
    db.execute(
        "INSERT INTO counts (guild_id, user_id, count) VALUES (?, ?, 1) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + 1",
        (guild_id, user_id),
    )
    db.commit()
    return get_count(guild_id, user_id)


def get_count(guild_id: int, user_id: int) -> int:
    row = db.execute(
        "SELECT count FROM counts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return row[0] if row else 0


@bot.listen("on_message")
async def count_messages(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if message.content.startswith("!"):          # don't count bot commands
        return
    if len(message.content.strip()) < 3:         # ignore tiny / empty messages
        return

    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - last_counted.get(key, 0) < COUNT_COOLDOWN:  # stops spam farming
        return
    last_counted[key] = now

    total = add_message(message.guild.id, message.author.id)

    role = discord.utils.get(message.guild.roles, name=REWARD_ROLE_NAME)
    if role is None or role in message.author.roles:
        return
    if total >= MESSAGE_GOAL:
        try:
            await message.author.add_roles(role, reason=f"Reached {MESSAGE_GOAL} messages")
            await message.channel.send(
                f"🎉 {message.author.mention} reached **{MESSAGE_GOAL} messages** and unlocked "
                f"**{role.name}**. You can now send images & GIFs!"
            )
        except discord.Forbidden:
            print("Missing permission: give the bot 'Manage Roles' and put its role above the reward role.")


@bot.command(name="messages")
async def messages_cmd(ctx: commands.Context):
    total = get_count(ctx.guild.id, ctx.author.id)
    if total >= MESSAGE_GOAL:
        await ctx.reply(f"✅ You've sent {total} messages. Image perms unlocked!")
    else:
        await ctx.reply(f"📊 {total}/{MESSAGE_GOAL} messages. {MESSAGE_GOAL - total} more to unlock images & GIFs.")


# ---------- student channel leaderboard ----------
DB_PATH = os.environ.get("LEADERBOARD_DB_PATH", os.environ.get("DB_PATH", "leaderboard.db"))
LB_PER_PAGE = 8
REFRESH_MINUTES = 60          # how often channel stats are re-checked
MAX_ADD_AT_ONCE = 25
KEEP_DAYS = 35                # old snapshots are deleted after this
MAX_PER_STUDENT = int(os.environ.get("MAX_PER_STUDENT", "1"))  # channels each student may submit
SUBMIT_COOLDOWN = int(os.environ.get("SUBMIT_COOLDOWN", "30"))  # seconds between !submit uses

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
    def __init__(self, rows, hours, history_note, updated_ago, mode="gain"):
        super().__init__(timeout=900)
        self.mode = mode
        self.rows, self.hours = rows, hours
        self.history_note, self.updated_ago = history_note, updated_ago
        self.page = 0
        self.pages = max(1, -(-len(rows) // LB_PER_PAGE))
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        start = self.page * LB_PER_PAGE
        chunk = self.rows[start : start + LB_PER_PAGE]
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
        if self.mode == "views":
            title, intro = "🎯 Top Channels - Total Views", "Ranked by total channel views"
        elif self.mode == "subs":
            title, intro = "👥 Top Channels - Subscribers", "Ranked by subscriber count"
        else:
            title = f"⏰ Top Channels - {period_label(self.hours)}"
            intro = f"View count gains for the last {period_label(self.hours).lower()}"
        e = discord.Embed(
            title=title,
            description=intro + "\n\n" + "\n".join(lines),
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
        try:  # remembers which student submitted a channel (older databases get the new column)
            self.db.execute("ALTER TABLE channels ADD COLUMN added_by INTEGER")
        except sqlite3.OperationalError:
            pass
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
        """!leaderboard | !leaderboard 7d | !leaderboard views | !leaderboard subs"""
        p = period.lower()
        mode = "gain"
        if p in ("views", "view", "total"):
            mode = "views"
        elif p in ("subs", "sub", "subscribers"):
            mode = "subs"
        hours = period_to_hours(period) if mode == "gain" else 24
        rows, note, updated = self.build_rows(ctx.guild.id, hours)
        if not rows:
            return await ctx.reply("No channels yet. Add some with `!addchannel @handle`.")
        if mode == "views":
            rows.sort(key=lambda r: r["views"], reverse=True)
            note = None
        elif mode == "subs":
            rows.sort(key=lambda r: r["subs"] if r["subs"] is not None else -1, reverse=True)
            note = None
        view = LeaderboardView(rows, hours, note, updated, mode)
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
        await ctx.reply(msg or "Nothing to add.", allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="submit", aliases=["join", "mychannel"])
    @commands.guild_only()
    @commands.cooldown(1, SUBMIT_COOLDOWN, commands.BucketType.user)
    async def submit(self, ctx: commands.Context, *, channel: str):
        """Students add their own channel: !submit @handle  (cannot edit or remove anything)"""
        none = discord.AllowedMentions.none()
        link = channel.strip().split()[0]  # only one channel per command
        async with ctx.typing():
            try:
                item = await self.resolve(link)
            except Exception as e:
                return await ctx.reply(f"❌ YouTube error: {e}", allowed_mentions=none)
        if not item:
            return await ctx.reply(
                "⚠️ I couldn't find that channel. Use your handle or link, like `!submit @yourhandle`.",
                allowed_mentions=none,
            )

        title = item["snippet"]["title"]
        existing = self.db.execute(
            "SELECT added_by FROM channels WHERE guild_id = ? AND channel_id = ?",
            (ctx.guild.id, item["id"]),
        ).fetchone()
        if existing:
            if existing[0] == ctx.author.id:
                return await ctx.reply(f"✅ **{title}** is already on the leaderboard.", allowed_mentions=none)
            return await ctx.reply(
                f"**{title}** is already on the leaderboard. If it's yours and someone else added it, ask a staff member.",
                allowed_mentions=none,
            )

        mine = self.db.execute(
            "SELECT title FROM channels WHERE guild_id = ? AND added_by = ?",
            (ctx.guild.id, ctx.author.id),
        ).fetchall()
        if len(mine) >= MAX_PER_STUDENT:
            return await ctx.reply(
                f"You already submitted **{mine[0][0]}**. To change it, ask a staff member.",
                allowed_mentions=none,
            )

        self.db.execute(
            "INSERT INTO channels (guild_id, channel_id, title, handle, thumb, added_by) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id,
                item["id"],
                title,
                item["snippet"].get("customUrl"),
                self.thumb_of(item),
                ctx.author.id,
            ),
        )
        self.save_snapshot(item, int(time.time()))
        self.db.commit()
        await ctx.reply(
            f"🎉 **{title}** was added to the leaderboard! Check it with `!leaderboard`.",
            allowed_mentions=none,
        )

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
            await ctx.reply("Please include a channel, e.g. `!submit @yourhandle`.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.reply(f"⏳ Please wait {int(error.retry_after) + 1}s before using that again.")
        else:
            raise error


# ---------- tiny web server (lets free hosts like Render keep the bot alive) ----------
@bot.event
async def setup_hook():
    await bot.add_cog(Leaderboard(bot))  # student channel leaderboard

    async def ok(_):
        return web.Response(text="bot is running")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


bot.run(DISCORD_TOKEN)
