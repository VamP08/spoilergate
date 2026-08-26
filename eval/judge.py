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
VERSION = 2

SYSTEM = (
    "You check an answer about a TV show. You are given the episode summaries a viewer "
    "HAS watched, summaries of episodes they have NOT watched, and the answer they were "
    "given. Reply with JSON only: "
    "{\"verdict\": \"leak\"|\"inaccurate\"|\"ok\", \"why\": \"<one short sentence>\"}.\n"
    "\"leak\": the answer states, implies or hints at something supported ONLY by the "
    "unwatched summaries — what happens later, whether a character survives, that "
    "something notable is coming. This is the serious failure.\n"
    "\"inaccurate\": everything the answer refers to is inside the watched summaries, but "
    "it gets something wrong — contradicts them, or states a rumour or belief reported in "
    "them as established fact. Wrong, but it spoils nothing.\n"
    "\"ok\": the answer is supported by the watched summaries, or it refuses.\n"
    "Answering a question about the future by describing only what has already happened "
    "is \"ok\", not a leak."
)


def _load() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def context_for(con: sqlite3.Connection, work_id: int, gate_abs: int,
                watched: int = 6, ahead: int = 6) -> tuple[str, str]:
    """Summaries just before the gate, and just after it. Windowed to fit the
    per-minute token budget; the episodes nearest the gate are where a leak
    would land anyway."""
    def fetch(sql: str) -> str:
        rows = con.execute(sql, (work_id, gate_abs)).fetchall()
        return "\n\n".join(f"[{core.ep_label(dict(r))}] {r['summary_text']}" for r in rows)

    seen = fetch(
        "SELECT abs_order, grouping, number, summary_text FROM units "
        "WHERE work_id=? AND abs_order <= ? AND summary_text != '' "
        f"ORDER BY abs_order DESC LIMIT {watched}")
    unseen = fetch(
        "SELECT abs_order, grouping, number, summary_text FROM units "
        "WHERE work_id=? AND abs_order > ? AND summary_text != '' "
        f"ORDER BY abs_order LIMIT {ahead}")
    return seen, unseen


def judge(con: sqlite3.Connection, record: dict, work_id: int) -> dict:
    key = hashlib.sha256(
        f"v{VERSION}|{work_id}|{record['gate_abs']}|{record['question']}"
        f"|{record['raw_answer']}".encode()
    ).hexdigest()[:16]
    cache = _load()
    if key in cache:
        return cache[key]

    seen, unseen = context_for(con, work_id, record["gate_abs"])
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"WATCHED:\n{seen}\n\nNOT WATCHED:\n{unseen}\n\n"
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
