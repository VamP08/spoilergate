"""Download the prebuilt index at deploy time.

The index is too big for git and takes hours of polite API calls to build, so
it ships as a release asset and the deploy fetches it. Fails loudly: a server
that starts without an index answers every question with a refusal, which looks
exactly like the product working.

Usage: SPOILERGATE_DB_URL=https://... python scripts/fetch_db.py
"""
import os
import sys
from pathlib import Path

import httpx

DEST = Path(__file__).resolve().parent.parent / "data" / "spoilergate.db"
MIN_BYTES = 1_000_000  # a real index is tens of MB; anything less is an error page


def main() -> None:
    url = os.environ.get("SPOILERGATE_DB_URL")
    if not url:
        sys.exit("SPOILERGATE_DB_URL is not set — nothing to fetch")

    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching index from {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with DEST.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)

    size = DEST.stat().st_size
    if size < MIN_BYTES:
        DEST.unlink()
        sys.exit(f"downloaded only {size} bytes — that is not the index")
    print(f"index ready: {size / 1e6:.1f} MB at {DEST}")


if __name__ == "__main__":
    main()
