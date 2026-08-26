"""Download the prebuilt index at deploy time.

The index is too big for git and takes hours of polite API calls to build, so
it ships as a release asset and the deploy fetches it. Fails loudly: a server
that starts without an index answers every question with a refusal, which looks
exactly like the product working.

GitHub releases reject a bare `.db`, and a gzipped SQLite file is less than half
the size anyway, so the asset is `spoilergate.db.gz`. Decompression is decided by
the file's magic bytes rather than its name, because a URL can be redirected or
renamed and the content is the thing that matters.

Usage: SPOILERGATE_DB_URL=https://... python scripts/fetch_db.py
"""
import gzip
import os
import shutil
import sys
from pathlib import Path

import httpx

DEST = Path(__file__).resolve().parent.parent / "data" / "spoilergate.db"
MIN_BYTES = 1_000_000  # a real index is tens of MB; anything less is an error page
GZIP_MAGIC = b"\x1f\x8b"


def main() -> None:
    url = os.environ.get("SPOILERGATE_DB_URL")
    if not url:
        sys.exit("SPOILERGATE_DB_URL is not set — nothing to fetch")

    DEST.parent.mkdir(parents=True, exist_ok=True)
    download = DEST.with_suffix(".download")
    print(f"fetching index from {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with download.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)

    with download.open("rb") as fh:
        compressed = fh.read(2) == GZIP_MAGIC

    if compressed:
        print(f"decompressing {download.stat().st_size / 1e6:.1f} MB")
        with gzip.open(download, "rb") as src, DEST.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        download.unlink()
    else:
        download.replace(DEST)

    size = DEST.stat().st_size
    if size < MIN_BYTES:
        DEST.unlink()
        sys.exit(f"downloaded only {size} bytes — that is not the index")
    print(f"index ready: {size / 1e6:.1f} MB at {DEST}")


if __name__ == "__main__":
    main()
