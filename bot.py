import os
import re
import datetime as dt

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

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
        "Example: `!trending steal an egg`"
    )


# ---------- tiny web server (lets free hosts like Render keep the bot alive) ----------
@bot.event
async def setup_hook():
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
