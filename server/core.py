"""Gate, retrieval, post-guard. No web framework here — this is the testable core.

The gate is SQL (`abs_order <= gate_abs`): the model never sees rows beyond the
viewer's position. gate_abs = abs_order of the last episode the viewer finished.
"""
import os
import re
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "spoilergate.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or os.environ.get("SPOILERGATE_DB", DEFAULT_DB))
    con.row_factory = sqlite3.Row
    return con


def search_works(con: sqlite3.Connection, q: str) -> list[dict]:
    rows = con.execute(
        "SELECT id, title, media_type, tier FROM works "
        "WHERE title LIKE ? AND tier != 'empty' "  # 'empty' = tried, no summaries to answer from
        "ORDER BY length(title), title LIMIT 20",
        (f"%{q}%",),
    )
    return [dict(r) for r in rows]


def list_units(con: sqlite3.Connection, work_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT abs_order, grouping, number, title, release_date FROM units "
        "WHERE work_id=? ORDER BY abs_order",
        (work_id,),
    )
    return [dict(r) for r in rows]


def fts_query(question: str) -> str:
    """Question text -> safe FTS5 OR-query of its words."""
    words = re.findall(r"[A-Za-z0-9]+", question)
    return " OR ".join(f'"{w}"' for w in words) if words else '""'


def gated_retrieve(
    con: sqlite3.Connection, work_id: int, gate_abs: int, question: str, k: int = 6
) -> list[dict]:
    """BM25 top-k over episode summaries, gate applied in SQL before ranking."""
    rows = con.execute(
        "SELECT u.abs_order, u.grouping, u.number, u.title, u.summary_text, u.source_url "
        "FROM units_fts JOIN units u ON u.id = units_fts.rowid "
        "WHERE units_fts MATCH ? AND u.work_id = ? AND u.abs_order <= ? "
        "AND u.summary_text != '' ORDER BY bm25(units_fts) LIMIT ?",
        (fts_query(question), work_id, gate_abs, k),
    )
    return [dict(r) for r in rows]


def gated_units(
    con: sqlite3.Connection, work_id: int, gate_abs: int, limit: int = 25
) -> list[dict]:
    """The most recent `limit` watched episodes, oldest first. Gate in SQL."""
    rows = con.execute(
        "SELECT abs_order, grouping, number, title, summary_text FROM units "
        "WHERE work_id=? AND abs_order <= ? AND summary_text != '' "
        "ORDER BY abs_order DESC LIMIT ?",
        (work_id, gate_abs, limit),
    )
    return sorted((dict(r) for r in rows), key=lambda u: u["abs_order"])


def future_entities(con: sqlite3.Connection, work_id: int, gate_abs: int) -> list[str]:
    """Names the viewer must not hear yet (post-guard blocklist)."""
    names: list[str] = []
    for r in con.execute(
        "SELECT name, aliases FROM entities WHERE work_id=? AND first_appearance_abs > ?",
        (work_id, gate_abs),
    ):
        names.append(r["name"])
        names.extend(a.strip() for a in r["aliases"].split("|") if a.strip())
    return names


def guard_leaks(answer: str, blocklist: list[str]) -> list[str]:
    """Future-entity names appearing in the answer. Word-boundary, case-insensitive."""
    low = answer.lower()
    return [
        n for n in blocklist
        if re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", low)
    ]


def ep_label(u: dict) -> str:
    return f"S{u['grouping']:02d}E{u['number']:02d}"
