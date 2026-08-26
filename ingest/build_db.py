"""Build the SpoilerGate SQLite from TVMaze spine + Wikipedia summaries.

Usage: python -m ingest.build_db "Breaking Bad" "Dark" ...
Writes data/spoilergate.db (replaces existing rows per work, idempotent).
"""
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
    con.executescript(SCHEMA)
    return con


def ingest_show(con: sqlite3.Connection, query: str) -> None:
    show = tvmaze.fetch_show(query)
    spine = tvmaze.fetch_episodes(show["id"])
    found = wikipedia.find_episode_page(show["name"])
    page, rows = (found[0], wikipedia.collect_rows(found[1])) if found else ("", [])
    units = wikipedia.match_to_spine(spine, rows)
    with_summary = sum(1 for u in units if u["summary"])
    print(f"{show['name']}: {len(spine)} episodes, {with_summary} summaries (page: {page or 'none'})")

    cur = con.cursor()
    cur.execute(
        "INSERT INTO works(title, tvmaze_id, wikipedia_page) VALUES(?,?,?) "
        "ON CONFLICT(tvmaze_id) DO UPDATE SET title=excluded.title, wikipedia_page=excluded.wikipedia_page "
        "RETURNING id",
        (show["name"], show["id"], page),
    )
    work_id = cur.fetchone()[0]
    old = [r[0] for r in cur.execute("SELECT id FROM units WHERE work_id=?", (work_id,))]
    for uid in old:
        cur.execute("INSERT INTO units_fts(units_fts, rowid, summary_text) "
                    "SELECT 'delete', id, summary_text FROM units WHERE id=?", (uid,))
    cur.execute("DELETE FROM units WHERE work_id=?", (work_id,))
    src = f"https://en.wikipedia.org/wiki/{page.replace(' ', '_')}" if page else ""
    for u in units:
        cur.execute(
            "INSERT INTO units(work_id, grouping, number, abs_order, title, release_date, summary_text, source_url) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (work_id, u["grouping"], u["number"], u["abs_order"], u["title"],
             u["release_date"], u["summary"], src),
        )
        cur.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)",
                    (cur.lastrowid, u["summary"]))
    con.commit()


def main() -> None:
    shows = sys.argv[1:]
    if not shows:
        sys.exit("usage: python -m ingest.build_db <show name> [...]")
    con = connect()
    for q in shows:
        ingest_show(con, q)
    con.close()


if __name__ == "__main__":
    main()
