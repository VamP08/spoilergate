"""SpoilerGate API. Run: uvicorn server.app:app"""
import sys
import time
import traceback
from pathlib import Path

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import ask as ask_module
from server import cache, core, durable, llm

load_dotenv()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Replay shows indexed in earlier sessions before serving anything.

    The filesystem is wiped whenever a free instance sleeps, so without this a
    show someone indexed yesterday is simply gone. Failure is not fatal — the
    baked index still answers everything it always did.
    """
    if durable.enabled():
        con = core.connect()
        try:
            durable.restore(con)
        finally:
            con.close()
    yield


app = FastAPI(title="SpoilerGate", lifespan=lifespan)


class AskRequest(BaseModel):
    work_id: int
    gate_abs: int  # abs_order of the last episode the viewer finished
    question: str


class RecapRequest(BaseModel):
    work_id: int
    gate_abs: int


@app.get("/api/works")
def works(q: str = ""):
    with core.connect() as con:
        return core.search_works(con, q)


@app.get("/api/works/{work_id}")
def work(work_id: int):
    with core.connect() as con:
        detail = core.work_detail(con, work_id)
    if detail is None or not detail["units"]:
        raise HTTPException(404, "unknown work")
    return detail


@app.get("/api/works/{work_id}/units")
def units(work_id: int):
    with core.connect() as con:
        result = core.list_units(con, work_id)
    if not result:
        raise HTTPException(404, "unknown work")
    return result


@app.post("/api/ask")
def ask(req: AskRequest):
    with core.connect() as con:
        work = con.execute("SELECT * FROM works WHERE id=?", (req.work_id,)).fetchone()
        if not work:
            raise HTTPException(404, "unknown work")
        result = ask_module.answer(con, work, req.gate_abs, req.question)
    result.pop("blocked", None)  # internal to the guard; not part of the API
    return result


# Groq's free tier caps tokens-per-minute at 8000 and counts max_tokens against
# it, so context size is a hard budget, not a preference: ~12 summaries (~3.5k)
# plus a 1.2k answer fits with headroom.
# ponytail: a recap therefore covers the last 12 episodes, not the whole arc.
# Fix by summarising per season offline once the deep tier exists (M3).
RECAP_WINDOW = 12


@app.post("/api/recap")
def recap(req: RecapRequest):
    with core.connect() as con:
        work = con.execute("SELECT * FROM works WHERE id=?", (req.work_id,)).fetchone()
        if not work:
            raise HTTPException(404, "unknown work")
        units = core.gated_units(con, req.work_id, req.gate_abs, RECAP_WINDOW)
        blocklist = core.future_entities(con, req.work_id, req.gate_abs)

    if not units:
        return {"recap": "You haven't started this show yet — nothing to recap.",
                "provenance": [], "mode": "gated"}

    provenance = [core.ep_label(u) for u in units]
    context = "\n\n".join(f"[{core.ep_label(u)}] {u['summary_text']}" for u in units)
    key = f"recap:{req.work_id}:{req.gate_abs}:{RECAP_WINDOW}"

    cached = cache.get(key)
    if cached:
        return {"recap": cached, "provenance": provenance, "mode": "cached"}

    reply = llm.chat([
        {"role": "system", "content":
            f"Write a \"previously on\" recap of {work['title']} for a viewer returning after a "
            f"break. Use ONLY the episode summaries provided — they end exactly where the viewer "
            f"stopped. Never mention or hint at anything beyond them, even if you know the show. "
            f"Lead with the threads left hanging. Under 250 words, present tense, no episode "
            f"numbers, no preamble."},
        {"role": "user", "content": context},
    ], max_tokens=1200)

    if reply is None:
        return {"recap": "AI is unavailable right now — here are your watched episodes:\n\n"
                         + context,
                "provenance": provenance, "mode": "extractive"}

    text = reply.text
    if core.guard_leaks(text, blocklist):
        text = "Couldn't build a spoiler-safe recap for this position. Try the summaries instead."

    cache.put(key, text)
    return {"recap": text, "provenance": provenance, "mode": "gated"}


class IndexRequest(BaseModel):
    query: str


# /api/index is the one endpoint an anonymous visitor can use to make us crawl
# somebody else's API. Left open, a bored visitor gets our IP rate-limited by
# TVMaze or Wikipedia, which breaks indexing for everyone. A whole-process cap
# is crude but it bounds the damage.
# ponytail: per-process and in-memory, so it resets on restart and does not
# span instances. Enough while there is one free instance.
INDEX_BUDGET = 20
INDEX_WINDOW = 3600
_index_times: list[float] = []


def take_index_slot() -> bool:
    now = time.monotonic()
    _index_times[:] = [t for t in _index_times if now - t < INDEX_WINDOW]
    if len(_index_times) >= INDEX_BUDGET:
        return False
    _index_times.append(now)
    return True


@app.post("/api/index")
def index_show(req: IndexRequest):
    """Index a show nobody has asked for yet — the long tail of Layer 2.

    Same pipeline as the offline ingest, so a show indexed here is gated and
    entity-indexed exactly like a precomputed one; there is no second, weaker
    path. The first person to ask waits; everyone after hits the database.

    Writes go to the running index and, when DATABASE_URL is set, to Postgres,
    which is what carries them across a free instance's sleep. With no database
    configured the show still works — until the filesystem is wiped.
    """
    from ingest import build_db, tvmaze

    query = req.query.strip()
    if len(query) < 2:
        raise HTTPException(400, "give me a show name")

    with core.connect() as con:
        existing = core.search_works(con, query)
    if existing:
        return {"status": "already indexed", **existing[0]}

    if not take_index_slot():
        raise HTTPException(
            429, "too many shows indexed in the last hour — try again later")

    con = build_db.connect()
    try:
        show = tvmaze.fetch_show(query)
    except Exception as e:                                        # noqa: BLE001
        # Logged, not just returned: a 404 body reaches one visitor, whereas the
        # logs are the only place anyone can see WHY indexing keeps failing.
        print(f"index: tvmaze had nothing for {query!r} ({type(e).__name__}: {e})",
              file=sys.stderr)
        raise HTTPException(404, "no show by that name")

    print(f"index: {show['name']!r} (tvmaze {show['id']})", file=sys.stderr)
    try:
        build_db.ingest_show(con, show)
    except Exception as e:                                        # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(502, f"could not index that show: {type(e).__name__}")

    row = con.execute(
        "SELECT id, title, media_type, tier FROM works WHERE tvmaze_id=?", (show["id"],)
    ).fetchone()
    if row is None or row["tier"] == "empty":
        con.close()
        print(f"index: no Wikipedia episode summaries for {show['name']!r}",
              file=sys.stderr)
        raise HTTPException(
            404, "found the show, but there are no episode summaries to answer from")

    # Keep it beyond this instance's life, so the next visitor after a sleep
    # does not pay to index the same show again.
    kept = durable.remember(con, row["id"])
    con.close()
    print(f"index: stored {row['title']!r} (kept={kept})", file=sys.stderr)
    return {"status": "indexed", "kept": kept, **dict(row)}


class CharacterRequest(BaseModel):
    work_id: int
    gate_abs: int
    entity_id: int


@app.get("/api/works/{work_id}/characters")
def characters(work_id: int, gate_abs: int):
    with core.connect() as con:
        return core.gated_entities(con, work_id, gate_abs)


@app.post("/api/character")
def character(req: CharacterRequest):
    with core.connect() as con:
        work = con.execute("SELECT * FROM works WHERE id=?", (req.work_id,)).fetchone()
        row = con.execute(
            "SELECT name, aliases, first_appearance_abs FROM entities WHERE id=? AND work_id=?",
            (req.entity_id, req.work_id),
        ).fetchone()
        # Meta-spoiler rule: a character the viewer has not met is answered
        # exactly like one that does not exist.
        if not work or not row or row["first_appearance_abs"] > req.gate_abs:
            raise HTTPException(404, "unknown character")
        units = core.character_units(
            con, req.work_id, row["name"], row["aliases"], req.gate_abs
        )
        blocklist = core.future_entities(con, req.work_id, req.gate_abs)

    provenance = [core.ep_label(u) for u in units]
    context = "\n\n".join(f"[{core.ep_label(u)}] {u['summary_text']}" for u in units)
    key = f"char:{req.entity_id}:{req.gate_abs}"

    cached = cache.get(key)
    if cached:
        return {"name": row["name"], "profile": cached, "provenance": provenance,
                "mode": "cached"}

    reply = llm.chat([
        {"role": "system", "content":
            f"Describe {row['name']} from {work['title']} for a viewer who has watched exactly "
            f"the episodes summarised below and no further. Who they are, what they want, where "
            f"they stand now. Use ONLY these summaries — never anything you know about the show "
            f"from elsewhere, and never hint that more is coming. Under 150 words, no preamble."},
        {"role": "user", "content": context},
    ], max_tokens=1000)

    if reply is None:
        return {"name": row["name"], "profile": "AI is unavailable — here is where they appear:"
                                                f"\n\n{context}",
                "provenance": provenance, "mode": "extractive"}

    profile = reply.text
    if core.guard_leaks(profile, blocklist):
        profile = "Couldn't build a spoiler-safe profile at this position."

    cache.put(key, profile)
    return {"name": row["name"], "profile": profile, "provenance": provenance, "mode": "gated"}


web_dir = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
