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
    columns = (
        "SELECT w.id, w.title, w.media_type, w.tier, "
        "COALESCE(a.poster_url,'') poster_url, COALESCE(a.premiered,'') premiered, "
        "COALESCE(a.network,'') network "
        "FROM works w LEFT JOIN artwork a ON a.work_id = w.id "
        "WHERE w.tier != 'empty' "  # 'empty' = tried, nothing to answer from
    )
    if not q.strip():
        # No query means the landing shelf. Shows were ingested in TVMaze
        # popularity order, so the row id doubles as a popularity rank and the
        # best-known shows come back without storing a score.
        rows = con.execute(
            columns + "AND a.poster_url != '' ORDER BY w.id LIMIT 24")
    else:
        rows = con.execute(
            columns + "AND w.title LIKE ? ORDER BY length(w.title), w.title LIMIT 20",
            (f"%{q}%",))
    return [dict(r) for r in rows]


def work_detail(con: sqlite3.Connection, work_id: int) -> dict | None:
    """Everything the page needs to render a show in one request."""
    row = con.execute(
        "SELECT w.id, w.title, w.media_type, w.tier, "
        "COALESCE(a.poster_url,'') poster_url, COALESCE(a.network,'') network, "
        "COALESCE(a.premiered,'') premiered, COALESCE(a.genres,'') genres, "
        "a.rating, COALESCE(a.tvmaze_url,'') tvmaze_url "
        "FROM works w LEFT JOIN artwork a ON a.work_id = w.id WHERE w.id = ?",
        (work_id,),
    ).fetchone()
    if row is None:
        return None
    work = dict(row)
    work["genres"] = [g for g in work["genres"].split("|") if g]
    work["units"] = list_units(con, work_id)
    work["guard"] = guard_state(con, work_id)
    return work


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


def gated_entities(con: sqlite3.Connection, work_id: int, gate_abs: int) -> list[dict]:
    """Characters the viewer has already met. Who exists is itself a spoiler,
    so this list is gated exactly like everything else. Only people are listed —
    the post-guard also blocks linked terms, but nobody wants to browse those."""
    rows = con.execute(
        "SELECT id, name, first_appearance_abs FROM entities "
        "WHERE work_id=? AND first_appearance_abs <= ? AND type='character' "
        "ORDER BY first_appearance_abs, name",
        (work_id, gate_abs),
    )
    return [dict(r) for r in rows]


def search_terms(name: str, aliases: str) -> list[str]:
    """Ways a summary might refer to this character.

    Summaries introduce "Jesse Pinkman" once and say "Jesse" thereafter, so the
    given name has to be one of the terms. Over-matching is safe here in a way
    it never is in `guard_leaks`: the worst case is an extra episode of context,
    where the worst case there is a wrongly refused answer.
    """
    terms = [name, *(a for a in aliases.split("|") if a)]
    given = name.split()[0]
    if len(given) >= 4 and given not in terms:
        terms.append(given)
    return terms


def character_units(
    con: sqlite3.Connection, work_id: int, name: str, aliases: str, gate_abs: int,
    k: int = 6,
) -> list[dict]:
    """Watched episodes that mention this character, most recent k, oldest first."""
    terms = search_terms(name, aliases)
    where = " OR ".join(["summary_text LIKE ?"] * len(terms))
    rows = con.execute(
        "SELECT abs_order, grouping, number, title, summary_text FROM units "
        f"WHERE work_id=? AND abs_order <= ? AND ({where}) "
        "ORDER BY abs_order DESC LIMIT ?",
        (work_id, gate_abs, *(f"%{t}%" for t in terms), k),
    )
    return sorted((dict(r) for r in rows), key=lambda u: u["abs_order"])


def guard_state(con: sqlite3.Connection, work_id: int) -> str:
    """Whether gate 2 exists for this work. Shows whose summaries link nothing
    get no entity index, so the post-guard has no blocklist and the retrieval
    gate plus the prompt are all that stand between the model and a spoiler.
    Say so rather than implying a guarantee that isn't there."""
    row = con.execute("SELECT 1 FROM entities WHERE work_id=? LIMIT 1", (work_id,)).fetchone()
    return "armed" if row else "prompt-only"


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


SHORT_NAME = 5  # below this a name is matched case-sensitively


def name_pattern(name: str) -> re.Pattern:
    """Does this entity name occur in a piece of text?

    The one definition, used both to block a name in an answer and to date it
    during ingest. If the two ever disagree, a name can be dated from text the
    guard would not have matched, or blocked on text the dating never saw.

    Word boundaries always. Short names are matched case-sensitively: nicknames
    like "Gus" or "Red" are worth blocking, but lowercased they collide with
    ordinary words, and a wrongly refused answer is worse than a rare missed
    nickname.
    """
    flags = 0 if len(name) < SHORT_NAME else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", flags)


def guard_leaks(answer: str, blocklist: list[str]) -> list[str]:
    """Future-entity names appearing in the answer."""
    return [name for name in blocklist if name_pattern(name).search(answer)]


def ep_label(u: dict) -> str:
    return f"S{u['grouping']:02d}E{u['number']:02d}"
