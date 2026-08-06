"""
One-off helper: looks up the Twitch game_id for a given game name.
Run this once, note the game_id, put it in your poller config.
Usage: python lookup_game_id.py "Darwin Project"
"""

import sys
import requests
from auth import get_auth_headers

GAMES_URL = "https://api.twitch.tv/helix/games"


def lookup_game_id(game_name: str):
    response = requests.get(
        GAMES_URL,
        headers=get_auth_headers(),
        params={"name": game_name},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json().get("data", [])

    if not data:
        print(f"No game found matching name: '{game_name}'")
        print("Double-check exact spelling/capitalization as listed on Twitch.")
        return None

    for game in data:
        print(f"Name: {game['name']}")
        print(f"game_id: {game['id']}")
        print(f"box_art_url: {game['box_art_url']}")
        print("-" * 40)

    return data


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Darwin Project"
    lookup_game_id(name)
