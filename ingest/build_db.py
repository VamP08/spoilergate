"""Build the SpoilerGate SQLite from TVMaze spine + Wikipedia summaries.

Usage: python -m ingest.build_db "Breaking Bad" "Dark" ...
Writes data/spoilergate.db (replaces existing rows per work, idempotent).
"""
import json
import sqlite3
import sys
from pathlib import Path

from ingest import tvmaze, wikipedia

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "spoilergate.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS works(
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'tv',
    tvmaze_id INTEGER UNIQUE,
    wikipedia_page TEXT,
    tier TEXT NOT NULL DEFAULT 'shallow'
);
CREATE TABLE IF NOT EXISTS units(
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id),
    unit_type TEXT NOT NULL DEFAULT 'episode',
    grouping INTEGER,
    number INTEGER,
    abs_order INTEGER NOT NULL,
    title TEXT,
    release_date TEXT,
    summary_text TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    UNIQUE(work_id, abs_order)
);
CREATE TABLE IF NOT EXISTS entities(
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id),
    name TEXT NOT NULL,
    aliases TEXT DEFAULT '',
    type TEXT DEFAULT 'character',
    first_appearance_abs INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS facts(
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id),
    entity_id INTEGER REFERENCES entities(id),
    revealed_abs INTEGER NOT NULL,
    fact_text TEXT NOT NULL,
    importance INTEGER DEFAULT 2,
    source_url TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    summary_text, content='units', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_text, content='facts', content_rowid='id'
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")  # server can read while a bulk ingest writes
    con.executescript(SCHEMA)
    return con


def already_attempted(con: sqlite3.Connection, tvmaze_id: int) -> bool:
    """A work row exists for this show, so a previous run already tried it —
    either it has summaries (tier 'shallow') or it has none (tier 'empty')."""
    return con.execute(
        "SELECT 1 FROM works WHERE tvmaze_id = ?", (tvmaze_id,)
    ).fetchone() is not None


def ingest_show(con: sqlite3.Connection, query: str | dict) -> None:
    show = tvmaze.fetch_show(query) if isinstance(query, str) else query
    spine = tvmaze.fetch_episodes(show["id"])
    found = wikipedia.find_episode_page(show["name"])
    page, rows = (found[0], wikipedia.collect_rows(found[1])) if found else ("", [])
    units = wikipedia.match_to_spine(spine, rows)
    with_summary = sum(1 for u in units if u["summary"])
    tier = "shallow" if with_summary else "empty"
    print(f"{show['name']}: {len(spine)} episodes, {with_summary} summaries (page: {page or 'none'})")

    cur = con.cursor()
    cur.execute(
        "INSERT INTO works(title, tvmaze_id, wikipedia_page, tier) VALUES(?,?,?,?) "
        "ON CONFLICT(tvmaze_id) DO UPDATE SET title=excluded.title, "
        "wikipedia_page=excluded.wikipedia_page, tier=excluded.tier "
        "RETURNING id",
        (show["name"], show["id"], page, tier),
    )
    work_id = cur.fetchone()[0]
    old = [r[0] for r in cur.execute("SELECT id FROM units WHERE work_id=?", (work_id,))]
    for uid in old:
        cur.execute("INSERT INTO units_fts(units_fts, rowid, summary_text) "
                    "SELECT 'delete', id, summary_text FROM units WHERE id=?", (uid,))
    cur.execute("DELETE FROM units WHERE work_id=?", (work_id,))
    src = f"https://en.wikipedia.org/wiki/{page.replace(' ', '_')}" if page else ""
    for u in units if with_summary else []:  # a spine with no summaries answers nothing
        cur.execute(
            "INSERT INTO units(work_id, grouping, number, abs_order, title, release_date, summary_text, source_url) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (work_id, u["grouping"], u["number"], u["abs_order"], u["title"],
             u["release_date"], u["summary"], src),
        )
        cur.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)",
                    (cur.lastrowid, u["summary"]))
    con.commit()


INDEX_PATH = DB_PATH.parent / "tvmaze_ranked.json"


def ranked_shows(top: int) -> list[dict]:
    """Shows by descending popularity. The full index is ~94k shows over 376
    pages of ~1MB each — twenty minutes — so keep the ranked head on disk and
    only re-crawl when it is missing."""
    if INDEX_PATH.exists():
        cached = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if len(cached) >= top:
            return cached[:top]
    index = tvmaze.fetch_show_index()
    print(f"tvmaze index: {len(index)} shows")
    ranked = sorted(index, key=lambda s: s.get("weight") or 0, reverse=True)[:5000]
    INDEX_PATH.write_text(json.dumps(ranked), encoding="utf-8")
    return ranked[:top]


def ingest_popular(con: sqlite3.Connection, top: int) -> None:
    """Ingest the `top` most popular shows TVMaze knows of, skipping any a
    previous run already attempted. Resumable: rerun the same command."""
    ranked = ranked_shows(top)
    done = failed = skipped = 0
    for i, show in enumerate(ranked, 1):
        if already_attempted(con, show["id"]):
            skipped += 1
            continue
        try:
            ingest_show(con, show)
            done += 1
        except Exception as e:  # one bad show must not end a two-hour run
            failed += 1
            print(f"[{i}/{len(ranked)}] FAILED {show['name']}: {type(e).__name__}: {e}")
        if i % 25 == 0:
            print(f"--- {i}/{len(ranked)} (ingested {done}, failed {failed}, skipped {skipped})")
    print(f"done: ingested {done}, failed {failed}, skipped {skipped}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python -m ingest.build_db <show name> [...] | --top N")
    con = connect()
    if args[0] == "--top":
        ingest_popular(con, int(args[1]))
    else:
        for q in args:
            ingest_show(con, q)
    con.close()


if __name__ == "__main__":
    main()
