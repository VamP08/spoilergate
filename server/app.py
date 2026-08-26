"""SpoilerGate API. Run: uvicorn server.app:app"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server import core, llm

app = FastAPI(title="SpoilerGate")

REFUSAL = "I don't know that at your position in the show."


class AskRequest(BaseModel):
    work_id: int
    gate_abs: int  # abs_order of the last episode the viewer finished
    question: str


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
        "gate_label": label,
    }


web_dir = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
