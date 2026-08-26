"""Run the question set through the real pipeline and record every answer.

Answers are recorded with the post-guard OFF. That is deliberate: it captures
what the model actually produced, so scoring can report both the leak rate the
system would have without gate 2 and the rate with it. Scoring the guarded
output alone would be circular — the guard removes exactly what the entity scan
looks for, so it would score itself perfect by construction.

Everything lands in JSONL, so scoring and rescoring cost no tokens.

Usage: python -m eval.run "Breaking Bad" [more shows...]
"""
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from eval import question_gen
from server import ask, core

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"

# Free tiers cap tokens per minute, and a batch this size will hit that wall.
# A web request must fail fast, but a batch job should just wait its turn.
RETRY_WAITS = (20, 45, 90)


def answer_with_retry(con, work, gate_abs: int, question: str) -> dict:
    for wait in (*RETRY_WAITS, None):
        result = ask.answer(con, work, gate_abs, question, apply_guard=False)
        if result["mode"] != "extractive":
            return result
        if wait is None:
            return result  # give up; scoring counts it as unusable, not as a pass
        time.sleep(wait)
    raise AssertionError("unreachable")


def run_show(con, title: str, limit: int) -> Path:
    work = con.execute("SELECT * FROM works WHERE title=?", (title,)).fetchone()
    if not work:
        sys.exit(f"unknown show: {title}")
    questions = question_gen.build(con, work["id"], limit)
    print(f"{title}: {len(questions)} questions")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{work['id']}-{title.replace(' ', '_')}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for i, q in enumerate(questions, 1):
            result = answer_with_retry(con, work, q["gate_abs"], q["question"])
            fh.write(json.dumps({
                **q,
                "work": title,
                "raw_answer": result["answer"],
                "blocked": result["blocked"],
                "provenance": result["provenance"],
                "mode": result["mode"],
                "guard": result["guard"],
                "model": result.get("model"),  # the router may fall back under load
            }) + "\n")
            fh.flush()
            if i % 10 == 0:
                print(f"  {i}/{len(questions)}")
    print(f"  wrote {path}")
    return path


def main() -> None:
    load_dotenv()
    shows = sys.argv[1:] or ["Breaking Bad"]
    limit = int(shows.pop()) if shows and shows[-1].isdigit() else 40
    con = core.connect()
    for title in shows:
        run_show(con, title, limit)


if __name__ == "__main__":
    main()
