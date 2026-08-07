"""
Takes clips with status='approved' and produces a final vertical (9:16)
video with a "clip via {broadcaster_name}" text overlay burned in, using
local ffmpeg. Downloads the result into processed/ and records the local
path in a new final_path column - the last stop before this project's
(not-yet-built) posting step.

This used to go through paid cloud APIs (Reap, then Shotstack). Reap was
dropped because its Auto Reframe API requires 2min-3hr inputs and Twitch
clips are 5-60s. Shotstack worked but costs money past its free/
watermarked sandbox tier - for a pipeline that only ever processes
whatever a human approves (low, bursty volume), local ffmpeg does the
same two jobs (crop to vertical, burn in text) for free, with no account
or API key at all.

UNVERIFIED ASSUMPTION (flagged for review): Helix's Get Clips response
has no direct video-file field, and the old "strip -preview-WxH.jpg off
thumbnail_url" trick stopped working for clips created after ~Sept 2024
(Twitch locked it down - confirmed via Twitch's own dev forum). This
script instead uses the same undocumented Twitch GraphQL endpoint that
twitch-dl/TwitchDownloader and other clip-downloader tools use
(gql.twitch.tv, persisted query VideoAccessToken_Clip, the well-known
public web Client-Id) to get a signed, temporary source URL. This is
still an unofficial/unsupported endpoint Twitch could change at any
time - verify it still works before relying on it for real.
"""

import os
import sys
import shutil
import sqlite3
import argparse
import subprocess
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "clips.db")
PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "processed")

# Ships on every Windows install - this project only targets Windows
# (see run_poller.bat/bot.py's Windows-specific assumptions elsewhere).
FONT_PATH = r"C:\Windows\Fonts\arialbd.ttf"

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

# Twitch's undocumented GQL endpoint, used the same way public clip-downloader
# tools use it - NOT the app Client-Id/Secret from auth.py, this is a
# different, unofficial, public web client id.
#
# Sends the full query text rather than a persisted-query hash: a
# community-sourced hash for this operation returned PERSISTED_QUERY_NOT_FOUND
# when tested against the live endpoint (Twitch's persisted-query registry
# doesn't match what's floating around in older tool source). The full query
# is confirmed working as of this writing - still unofficial, so re-verify if
# this ever starts failing.
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL_CLIP_QUERY = """
query VideoAccessToken_Clip($slug: ID!) {
  clip(slug: $slug) {
    playbackAccessToken(params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}) {
      signature
      value
    }
    videoQualities {
      frameRate
      quality
      sourceURL
    }
  }
}
"""


def _connect():
    if not os.path.exists(DB_PATH):
        raise SystemExit("No clips.db found - run poller.py at least once first.")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # final_path/final_error are migrations on top of the original schema -
    # add them defensively, same pattern bot.py uses for discord_message_id.
    for column in ("final_path TEXT", "final_error TEXT"):
        try:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {column}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def _approved_unprocessed_clips(conn):
    # final_error IS NULL: a clip that failed once (e.g. deleted upstream,
    # GQL couldn't resolve it) stays skipped instead of being retried and
    # burning effort on every single run - see run()'s docstring note.
    return conn.execute(
        """
        SELECT clip_id, broadcaster_name
        FROM clips
        WHERE status = 'approved' AND final_path IS NULL AND final_error IS NULL
        ORDER BY discovered_at ASC
        """
    ).fetchall()


def _record_final_path(conn, clip_id, path):
    conn.execute(
        "UPDATE clips SET final_path = ?, final_error = NULL WHERE clip_id = ?",
        (path, clip_id),
    )
    conn.commit()


def _record_final_error(conn, clip_id, message):
    conn.execute(
        "UPDATE clips SET final_error = ? WHERE clip_id = ?", (message, clip_id)
    )
    conn.commit()


def _resolve_source_video_url(clip_slug):
    """
    clip_slug is the Twitch clip_id (Helix's "id" field is the same value
    used as the GQL clip slug). Returns a signed, time-limited CDN URL for
    the clip's source video, or raises if Twitch's GQL shape doesn't match
    what's expected (deleted clip, endpoint changed, etc).
    """
    response = requests.post(
        TWITCH_GQL_URL,
        headers={"Client-Id": TWITCH_GQL_CLIENT_ID},
        json={
            "operationName": "VideoAccessToken_Clip",
            "query": TWITCH_GQL_CLIP_QUERY,
            "variables": {"slug": clip_slug},
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json().get("data", {}).get("clip")
    if not payload:
        raise RuntimeError(f"Twitch GQL returned no clip data for {clip_slug} (deleted/private?).")

    qualities = payload.get("videoQualities") or []
    if not qualities:
        raise RuntimeError(f"Twitch GQL returned no videoQualities for {clip_slug}.")
    source_url = qualities[0]["sourceURL"]

    token = payload.get("playbackAccessToken") or {}
    signature, value = token.get("signature"), token.get("value")
    if not signature or not value:
        raise RuntimeError(f"Twitch GQL returned no playbackAccessToken for {clip_slug}.")

    return f"{source_url}?sig={signature}&token={quote(value)}"


def _download_source(video_url, dest_path):
    part_path = dest_path + ".part"
    with requests.get(video_url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(part_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    os.replace(part_path, dest_path)  # atomic - no truncated file left as dest_path on a mid-write crash


def _find_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it (winget install Gyan.FFmpeg) "
            "and restart your terminal/Task Scheduler process so the updated PATH takes effect."
        )
    return ffmpeg_path


def _run_ffmpeg(ffmpeg_path, source_path, dest_path, overlay_text_path):
    # scale to cover the target frame then center-crop - equivalent to
    # Shotstack's old "fit: crop" behavior. drawtext reads the overlay
    # text from a file (textfile=) rather than inlining it in the filter
    # string, sidestepping ffmpeg filtergraph escaping issues with
    # apostrophes/colons that can show up in broadcaster names.
    font_path_escaped = FONT_PATH.replace("\\", "/").replace(":", r"\:")
    text_path_escaped = overlay_text_path.replace("\\", "/").replace(":", r"\:")
    # Positioned near the top, not bottom - bottom-of-frame is where game
    # HUDs/captions/killfeeds usually live, and the overlay collided with
    # burned-in Twitch captions there on a real test clip.
    vf = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"drawtext=fontfile='{font_path_escaped}':textfile='{text_path_escaped}':"
        f"fontcolor=white:fontsize=48:x=(w-text_w)/2:y=60:"
        f"box=1:boxcolor=black@0.5:boxborderw=16"
    )
    part_path = dest_path + ".part.mp4"
    result = subprocess.run(
        [
            ffmpeg_path, "-y",
            "-i", source_path,
            "-vf", vf,
            "-c:a", "copy",
            part_path,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {result.stderr[-2000:]}")
    os.replace(part_path, dest_path)


def render_clip(ffmpeg_path, clip_id, broadcaster_name):
    video_url = _resolve_source_video_url(clip_id)
    overlay_text = f"clip via {broadcaster_name or 'unknown'}"

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    source_path = os.path.join(PROCESSED_DIR, f"{clip_id}_source.mp4")
    dest_path = os.path.join(PROCESSED_DIR, f"{clip_id}_final.mp4")
    text_path = os.path.join(PROCESSED_DIR, f"{clip_id}_overlay.txt")

    _download_source(video_url, source_path)
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(overlay_text)

    try:
        _run_ffmpeg(ffmpeg_path, source_path, dest_path, text_path)
    finally:
        # source/overlay-text are intermediates - only final_path matters downstream.
        for temp_path in (source_path, text_path):
            if os.path.exists(temp_path):
                os.remove(temp_path)

    return dest_path


def run(limit):
    ffmpeg_path = _find_ffmpeg()

    conn = _connect()
    try:
        rows = _approved_unprocessed_clips(conn)
        if limit is not None:
            rows = rows[:limit]

        if not rows:
            print("No approved clips waiting on rendering.")
            return

        done = 0
        for clip_id, broadcaster_name in rows:
            print(f"Rendering {clip_id} ({broadcaster_name}) ...")
            try:
                dest_path = render_clip(ffmpeg_path, clip_id, broadcaster_name)
            except Exception as exc:  # noqa: BLE001 - keep going on a per-clip failure
                print(f"  FAILED: {exc}", file=sys.stderr)
                _record_final_error(conn, clip_id, str(exc))
                continue
            _record_final_path(conn, clip_id, dest_path)
            print(f"  -> {dest_path}")
            done += 1

        print(f"Rendered {done}/{len(rows)} clip(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Render approved Twitch clips to vertical video with attribution overlay via local ffmpeg."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max number of clips to process this run"
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    run(args.limit)
