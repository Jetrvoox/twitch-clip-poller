"""
Minimal approval-gate CLI. Lets you list pending clips, open one to watch,
then mark it approved/rejected so it's excluded from future review passes.
This is a placeholder for the Discord bot / web page - same 'status'
column, so nothing downstream needs to change when that gets built.
"""

import os
import sqlite3
import argparse
import webbrowser

DB_PATH = os.path.join(os.path.dirname(__file__), "clips.db")


def _connect():
    if not os.path.exists(DB_PATH):
        raise SystemExit("No clips.db found - run poller.py at least once first.")
    return sqlite3.connect(DB_PATH)


def list_clips(status):
    conn = _connect()
    rows = conn.execute(
        """
        SELECT clip_id, broadcaster_name, title, view_count, url
        FROM clips WHERE status = ?
        ORDER BY view_count DESC
        """,
        (status,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No clips with status '{status}'.")
        return

    for clip_id, broadcaster, title, views, url in rows:
        print(f"[{clip_id}] {broadcaster} - {title!r} ({views} views)")
        print(f"  {url}")


def set_status(clip_id, status, open_first):
    conn = _connect()
    row = conn.execute(
        "SELECT url FROM clips WHERE clip_id = ?", (clip_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise SystemExit(f"No clip found with clip_id '{clip_id}'.")

    if open_first:
        webbrowser.open(row[0])

    conn.execute("UPDATE clips SET status = ? WHERE clip_id = ?", (status, clip_id))
    conn.commit()
    conn.close()
    print(f"{clip_id} -> {status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review pending Twitch clips.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="List clips by status (default: pending)")
    list_cmd.add_argument("--status", default="pending")

    approve_cmd = sub.add_parser("approve", help="Mark a clip approved")
    approve_cmd.add_argument("clip_id")
    approve_cmd.add_argument(
        "--open", action="store_true", help="Open the clip URL in your browser first"
    )

    reject_cmd = sub.add_parser("reject", help="Mark a clip rejected")
    reject_cmd.add_argument("clip_id")
    reject_cmd.add_argument(
        "--open", action="store_true", help="Open the clip URL in your browser first"
    )

    args = parser.parse_args()

    if args.command == "list":
        list_clips(args.status)
    elif args.command == "approve":
        set_status(args.clip_id, "approved", args.open)
    elif args.command == "reject":
        set_status(args.clip_id, "rejected", args.open)
