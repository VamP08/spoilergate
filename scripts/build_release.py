"""Package the index for a GitHub release.

Two traps this exists to avoid, both silent:

`VACUUM INTO` rather than copying the file. The database runs in WAL mode, so
recent commits sit in the -wal sidecar until a checkpoint — copying only the
.db captured a snapshot missing the last 74 shows' artwork, and nothing about
it looked wrong. VACUUM INTO writes a consistent, compacted copy of everything
committed.

Gzip, because GitHub releases reject a bare .db, and a SQLite file of mostly
prose halves in size anyway.

Usage: python scripts/build_release.py
"""
import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SOURCE = DATA / "spoilergate.db"
SNAPSHOT = DATA / "release-snapshot.db"
ARCHIVE = DATA / "spoilergate.db.gz"

COUNTS = {
    "works with summaries": "SELECT COUNT(*) FROM works WHERE tier != 'empty'",
    "episodes": "SELECT COUNT(*) FROM units",
    "entities": "SELECT COUNT(*) FROM entities",
    "posters": "SELECT COUNT(*) FROM artwork WHERE LENGTH(poster_url) > 0",
}


def counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return {label: con.execute(sql).fetchone()[0] for label, sql in COUNTS.items()}
    finally:
        con.close()


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"no index at {SOURCE}")
    SNAPSHOT.unlink(missing_ok=True)

    con = sqlite3.connect(SOURCE)
    con.execute("VACUUM INTO ?", (SNAPSHOT.as_posix(),))
    con.close()

    live, snapped = counts(SOURCE), counts(SNAPSHOT)
    for label in COUNTS:
        flag = "" if live[label] == snapped[label] else "  <-- MISMATCH"
        print(f"{label:22} {snapped[label]:>7}{flag}")
    if live != snapped:
        sys.exit("snapshot does not match the live index — refusing to package it")

    with SNAPSHOT.open("rb") as src, gzip.open(ARCHIVE, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    SNAPSHOT.unlink()

    print(f"\nupload this: {ARCHIVE}")
    print(f"{ARCHIVE.stat().st_size / 1e6:.1f} MB compressed "
          f"from {SOURCE.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
