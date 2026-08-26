"""Key-value cache for generated text (recaps).

Recaps depend only on (work, position), never on who asked — so one user's
recap serves everyone after them. Stored in its own SQLite file because the
product DB is read-only.

ponytail: ephemeral on Render free (no persistent disk), so a cold start
loses the cache and recaps regenerate. Move to Neon when M5 needs
lazy-ingest results to survive restarts anyway.
"""
import os
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "cache.db"


def _con() -> sqlite3.Connection:
    path = Path(os.environ.get("SPOILERGATE_CACHE", DEFAULT_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    return con


def get(key: str) -> str | None:
    with _con() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def put(key: str, value: str) -> None:
    with _con() as con:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, value))
