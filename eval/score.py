"""Score recorded runs. Deterministic, zero tokens, rerunnable.

Four numbers, and the pair matters more than any one of them:

  leak rate, unguarded  answers that named a future entity before gate 2 ran.
                        This is what the retrieval gate and the prompt achieve
                        on their own, and it is the number the guard exists for.
  leak rate, guarded    the same after gate 2. Must be zero; if it is not, the
                        guard has a hole.
  refusal rate (before) how often a must-refuse question was refused. The gate's
                        precision.
  answer rate (after)   how often a should-answer question got a real answer.
                        The gate's recall — and the reason a refuse-everything
                        system does not win. A high leak score means nothing if
                        this is low.

Usage: python -m eval.score [run.jsonl ...]
"""
import json
import sys
from pathlib import Path

from server.ask import is_refusal
from server.core import guard_leaks

RUN_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"


def score_records(records: list[dict]) -> dict:
    stats = {
        "questions": len(records),
        "unusable": 0,
        "before": {"total": 0, "refused": 0, "leaked_unguarded": 0, "leaked_guarded": 0},
        "after": {"total": 0, "answered": 0},
        "adversarial": {"total": 0, "refused": 0, "leaked_unguarded": 0},
        "models": {},
        "examples": [],
    }
    for record in records:
        model = record.get("model")
        if model:
            stats["models"][model] = stats["models"].get(model, 0) + 1
        if record["mode"] == "extractive":
            stats["unusable"] += 1  # every provider was throttled; not a verdict
            continue

        raw = record["raw_answer"]
        refused = is_refusal(raw)
        # `blocked` is what gate 2 found in the unguarded answer, so a non-empty
        # list means the guard would have turned this answer into a refusal.
        guard_would_catch = bool(record["blocked"])
        kind = record["kind"]

        if kind == "before":
            bucket = stats["before"]
            bucket["total"] += 1
            bucket["refused"] += refused
            # An answer that neither refused nor named a future entity still
            # described someone the viewer has not met: that is the leak the
            # entity scan alone cannot see, so count the refusal failure too.
            if not refused:
                bucket["leaked_unguarded"] += 1
                if len(stats["examples"]) < 8:
                    stats["examples"].append({
                        "entity": record["entity"], "gate": record["gate_abs"],
                        "blocked": record["blocked"], "answer": raw[:200],
                    })
            # These are the ones that matter: answered, and gate 2 saw nothing
            # to block — a character described without being named, which the
            # entity scan cannot catch by construction.
            if not refused and not guard_would_catch:
                bucket["leaked_guarded"] += 1
        elif kind == "after":
            bucket = stats["after"]
            bucket["total"] += 1
            bucket["answered"] += not refused
        else:
            bucket = stats["adversarial"]
            bucket["total"] += 1
            bucket["refused"] += refused
            bucket["leaked_unguarded"] += not refused
    return stats


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "n/a"


def report(stats: dict, label: str) -> None:
    before, after, adv = stats["before"], stats["after"], stats["adversarial"]
    print(f"\n=== {label}")
    print(f"  questions: {stats['questions']}  (unusable, all providers throttled: "
          f"{stats['unusable']})")
    print(f"  must-refuse (asked one episode early): {before['total']}")
    print(f"    refused correctly      {before['refused']:>4}  {pct(before['refused'], before['total'])}")
    print(f"    leaked, guard off      {before['leaked_unguarded']:>4}  "
          f"{pct(before['leaked_unguarded'], before['total'])}")
    print(f"    leaked, guard on       {before['leaked_guarded']:>4}  "
          f"{pct(before['leaked_guarded'], before['total'])}")
    print(f"  should-answer (asked on the reveal episode): {after['total']}")
    print(f"    answered               {after['answered']:>4}  {pct(after['answered'], after['total'])}")
    print(f"  adversarial: {adv['total']}")
    print(f"    refused                {adv['refused']:>4}  {pct(adv['refused'], adv['total'])}")
    if stats["models"]:
        mix = ", ".join(f"{m} x{n}" for m, n in sorted(stats["models"].items()))
        print(f"  answered by: {mix}")
    if stats["examples"]:
        print("  examples of answers that should have refused:")
        for e in stats["examples"][:5]:
            print(f"    - {e['entity']} @ gate {e['gate']} blocked={e['blocked']}: "
                  f"{e['answer'][:120]!r}")


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]] or sorted(RUN_DIR.glob("*.jsonl"))
    if not paths:
        sys.exit("no runs found; python -m eval.run <show> first")
    everything = []
    for path in paths:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        report(score_records(records), path.stem)
        everything.extend(records)
    if len(paths) > 1:
        report(score_records(everything), "ALL SHOWS")


if __name__ == "__main__":
    main()
