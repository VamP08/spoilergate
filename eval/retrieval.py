"""Does retrieval return the episode that actually answers the question?

Separate from the leak numbers, and free: no model is involved, so this can run
against every indexed show as often as it likes.

It exists because of a case the leak eval could not see. Asked at episode 15
what the biggest twist was, the answer said "Jesse kills Spooge" — which is a
rumour reported in episode 14, contradicted by episode 13, where Spooge's
girlfriend kills him. Episode 13 was inside the gate and was never retrieved,
so neither the answering model nor the judge ever saw the thing that would have
corrected it. The gate was not at fault; recall was.

The check: ask "Who is X?" with the gate set to the episode X first appears in.
That episode is the one that answers the question, by construction. If it is
not in the retrieved chunks, the model is being asked to answer from the wrong
material.

Usage: python -m eval.retrieval [show ...]
"""
import sys

from server import core


def recall_for_show(con, work_id: int, limit: int = 60) -> dict:
    rows = con.execute(
        "SELECT name, first_appearance_abs FROM entities "
        "WHERE work_id=? AND type='character' AND first_appearance_abs > 1 "
        "ORDER BY first_appearance_abs LIMIT ?",
        (work_id, limit),
    ).fetchall()

    hits, misses, ranks = 0, [], []
    for name, reveal in rows:
        chunks = core.gated_retrieve(con, work_id, reveal, f"Who is {name}?")
        orders = [c["abs_order"] for c in chunks]
        if reveal in orders:
            hits += 1
            ranks.append(orders.index(reveal) + 1)
        else:
            misses.append((name, reveal, orders))
    return {"total": len(rows), "hits": hits, "misses": misses, "ranks": ranks}


def report(title: str, result: dict) -> None:
    total, hits = result["total"], result["hits"]
    if not total:
        print(f"{title}: no datable characters")
        return
    ranks = result["ranks"]
    mean_rank = sum(ranks) / len(ranks) if ranks else 0
    print(f"{title}: reveal episode retrieved {hits}/{total} "
          f"({100 * hits / total:.0f}%), mean rank {mean_rank:.1f}")
    for name, reveal, orders in result["misses"][:5]:
        print(f"    miss: {name!r} reveal {reveal}, got {orders}")


def main() -> None:
    con = core.connect()
    titles = sys.argv[1:] or ["Breaking Bad", "Game of Thrones", "Arrow"]
    for title in titles:
        row = con.execute("SELECT id FROM works WHERE title=?", (title,)).fetchone()
        if not row:
            print(f"{title}: not indexed")
            continue
        report(title, recall_for_show(con, row["id"]))


if __name__ == "__main__":
    main()
