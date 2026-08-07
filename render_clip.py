"""
Takes clips with status='approved' and produces a final vertical (9:16)
video with a "clip via {broadcaster_name}" text overlay burned in, via
Shotstack's Edit API. Downloads the result into processed/ and records
the local path in a new final_path column - the last stop before this
project's (not-yet-built) posting step.

This used to be a two-step Reap (reframe) + Shotstack (overlay) pipeline.
Reap was dropped after review: its Auto Reframe API requires inputs
between 2 minutes and 3 hours long, and Twitch clips are 5-60 seconds -
every clip would have been rejected. Shotstack can do the vertical
reframe itself (via output.aspectRatio + a "crop" fit on the video
clip), so this is now a single render per clip.

Shotstack renders in the cloud and fetches every asset src as a public
HTTPS URL - it can't read a local file or a private one. Rather than
re-hosting a downloaded copy somewhere public (which was the previous
design's PUBLIC_BASE_URL problem, and never got solved), this script
resolves each clip straight to Twitch's own CDN URL and hands that to
Shotstack directly - no local download/re-hosting step at all.

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

Edit API is async: POST /render queues the job and returns an id;
GET /render/{id} is polled until response.status is "done" (or
"failed"), at which point response.url holds the final rendered file.
"""

import os
import sys
import time
import sqlite3
import argparse
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "clips.db")
PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "processed")

SHOTSTACK_API_KEY = os.getenv("SHOTSTACK_API_KEY")

# Shotstack has separate stage (sandbox, free/watermarked) and v1 (production,
# billed) environments under the same API - default to stage so this can't
# accidentally spend real render credits until you're ready.
ENV_URLS = {
    "stage": "https://api.shotstack.io/edit/stage",
    "v1": "https://api.shotstack.io/edit/v1",
}

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

POLL_INTERVAL_SECONDS = 5
MAX_POLLS = 60  # ~5 minutes ceiling before we give up on one clip


def _shotstack_headers():
    if not SHOTSTACK_API_KEY:
        raise RuntimeError("Missing SHOTSTACK_API_KEY. Check your .env file.")
    return {"x-api-key": SHOTSTACK_API_KEY, "Content-Type": "application/json"}


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
    # burning render quota on every single run - see run()'s docstring note.
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


def _build_edit(video_url, overlay_text):
    return {
        "timeline": {
            "background": "#000000",
            "tracks": [
                # Shotstack layers top-down by track index - the overlay
                # must come before the video track or the video paints
                # over the text.
                {
                    "clips": [
                        {
                            "asset": {
                                "type": "text",
                                "text": overlay_text,
                                "font": {"family": "Montserrat", "size": 36, "color": "#ffffff"},
                                "alignment": {"horizontal": "center", "vertical": "bottom"},
                            },
                            "start": 0,
                            "length": "end",
                            "offset": {"y": 0.05},
                        }
                    ]
                },
                {
                    "clips": [
                        {
                            "asset": {"type": "video", "src": video_url},
                            "start": 0,
                            "length": "auto",
                            "fit": "crop",
                        }
                    ]
                },
            ],
        },
        "output": {"format": "mp4", "resolution": "hd", "aspectRatio": "9:16"},
    }


def _submit_render(env_url, edit):
    response = requests.post(
        f"{env_url}/render", headers=_shotstack_headers(), json=edit, timeout=15
    )
    response.raise_for_status()
    return response.json()["response"]["id"]


def _poll_render(env_url, render_id):
    headers = _shotstack_headers()
    for _ in range(MAX_POLLS):
        response = requests.get(
            f"{env_url}/render/{render_id}", headers=headers, timeout=15
        )
        response.raise_for_status()
        result = response.json()["response"]
        status = result.get("status")
        if status == "done":
            return result["url"]
        if status == "failed":
            raise RuntimeError(f"Shotstack render {render_id} failed.")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"Shotstack render {render_id} did not finish within the poll window.")


def _download_final(video_url, dest_path):
    part_path = dest_path + ".part"
    with requests.get(video_url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(part_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    os.replace(part_path, dest_path)  # atomic - no truncated file left as dest_path on a mid-write crash


def render_clip(env_url, clip_id, broadcaster_name):
    video_url = _resolve_source_video_url(clip_id)
    overlay_text = f"clip via {broadcaster_name or 'unknown'}"

    edit = _build_edit(video_url, overlay_text)
    render_id = _submit_render(env_url, edit)
    result_url = _poll_render(env_url, render_id)

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    dest_path = os.path.join(PROCESSED_DIR, f"{clip_id}_final.mp4")
    _download_final(result_url, dest_path)
    return dest_path


def run(env_name, limit):
    env_url = ENV_URLS[env_name]

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
                dest_path = render_clip(env_url, clip_id, broadcaster_name)
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
        description="Render approved Twitch clips to vertical video with attribution overlay via Shotstack."
    )
    parser.add_argument(
        "--env",
        choices=["stage", "v1"],
        default="stage",
        help="Shotstack environment - 'stage' is the free/watermarked sandbox, 'v1' is billed production (default: stage)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max number of clips to process this run"
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    run(args.env, args.limit)
