"""TVMaze episode spine: canonical ordering for a show.

TVMaze is CC BY-SA, keyless, ~20 req/10s. We take the ordering skeleton
(season/number/airdate/title); summary text comes from Wikipedia.
"""
import time

import httpx

BASE = "https://api.tvmaze.com"


def fetch_show(query: str) -> dict:
    r = httpx.get(f"{BASE}/singlesearch/shows", params={"q": query}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_episodes(show_id: int) -> list[dict]:
    """Regular episodes only, in airing order. abs_order assigned by position."""
    r = httpx.get(f"{BASE}/shows/{show_id}/episodes", timeout=30)
    r.raise_for_status()
    time.sleep(0.6)  # stay far under the rate limit when looping shows
    eps = [e for e in r.json() if e.get("type") == "regular"]
    return [
        {
            "grouping": e["season"],
            "number": e["number"],
            "abs_order": i + 1,
            "title": e["name"],
            "release_date": e.get("airdate") or "",
            "tvmaze_id": e["id"],
        }
        for i, e in enumerate(eps)
    ]
