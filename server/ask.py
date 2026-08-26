"""Answering one gated question.

Lives apart from the HTTP layer so the eval harness can measure the real
pipeline instead of a copy of it — a copy would drift from the prompt and the
guard, and then the published number would describe something nobody runs.
`apply_guard=False` records what the model produced before gate 2 removed
anything, which is how the guard's own catch rate gets measured.
"""
import sqlite3

from server import core, llm

REFUSAL = "I don't know that at your position in the show."


def system_prompt(title: str) -> str:
    return (
        f"You answer questions about the TV show {title} for a viewer who has "
        f"watched only up to a certain episode. Use ONLY the episode summaries provided. "
        f"If the answer is not in them, reply exactly: \"{REFUSAL}\" "
        f"Never mention events, characters, or episodes beyond the provided summaries, "
        f"even if you know the show from elsewhere. Do not hint that more happens later."
    )


def answer(
    con: sqlite3.Connection, work: sqlite3.Row, gate_abs: int, question: str,
    apply_guard: bool = True,
) -> dict:
    work_id = work["id"]
    chunks = core.gated_retrieve(con, work_id, gate_abs, question)
    blocklist = core.future_entities(con, work_id, gate_abs)
    guard = core.guard_state(con, work_id)
    provenance = [core.ep_label(c) for c in chunks]

    if not chunks:
        return {"answer": REFUSAL, "provenance": [], "mode": "gated",
                "tier": work["tier"], "guard": guard, "blocked": []}

    context = "\n\n".join(f"[{core.ep_label(c)}] {c['summary_text']}" for c in chunks)
    system = system_prompt(work["title"])
    reply = llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": f"Summaries:\n{context}\n\nQuestion: {question}"},
    ])

    if reply is None:  # no key / every provider down -> extractive, still gated
        return {
            "answer": "AI is unavailable right now — here are the relevant episode summaries "
                      "from your watched range:\n\n" + context,
            "provenance": provenance, "mode": "extractive", "tier": work["tier"],
            "guard": guard, "blocked": [], "model": None,
        }

    text, model = reply
    leaks = core.guard_leaks(text, blocklist)
    if leaks and apply_guard:  # one retry naming the leak, then refuse outright
        retry = llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content":
                f"Summaries:\n{context}\n\nQuestion: {question}\n\n"
                f"Your previous draft mentioned {', '.join(leaks)} — those must not appear. "
                f"Answer again without them."},
        ])
        if retry is None or core.guard_leaks(retry.text, blocklist):
            text = REFUSAL
        else:
            text, model = retry

    return {
        "answer": text, "provenance": provenance, "mode": "gated",
        "tier": work["tier"], "guard": guard, "model": model,
        "blocked": leaks,  # what gate 2 saw, whether or not it acted
        "gate_label": core.ep_label(max(chunks, key=lambda c: c["abs_order"])),
    }


def is_refusal(text: str) -> bool:
    """The model paraphrases the refusal as often as it copies it."""
    lowered = text.lower()
    return REFUSAL.lower() in lowered or lowered.startswith("i don't know")
