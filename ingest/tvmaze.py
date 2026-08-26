"""TVMaze episode spine: canonical ordering for a show.

TVMaze is CC BY-SA, keyless, ~20 req/10s. We take the ordering skeleton
(season/number/airdate/title); summary text comes from Wikipedia.
"""
import time

import httpx

from ingest import http

BASE = "https://api.tvmaze.com"


def fetch_show(query: str) -> dict:
    r = http.get(f"{BASE}/singlesearch/shows", params={"q": query}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_show_index() -> list[dict]:
    """Every show TVMaze knows, with its `weight` popularity score (0-100).
    Paginated 250 at a time; the index ends with a 404."""
    shows, page = [], 0
    while True:
        r = http.get(f"{BASE}/shows", params={"page": page}, timeout=60)
        if r.status_code == 404:
            return shows
        r.raise_for_status()
        shows.extend(r.json())
        page += 1
        time.sleep(0.6)


def fetch_show_by_id(show_id: int) -> dict:
    r = http.get(f"{BASE}/shows/{show_id}", timeout=30)
    r.raise_for_status()
    time.sleep(0.55)  # 20 calls / 10s is the documented limit
    return r.json()


def fetch_cast(show_id: int) -> list[str]:
    """Billed character names, e.g. "Gustavo 'Gus' Fring". Empty on failure —
    entity extraction still has the wikilink source to fall back on."""
    try:
        r = http.get(f"{BASE}/shows/{show_id}/cast", timeout=30)
        r.raise_for_status()
        time.sleep(0.6)
        return [c["character"]["name"] for c in r.json() if not c.get("self")]
    except (httpx.HTTPError, KeyError):
        return []


def fetch_episodes(show_id: int) -> list[dict]:
    """Regular episodes only, in airing order. abs_order assigned by position."""
    r = http.get(f"{BASE}/shows/{show_id}/episodes", timeout=30)
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
