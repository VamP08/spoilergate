"""SpoilerGate API. Run: uvicorn server.app:app"""
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import cache, core, llm

load_dotenv()

app = FastAPI(title="SpoilerGate")

REFUSAL = "I don't know that at your position in the show."


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
        chunks = core.gated_retrieve(con, req.work_id, req.gate_abs, req.question)
        blocklist = core.future_entities(con, req.work_id, req.gate_abs)
        guard = core.guard_state(con, req.work_id)

    if not chunks:
        return {"answer": REFUSAL, "provenance": [], "mode": "gated", "tier": work["tier"]}

    label = core.ep_label(
        max(chunks, key=lambda c: c["abs_order"])
    ) if chunks else ""
    context = "\n\n".join(
        f"[{core.ep_label(c)}] {c['summary_text']}" for c in chunks
    )
    system = (
        f"You answer questions about the TV show {work['title']} for a viewer who has "
        f"watched only up to a certain episode. Use ONLY the episode summaries provided. "
        f"If the answer is not in them, reply exactly: \"{REFUSAL}\" "
        f"Never mention events, characters, or episodes beyond the provided summaries, "
        f"even if you know the show from elsewhere. Do not hint that more happens later."
    )
    answer = llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": f"Summaries:\n{context}\n\nQuestion: {req.question}"},
    ])

    if answer is None:  # no key / provider down -> extractive mode, still gated
        return {
            "answer": "AI is unavailable right now — here are the relevant episode summaries "
                      "from your watched range:\n\n" + context,
            "provenance": [core.ep_label(c) for c in chunks],
            "mode": "extractive",
            "tier": work["tier"],
        }

    leaks = core.guard_leaks(answer, blocklist)
    if leaks:  # one retry with the leak named, then refuse outright
        answer = llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content":
                f"Summaries:\n{context}\n\nQuestion: {req.question}\n\n"
                f"Your previous draft mentioned {', '.join(leaks)} — those must not appear. "
                f"Answer again without them."},
        ])
        if answer is None or core.guard_leaks(answer, blocklist):
            answer = REFUSAL

    return {
        "answer": answer,
        "provenance": [core.ep_label(c) for c in chunks],
        "mode": "gated",
        "tier": work["tier"],
        "guard": guard,
        "gate_label": label,
    }


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

    text = llm.chat([
        {"role": "system", "content":
            f"Write a \"previously on\" recap of {work['title']} for a viewer returning after a "
            f"break. Use ONLY the episode summaries provided — they end exactly where the viewer "
            f"stopped. Never mention or hint at anything beyond them, even if you know the show. "
            f"Lead with the threads left hanging. Under 250 words, present tense, no episode "
            f"numbers, no preamble."},
        {"role": "user", "content": context},
    ], max_tokens=1200)

    if text is None:
        return {"recap": "AI is unavailable right now — here are your watched episodes:\n\n"
                         + context,
                "provenance": provenance, "mode": "extractive"}

    if core.guard_leaks(text, blocklist):
        text = "Couldn't build a spoiler-safe recap for this position. Try the summaries instead."

    cache.put(key, text)
    return {"recap": text, "provenance": provenance, "mode": "gated"}


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

    profile = llm.chat([
        {"role": "system", "content":
            f"Describe {row['name']} from {work['title']} for a viewer who has watched exactly "
            f"the episodes summarised below and no further. Who they are, what they want, where "
            f"they stand now. Use ONLY these summaries — never anything you know about the show "
            f"from elsewhere, and never hint that more is coming. Under 150 words, no preamble."},
        {"role": "user", "content": context},
    ], max_tokens=1000)

    if profile is None:
        return {"name": row["name"], "profile": "AI is unavailable — here is where they appear:"
                                                f"\n\n{context}",
                "provenance": provenance, "mode": "extractive"}

    if core.guard_leaks(profile, blocklist):
        profile = "Couldn't build a spoiler-safe profile at this position."

    cache.put(key, profile)
    return {"name": row["name"], "profile": profile, "provenance": provenance, "mode": "gated"}


web_dir = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
