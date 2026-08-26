"""Answering one gated question.

Lives apart from the HTTP layer so the eval harness can measure the real
pipeline instead of a copy of it — a copy would drift from the prompt and the
guard, and then the published number would describe something nobody runs.
`apply_guard=False` records what the model produced before gate 2 removed
anything, which is how the guard's own catch rate gets measured.
"""
import re
import sqlite3

from server import core, llm

REFUSAL = "I don't know that at your position in the show."
FUTURE_REFUSAL = (
    "That asks about what happens after where you are. I can only answer from "
    "what you've already watched."
)

# Questions whose answer lies beyond the gate no matter what was retrieved.
# The eval caught the reason this has to be a rule and not a prompt: asked at
# episode 1 whether Adam Hunt was still around, and at episode 3 whether he
# dies, the model answered "yes" and "no". He is killed in episode 9. Nothing
# could catch it downstream — he appears in the pilot, so he is not a future
# entity and there is no name for the post-guard to block.
#
# The deeper rule: the gated summaries show what HAS happened, never what has
# not. Any answer asserting that someone lives, survives or is fine is
# unsupported by construction, and wrong as often as it is right.
FUTURE_SHAPED = re.compile(
    r"\b(?:"
    r"still (?:alive|around|there|standing)"
    r"|survives?\b|survive the|make[s]? it (?:to the end|out)"
    r"|by the end|in the end|at the end of the (?:show|series|season)"
    r"|end(?:s|ing)? of the (?:show|series)"
    r"|(?:show|series|season)\s+(?:ends?|ending)\b"
    r"|what happens (?:next|later|after)"
    r"|happens? to .{0,30}\b(?:later|eventually|in the end)"
    r"|(?:dies?|died|death|killed) (?:later|next|eventually|in the end)"
    r"|(?:later|eventually) (?:dies?|die|betrays?|leaves?)"
    # "live" is deliberately absent: "where does Jesse live?" is about a house,
    # not a fate, and a false refusal costs more than missing a rare phrasing.
    r"|does .{0,40}\b(?:die|survive|make it)\b"
    r"|who dies|which characters? dies?"
    r"|coming up|upcoming (?:twist|death)|biggest twist"
    r"|spoil(?:er|ers)?\b.{0,20}\b(?:but|is|does)"
    r")",
    re.IGNORECASE,
)


def asks_about_the_future(question: str) -> bool:
    return bool(FUTURE_SHAPED.search(question))


def system_prompt(title: str) -> str:
    return (
        f"You answer questions about the TV show {title} for a viewer who has "
        f"watched only up to a certain episode. Use ONLY the episode summaries provided. "
        f"If the answer is not in them, reply exactly: \"{REFUSAL}\" "
        f"Never mention events, characters, or episodes beyond the provided summaries, "
        f"even if you know the show from elsewhere. Do not hint that more happens later. "
        f"The summaries show what has happened, never what has not: never say that "
        f"someone is safe, alive, unharmed or still around, and never say that something "
        f"did not or will not happen. Say only what the summaries show."
    )


def answer(
    con: sqlite3.Connection, work: sqlite3.Row, gate_abs: int, question: str,
    apply_guard: bool = True,
) -> dict:
    work_id = work["id"]
    guard = core.guard_state(con, work_id)

    # Gate 0, before retrieval spends anything: some questions are about the
    # future by their shape, and no gated context can answer them.
    if asks_about_the_future(question):
        return {"answer": FUTURE_REFUSAL, "provenance": [], "mode": "gated",
                "tier": work["tier"], "guard": guard, "blocked": [], "model": None,
                "refused_by": "shape"}

    chunks = core.gated_retrieve(con, work_id, gate_abs, question)
    blocklist = core.future_entities(con, work_id, gate_abs)
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
    return (
        REFUSAL.lower() in lowered
        or FUTURE_REFUSAL.lower() in lowered
        or lowered.startswith("i don't know")
    )
