"""Durable storage for shows indexed on demand.

The 832-show index ships as a release asset and is read-only. Anything a
visitor indexes afterwards lands in an ephemeral filesystem, which on a free
host is wiped every time the service sleeps — so those shows are also written
here, and replayed into the local index on the next boot.

Postgres holds them; SQLite still answers every query. Two stores means two
query paths — FTS5 and tsvector — which is two implementations of the gate that
can drift apart, and the gate is the one thing in this codebase that must not.
So Postgres stays deliberately dumb: one row per show, the whole show as JSON,
no querying. It is a log to replay, not a database to search.

Without DATABASE_URL the module reports itself disabled and everything still
works with the baked index alone — which is what a fork with no database of its
own gets.
"""
import json
import os
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS indexed_shows (
    tvmaze_id  INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    payload    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def enabled() -> bool:
    return url() is not None


def _connect():
    """Open a Postgres connection, or return None if it cannot be had.

    Never raises: a durable store that is down must not take the app with it.
    The index still answers; it just forgets new shows again.
    """
    if not enabled():
        return None
    try:
        import psycopg
        return psycopg.connect(url(), connect_timeout=10)
    except Exception as e:                                    # noqa: BLE001
        print(f"durable: unavailable ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def export_show(con: sqlite3.Connection, work_id: int) -> dict:
    """Everything needed to rebuild one show in a fresh index."""
    con.row_factory = sqlite3.Row
    work = dict(con.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone())
    units = [dict(r) for r in con.execute(
        "SELECT * FROM units WHERE work_id=? ORDER BY abs_order", (work_id,))]
    entities = [dict(r) for r in con.execute(
        "SELECT * FROM entities WHERE work_id=?", (work_id,))]
    art = con.execute("SELECT * FROM artwork WHERE work_id=?", (work_id,)).fetchone()
    return {
        "work": work,
        "units": units,
        "entities": entities,
        "artwork": dict(art) if art else None,
    }


def remember(con: sqlite3.Connection, work_id: int) -> bool:
    """Persist a freshly indexed show. Returns whether it was stored."""
    pg = _connect()
    if pg is None:
        return False
    try:
        payload = export_show(con, work_id)
        with pg, pg.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute(
                "INSERT INTO indexed_shows (tvmaze_id, title, payload) VALUES (%s,%s,%s) "
                "ON CONFLICT (tvmaze_id) DO UPDATE SET title = EXCLUDED.title, "
                "payload = EXCLUDED.payload",
                (payload["work"]["tvmaze_id"], payload["work"]["title"],
                 json.dumps(payload)),
            )
        return True
    except Exception as e:                                    # noqa: BLE001
        print(f"durable: could not store show {work_id} ({e})", file=sys.stderr)
        return False
    finally:
        pg.close()


def apply_show(con: sqlite3.Connection, payload: dict) -> None:
    """Insert one exported show into a SQLite index, ids reassigned.

    Ids are deliberately not reused: the baked index owns its own, and a show's
    id in the store is meaningless here.
    """
    work = payload["work"]
    cur = con.cursor()
    cur.execute(
        "INSERT INTO works(title, media_type, tvmaze_id, wikipedia_page, tier) "
        "VALUES(?,?,?,?,?) ON CONFLICT(tvmaze_id) DO UPDATE SET title=excluded.title "
        "RETURNING id",
        (work["title"], work.get("media_type", "tv"), work["tvmaze_id"],
         work.get("wikipedia_page", ""), work.get("tier", "shallow")),
    )
    work_id = cur.fetchone()[0]

    cur.execute("DELETE FROM units WHERE work_id=?", (work_id,))
    for u in payload["units"]:
        cur.execute(
            "INSERT INTO units(work_id, unit_type, grouping, number, abs_order, title, "
            "release_date, summary_text, source_url) VALUES(?,?,?,?,?,?,?,?,?)",
            (work_id, u.get("unit_type", "episode"), u["grouping"], u["number"],
             u["abs_order"], u["title"], u["release_date"], u["summary_text"],
             u["source_url"]),
        )
        cur.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)",
                    (cur.lastrowid, u["summary_text"]))

    cur.execute("DELETE FROM entities WHERE work_id=?", (work_id,))
    for e in payload["entities"]:
        cur.execute(
            "INSERT INTO entities(work_id, name, aliases, type, first_appearance_abs) "
            "VALUES(?,?,?,?,?)",
            (work_id, e["name"], e["aliases"], e["type"], e["first_appearance_abs"]),
        )

    art = payload.get("artwork")
    if art:
        cur.execute(
            "INSERT INTO artwork(work_id, poster_url, network, premiered, genres, rating, "
            "tvmaze_url) VALUES(?,?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET "
            "poster_url=excluded.poster_url",
            (work_id, art["poster_url"], art["network"], art["premiered"],
             art["genres"], art["rating"], art["tvmaze_url"]),
        )
    con.commit()


def restore(con: sqlite3.Connection) -> int:
    """Replay stored shows the local index does not have. Returns how many."""
    pg = _connect()
    if pg is None:
        return 0
    try:
        known = {r[0] for r in con.execute(
            "SELECT tvmaze_id FROM works WHERE tvmaze_id IS NOT NULL")}
        with pg, pg.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("SELECT tvmaze_id, payload FROM indexed_shows")
            rows = cur.fetchall()
        restored = 0
        for tvmaze_id, payload in rows:
            if tvmaze_id in known:
                continue
            apply_show(con, payload if isinstance(payload, dict) else json.loads(payload))
            restored += 1
        if restored:
            print(f"durable: restored {restored} show(s) indexed in earlier sessions")
        return restored
    except Exception as e:                                    # noqa: BLE001
        print(f"durable: could not restore ({e})", file=sys.stderr)
        return 0
    finally:
        pg.close()
