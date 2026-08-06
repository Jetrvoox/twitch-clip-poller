"""
Handles Twitch App Access Token retrieval (Client Credentials flow).
No user login involved. Token is cached to a local file and refreshed
automatically when expired or close to expiring.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TOKEN_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".token_cache.json")

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")


def _load_cached_token():
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None
    try:
        with open(TOKEN_CACHE_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt/truncated cache file - just fetch a fresh token instead of crashing.
        return None
    # Refresh 5 minutes before actual expiry to avoid edge-case failures mid-run
    if data.get("expires_at", 0) - 300 > time.time():
        return data["access_token"]
    return None


def _save_token_cache(access_token, expires_in):
    data = {
        "access_token": access_token,
        "expires_at": time.time() + expires_in,
    }
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(data, f)


def get_app_access_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET. "
            "Check your .env file."
        )

    cached = _load_cached_token()
    if cached:
        return cached

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()

    access_token = payload["access_token"]
    expires_in = payload["expires_in"]

    _save_token_cache(access_token, expires_in)
    return access_token


def get_auth_headers():
    return {
        "Client-Id": CLIENT_ID,
        "Authorization": f"Bearer {get_app_access_token()}",
    }


if __name__ == "__main__":
    token = get_app_access_token()
    print("Token acquired successfully.")
    print(f"First 8 chars: {token[:8]}... (truncated, do not share full token)")
