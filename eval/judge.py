"""Stage two: does an answer state anything the viewer could not know yet?

The entity scan catches a future character by name. It cannot catch a future
character described without one, or an event summarised in the abstract, and
those are the leaks that matter — the first eval run produced an answer reading
"Yes, Hank Schrader is still around", which names nobody from the future and
still tells a viewer at episode 20 something about the finale.

So a model reads the answer against the summaries the viewer HAS seen and the
ones they have not, and says whether anything in the answer depends on the
second set. Verdicts are cached by content hash, so rescoring costs nothing and
only genuinely new answers spend tokens.
"""
import hashlib
import json
import sqlite3
from pathlib import Path

from eval.throttle import retrying
from server import core, llm

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "eval" / "verdicts.json"

# Version the prompt: cached verdicts were produced by a specific rubric, and a
# rubric change has to invalidate them rather than silently mix two standards.
VERSION = 3

SYSTEM = (
    "You check an answer about a TV show for spoilers. You are given SOURCE (the only "
    "episode summaries the answer was allowed to use), AFTER (summaries of episodes the "
    "viewer has NOT reached), and the ANSWER. Reply with JSON only: "
    "{\"verdict\": \"leak\"|\"inaccurate\"|\"ok\", \"why\": \"<one short sentence>\"}.\n"
    "\"leak\": the answer states, implies or hints at something from AFTER — what happens "
    "later, whether someone survives, that something notable is coming. The serious "
    "failure.\n"
    "\"inaccurate\": everything the answer refers to is in SOURCE, but it gets something "
    "wrong — contradicts SOURCE, states a rumour reported in SOURCE as established fact, "
    "or misdescribes when something happened. Wrong, but it spoils nothing.\n"
    "\"ok\": the answer is supported by SOURCE, or it refuses.\n"
    "Judge only against these two sets. Anything you know about the show from elsewhere "
    "is irrelevant. Answering a question about the future by describing only what is in "
    "SOURCE is \"ok\", not a leak."
)


def _load() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def context_for(con: sqlite3.Connection, work_id: int, gate_abs: int, question: str,
                ahead: int = 8) -> tuple[str, str]:
    """What the answer was allowed to use, and what it must not have used.

    The source is the retrieved chunks themselves, rebuilt deterministically —
    the model saw those and nothing else, so anything outside them is
    unsupported by construction. An earlier version handed the judge the six
    most recent watched episodes instead, and it duly reported a leak for an
    answer citing three deaths from episodes the viewer had watched but that
    window did not reach. The judge can only be as right as its evidence.
    """
    chunks = core.gated_retrieve(con, work_id, gate_abs, question)
    source = "\n\n".join(f"[{core.ep_label(c)}] {c['summary_text']}" for c in chunks)
    rows = con.execute(
        "SELECT abs_order, grouping, number, summary_text FROM units "
        "WHERE work_id=? AND abs_order > ? AND summary_text != '' "
        f"ORDER BY abs_order LIMIT {ahead}",
        (work_id, gate_abs),
    ).fetchall()
    after = "\n\n".join(f"[{core.ep_label(dict(r))}] {r['summary_text']}" for r in rows)
    return source, after


def judge(con: sqlite3.Connection, record: dict, work_id: int) -> dict:
    key = hashlib.sha256(
        f"v{VERSION}|{work_id}|{record['gate_abs']}|{record['question']}"
        f"|{record['raw_answer']}".encode()
    ).hexdigest()[:16]
    cache = _load()
    if key in cache:
        return cache[key]

    source, after = context_for(con, work_id, record["gate_abs"], record["question"])
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"SOURCE:\n{source}\n\nAFTER:\n{after}\n\n"
            f"QUESTION: {record['question']}\nANSWER: {record['raw_answer']}"},
    ]
    # A judge run follows a full eval run, so it starts with every model's
    # per-minute budget already spent. Waiting is the whole difference between
    # a verdict and a hole in the results.
    reply = retrying(lambda: llm.chat(messages, max_tokens=800), lambda r: r is None)

    if reply is None:
        return {"verdict": None, "why": "judge unavailable"}
    try:
        text = reply.text[reply.text.index("{"):reply.text.rindex("}") + 1]
        parsed = json.loads(text)
        if parsed["verdict"] not in ("leak", "inaccurate", "ok"):
            raise ValueError(parsed["verdict"])
        verdict = {"verdict": parsed["verdict"], "why": str(parsed.get("why", ""))[:200]}
    except (ValueError, KeyError):
        return {"verdict": None, "why": f"unparseable verdict: {reply.text[:80]}"}

    cache[key] = verdict
    _save(cache)
    return verdict
