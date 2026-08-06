"""
Polls Twitch Helix "Get Clips" for a given game_id, keeps only clips above
a view-count threshold, and stores new matches in a local SQLite db.

Helix's Get Clips does NOT support filtering by view count server-side -
it only accepts a date window, ids, and pagination params. So we filter
client-side. Twitch returns clips in descending view-count order, so we
stop paging as soon as a page's last clip drops below min_views - this
avoids paging through thousands of low-view clips we'd throw away anyway,
and sidesteps Twitch's ~1000-result pagination ceiling for high-volume
windows (see dev.twitch.tv/docs/api/guide on pagination limits).

Re-running this on a schedule is the intended usage (small playerbase
games get clipped slowly, so polling daily/hourly is fine). Dedup is
handled by the clip_id primary key; re-runs upsert view_count/title so
numbers stay current, while leaving 'status' and 'discovered_at' alone
since those hold manual workflow state set outside this script.
"""

import os
import sys
import time
import sqlite3
import argparse
from datetime import datetime, timedelta, timezone

import requests
from auth import get_auth_headers

CLIPS_URL = "https://api.twitch.tv/helix/clips"
DB_PATH = os.path.join(os.path.dirname(__file__), "clips.db")

# Helix caps "first" at 100 per page for Get Clips.
PAGE_SIZE = 100
# Sanity ceiling so a misbehaving cursor can't spin this forever.
MAX_PAGES = 20


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clips (
            clip_id TEXT PRIMARY KEY,
            broadcaster_name TEXT,
            title TEXT,
            view_count INTEGER,
            url TEXT,
            thumbnail_url TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'pending',
            discovered_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _get_with_backoff(headers, params):
    """
    Wraps the GET call with 429 handling. Twitch reports a token-bucket
    rate limit via Ratelimit-Reset (unix timestamp) - honor it instead of
    guessing a sleep duration.
    """
    for attempt in range(5):
        response = requests.get(CLIPS_URL, headers=headers, params=params, timeout=10)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        reset_at = response.headers.get("Ratelimit-Reset")
        wait = max(float(reset_at) - time.time(), 1) if reset_at else 2 ** attempt
        print(f"Rate limited, waiting {wait:.1f}s...", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError("Rate limited repeatedly; giving up.")


def fetch_clips(game_id: str, started_at: str, ended_at: str, min_views: int):
    """
    Pages through clips for game_id in [started_at, ended_at], stopping
    early once a page's lowest view_count drops below min_views (clips
    arrive sorted descending by views, so everything after that point
    would be filtered out anyway).
    """
    headers = get_auth_headers()
    all_clips = []
    cursor = None

    for _ in range(MAX_PAGES):
        params = {
            "game_id": game_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": PAGE_SIZE,
        }
        if cursor:
            params["after"] = cursor

        response = _get_with_backoff(headers, params)
        payload = response.json()

        page = payload.get("data", [])
        all_clips.extend(page)

        if page and page[-1].get("view_count", 0) < min_views:
            break

        # Docs: end of results is an empty pagination object (cursor absent).
        cursor = payload.get("pagination", {}).get("cursor")
        if not cursor or not page:
            break

    return all_clips


def upsert_clips(conn, clips, min_views):
    """
    Applies the client-side view filter, then upserts matches. On conflict,
    refreshes view_count/title (the numbers that change over time) but
    leaves status/discovered_at untouched so manual workflow state survives
    re-runs. Returns (matched_count, inserted_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    matched = [c for c in clips if c.get("view_count", 0) >= min_views]

    matched_ids = [c["id"] for c in matched]
    existing_ids = set()
    if matched_ids:
        placeholders = ",".join("?" * len(matched_ids))
        rows = conn.execute(
            f"SELECT clip_id FROM clips WHERE clip_id IN ({placeholders})", matched_ids
        ).fetchall()
        existing_ids = {row[0] for row in rows}

    for clip in matched:
        conn.execute(
            """
            INSERT INTO clips
                (clip_id, broadcaster_name, title, view_count, url,
                 thumbnail_url, created_at, status, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                view_count = excluded.view_count,
                title = excluded.title
            """,
            (
                clip["id"],
                clip.get("broadcaster_name"),
                clip.get("title"),
                clip.get("view_count", 0),
                clip.get("url"),
                clip.get("thumbnail_url"),
                clip.get("created_at"),
                now,
            ),
        )

    conn.commit()
    inserted = len(matched_ids) - len(existing_ids & set(matched_ids))
    return len(matched), inserted


def poll(game_id: str, min_views: int, lookback_days: int):
    ended_at = datetime.now(timezone.utc)
    started_at = ended_at - timedelta(days=lookback_days)

    # Helix wants RFC3339 timestamps.
    started_at_str = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    ended_at_str = ended_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    clips = fetch_clips(game_id, started_at_str, ended_at_str, min_views)

    conn = _init_db()
    try:
        matched, inserted = upsert_clips(conn, clips, min_views)
    finally:
        conn.close()

    print(f"Fetched:  {len(clips)} clips (last {lookback_days} day(s))")
    print(f"Matched:  {matched} clips with view_count >= {min_views}")
    print(f"Inserted: {inserted} new (already known/updated: {matched - inserted})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll Twitch clips for a game.")
    parser.add_argument("game_id", help="Twitch game_id (see lookup_game_id.py)")
    parser.add_argument(
        "--min-views", type=int, default=5, help="Minimum view count to keep (default: 5)"
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="How many days back to search for clips (default: 7)",
    )
    args = parser.parse_args()

    if args.lookback_days < 1:
        parser.error("--lookback-days must be at least 1")
    if args.min_views < 0:
        parser.error("--min-views cannot be negative")

    poll(args.game_id, args.min_views, args.lookback_days)
