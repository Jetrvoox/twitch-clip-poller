"""
Discord approval-gate bot. Posts pending clips as embeds (thumbnail,
broadcaster, views, link); reacting with the checkmark/cross approves or
rejects, writing straight back to clips.db's status column - the same
seam review.py uses, so nothing downstream needs to know which UI made
the call.

Runs as its own long-lived process alongside poller.py (which keeps
finding new clips on its own schedule) - this just posts whatever's
pending and hasn't been posted yet, then listens for reactions until
you stop it.
"""

import os
import asyncio
import sqlite3
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "clips.db")
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

APPROVE_EMOJI = "✅"  # checkmark
REJECT_EMOJI = "❌"   # cross
POLL_INTERVAL_SECONDS = 300
# Caps how many clips get posted per loop cycle, and paces sends within a
# cycle - posting a big backlog all at once floods the channel and reliably
# trips Discord's per-route rate limit on reactions.
MAX_POSTS_PER_CYCLE = 5
SECONDS_BETWEEN_POSTS = 2

intents = discord.Intents.default()
intents.reactions = True
client = discord.Client(intents=intents)


def _connect():
    # timeout gives SQLite a grace period if poller.py/review.py hold the
    # file lock at the same instant this long-lived process wakes up.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # discord_message_id is a migration on top of poller.py's original
    # schema - add it defensively since existing clips.db files won't
    # have it yet, and repeat calls should no-op once it does.
    try:
        conn.execute("ALTER TABLE clips ADD COLUMN discord_message_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def _unposted_pending_clips(conn):
    return conn.execute(
        """
        SELECT clip_id, broadcaster_name, title, view_count, url, thumbnail_url
        FROM clips
        WHERE status = 'pending' AND discord_message_id IS NULL
        ORDER BY view_count DESC
        """
    ).fetchall()


def _record_message_id(conn, clip_id, message_id):
    conn.execute(
        "UPDATE clips SET discord_message_id = ? WHERE clip_id = ?",
        (str(message_id), clip_id),
    )
    conn.commit()


def _set_status_by_message(conn, message_id, status):
    cursor = conn.execute(
        "UPDATE clips SET status = ? WHERE discord_message_id = ?",
        (status, str(message_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


def _build_embed(clip_id, broadcaster_name, title, view_count, url, thumbnail_url):
    embed = discord.Embed(title=title or "(untitled clip)", url=url, color=0x9146FF)
    embed.set_author(name=broadcaster_name or "unknown")
    embed.add_field(name="Views", value=str(view_count), inline=True)
    if thumbnail_url:
        embed.set_image(url=thumbnail_url)
    embed.set_footer(
        text=f"clip_id: {clip_id}  |  react {APPROVE_EMOJI} approve / {REJECT_EMOJI} reject"
    )
    return embed


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def post_pending_clips():
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(
            f"Channel {CHANNEL_ID} not found/visible to bot - "
            "check DISCORD_CHANNEL_ID and that the bot was actually invited to that server."
        )
        return

    conn = _connect()
    try:
        rows = _unposted_pending_clips(conn)[:MAX_POSTS_PER_CYCLE]
        for clip_id, broadcaster_name, title, view_count, url, thumbnail_url in rows:
            embed = _build_embed(clip_id, broadcaster_name, title, view_count, url, thumbnail_url)
            message = await channel.send(embed=embed)
            await message.add_reaction(APPROVE_EMOJI)
            await message.add_reaction(REJECT_EMOJI)
            _record_message_id(conn, clip_id, message.id)
            await asyncio.sleep(SECONDS_BETWEEN_POSTS)
    finally:
        conn.close()

    if rows:
        print(f"Posted {len(rows)} new clip(s) for review.")


@post_pending_clips.before_loop
async def before_post_pending_clips():
    await client.wait_until_ready()


@client.event
async def on_ready():
    print(f"Logged in as {client.user} - watching channel {CHANNEL_ID}")
    if not post_pending_clips.is_running():
        post_pending_clips.start()


@client.event
async def on_raw_reaction_add(payload):
    if client.user is not None and payload.user_id == client.user.id:
        return  # ignore the bot's own reactions (added when posting)
    if payload.channel_id != CHANNEL_ID:
        return

    emoji = str(payload.emoji)
    if emoji == APPROVE_EMOJI:
        status = "approved"
    elif emoji == REJECT_EMOJI:
        status = "rejected"
    else:
        return

    conn = _connect()
    try:
        updated = _set_status_by_message(conn, payload.message_id, status)
    finally:
        conn.close()

    if updated:
        channel = client.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        await message.reply(f"Marked **{status}**.", mention_author=False)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN. Check your .env file.")
    if not CHANNEL_ID:
        raise SystemExit("Missing DISCORD_CHANNEL_ID. Check your .env file.")
    client.run(TOKEN)
