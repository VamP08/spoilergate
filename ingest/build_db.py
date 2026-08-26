"""Build the SpoilerGate SQLite from TVMaze spine + Wikipedia summaries.

Usage: python -m ingest.build_db "Breaking Bad" "Dark" ...
Writes data/spoilergate.db (replaces existing rows per work, idempotent).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

from ingest import characters, tvmaze, wikipedia

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
CREATE TABLE IF NOT EXISTS artwork(
    work_id INTEGER PRIMARY KEY REFERENCES works(id),
    -- Hotlinked from TVMaze's CDN, never copied here: their licence covers the
    -- metadata, not the images, and their admins are explicit that linking
    -- keeps takedown responsibility with them while mirroring moves it to us.
    poster_url TEXT DEFAULT '',
    network TEXT DEFAULT '',
    premiered TEXT DEFAULT '',
    genres TEXT DEFAULT '',
    rating REAL,
    tvmaze_url TEXT DEFAULT ''
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


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("SPOILERGATE_DB", DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # server can read while a bulk ingest writes
    con.execute("PRAGMA busy_timeout=15000")  # two ingest passes can overlap; queue, don't fail
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
    if with_summary:
        write_entities(con, work_id, units, tvmaze.fetch_cast(show["id"]))


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


def write_entities(con: sqlite3.Connection, work_id: int, units: list[dict],
                   cast: list[str] = ()) -> int:
    """Replace this work's entities: wikilinks give guests and terms, the billed
    cast gives the leads the episode tables never link."""
    text_units = [(u["abs_order"], u.get("summary", "")) for u in units]
    from_cast = characters.entities_from_cast(list(cast), text_units)
    found = characters.redate_by_text(
        characters.merge_entities(characters.entities_from_units(units), from_cast),
        text_units,
    )
    found = characters.drop_episode_titles(found, [u.get("title", "") for u in units])
    con.execute("DELETE FROM entities WHERE work_id=?", (work_id,))
    con.executemany(
        "INSERT INTO entities(work_id, name, aliases, type, first_appearance_abs) "
        "VALUES(?,?,?,?,?)",
        [(work_id, e["name"], e["aliases"], e["type"], e["first_appearance_abs"])
         for e in found],
    )
    con.commit()
    return len(found)


def entities_pass(con: sqlite3.Connection) -> None:
    """Re-derive entities for shows ingested before links were captured.

    New ingests write entities inline; this refetches the episode page for the
    rest, and can be rerun whenever the link heuristics improve.
    """
    works = con.execute(
        "SELECT id, title, wikipedia_page, tvmaze_id FROM works WHERE tier='shallow' "
        "AND wikipedia_page != '' AND id NOT IN (SELECT DISTINCT work_id FROM entities) "
        "ORDER BY id"
    ).fetchall()
    print(f"entities pass: {len(works)} shows")
    for i, (work_id, title, page, tvmaze_id) in enumerate(works, 1):
        try:
            stored = [
                {"abs_order": r[0], "title": r[1], "summary": r[2]}
                for r in con.execute(
                    "SELECT abs_order, title, summary_text FROM units "
                    "WHERE work_id=? ORDER BY abs_order",
                    (work_id,),
                )
            ]
            text = wikipedia.fetch_wikitext(page)
            rows = wikipedia.collect_rows(text) if text else []
            units = wikipedia.match_to_spine(stored, rows)
            # match_to_spine overwrites summaries with the freshly parsed ones;
            # keep the stored text so cast dating works even if a row fails to match.
            for unit, saved in zip(units, stored):
                unit["summary"] = unit["summary"] or saved["summary"]
            n = write_entities(con, work_id, units, tvmaze.fetch_cast(tvmaze_id))
            chars = sum(1 for e in characters.entities_from_units(units)
                        if e["type"] == "character")
            print(f"[{i}/{len(works)}] {title}: {n} entities ({chars} linked characters)")
        except Exception as e:
            print(f"[{i}/{len(works)}] FAILED {title}: {type(e).__name__}: {e}")


def write_artwork(con: sqlite3.Connection, work_id: int, show: dict) -> None:
    """Store the poster URL and the presentation metadata beside it.

    Only URLs and CC BY-SA metadata land here — the images themselves stay on
    TVMaze's CDN and are hotlinked by the page.
    """
    image = show.get("image") or {}
    con.execute(
        "INSERT INTO artwork(work_id, poster_url, network, premiered, genres, rating, "
        "tvmaze_url) VALUES(?,?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET "
        "poster_url=excluded.poster_url, network=excluded.network, "
        "premiered=excluded.premiered, genres=excluded.genres, rating=excluded.rating, "
        "tvmaze_url=excluded.tvmaze_url",
        (
            work_id,
            image.get("medium") or image.get("original") or "",
            ((show.get("network") or show.get("webChannel") or {}).get("name")) or "",
            (show.get("premiered") or "")[:4],
            "|".join(show.get("genres") or []),
            (show.get("rating") or {}).get("average"),
            show.get("url") or "",
        ),
    )


def artwork_pass(con: sqlite3.Connection) -> None:
    """Fetch posters for every show that can answer something."""
    works = con.execute(
        "SELECT id, tvmaze_id, title FROM works WHERE tier != 'empty' "
        "AND id NOT IN (SELECT work_id FROM artwork) ORDER BY id"
    ).fetchall()
    print(f"artwork pass: {len(works)} shows")
    posters = 0
    for i, (work_id, tvmaze_id, title) in enumerate(works, 1):
        try:
            show = tvmaze.fetch_show_by_id(tvmaze_id)
            write_artwork(con, work_id, show)
            con.commit()
            posters += bool((show.get("image") or {}).get("medium"))
        except Exception as e:
            print(f"[{i}/{len(works)}] FAILED {title}: {type(e).__name__}: {e}")
        if i % 50 == 0:
            print(f"--- {i}/{len(works)} ({posters} with a poster)")
    print(f"done: {posters} posters over {len(works)} shows")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python -m ingest.build_db <show name> [...] | --top N | --entities")
    con = connect()
    if args[0] == "--top":
        ingest_popular(con, int(args[1]))
    elif args[0] == "--entities":
        entities_pass(con)
    elif args[0] == "--artwork":
        artwork_pass(con)
    else:
        for q in args:
            ingest_show(con, q)
    con.close()


if __name__ == "__main__":
    main()
