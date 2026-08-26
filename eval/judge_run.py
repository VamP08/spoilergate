"""Judge recorded answers that the deterministic scorer cannot settle.

Only the ambiguous ones are sent: an answer that refused is safe by
construction, and one the entity scan already flagged is a known leak. What
needs a judgement is an answer that said something without naming anyone from
the future.

Usage: python -m eval.judge_run [run.jsonl ...]
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from eval import judge
from eval.score import RUN_DIR
from server import core
from server.ask import is_refusal


def needs_judging(record: dict) -> bool:
    return (
        record["mode"] != "extractive"
        and not is_refusal(record["raw_answer"])
        and not record["blocked"]
    )


def main() -> None:
    load_dotenv()
    paths = [Path(p) for p in sys.argv[1:]] or sorted(RUN_DIR.glob("*.jsonl"))
    con = core.connect()
    for path in paths:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        work_id = con.execute(
            "SELECT id FROM works WHERE title=?", (records[0]["work"],)).fetchone()[0]
        pending = [r for r in records if needs_judging(r)]
        print(f"{path.stem}: judging {len(pending)} of {len(records)}")

        for i, record in enumerate(pending, 1):
            verdict = judge.judge(con, record, work_id)
            record["judge"] = verdict
            flag = {"leak": "LEAK", "inaccurate": "inacc", "ok": "ok", None: "??"}[
                verdict.get("verdict")]
            print(f"  [{i}/{len(pending)}] {flag:5} {record['kind']:11} "
                  f"gate {record['gate_abs']:>3}  {record['question'][:52]}")
            if verdict.get("verdict") in ("leak", "inaccurate"):
                print(f"        why: {verdict['why']}")

        by_key = {(r["kind"], r["gate_abs"], r["question"]): r for r in pending}
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                judged = by_key.get((record["kind"], record["gate_abs"], record["question"]))
                if judged is not None:
                    record["judge"] = judged["judge"]
                fh.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    main()
