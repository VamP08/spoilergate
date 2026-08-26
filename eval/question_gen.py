"""Question sets with free ground truth.

Every entity carries the episode it first appears in, so each one yields two
questions whose correct behaviour is known without anyone labelling anything:

  asked one episode BEFORE it appears  -> the system must refuse
  asked ON the episode it appears      -> the system should answer

That is the gate's precision and recall. Refusing everything scores perfectly
on the first and terribly on the second, which is the point: a gate that never
leaks because it never answers is not a product.

Adversarial questions are separate. They have no positive case — the correct
response is refusal at every position, because the answer lies in the future
by construction.
"""
import sqlite3

ADVERSARIAL = [
    "What happens next?",
    "How does the series end?",
    "Who dies later in the show?",
    "What is the biggest twist coming up?",
    "Tell me something from a later season.",
    "Without spoiling anything, is {name} still around by the end?",
    "Does {name} die?",
    "Who does {name} betray later?",
]


def reveal_questions(con: sqlite3.Connection, work_id: int, limit: int = 40) -> list[dict]:
    """Paired must-refuse / should-answer questions from the entity index.

    Entities appearing in episode 1 are skipped: there is no position before
    them, so they cannot test the gate.
    """
    rows = con.execute(
        "SELECT name, first_appearance_abs FROM entities "
        "WHERE work_id=? AND type='character' AND first_appearance_abs > 1 "
        "ORDER BY first_appearance_abs, name LIMIT ?",
        (work_id, limit),
    ).fetchall()

    questions = []
    for name, reveal in rows:
        question = f"Who is {name}?"
        questions.append({"kind": "before", "question": question,
                          "gate_abs": reveal - 1, "entity": name,
                          "reveal_abs": reveal, "expect": "refuse"})
        questions.append({"kind": "after", "question": question,
                          "gate_abs": reveal, "entity": name,
                          "reveal_abs": reveal, "expect": "answer"})
    return questions


def adversarial_questions(con: sqlite3.Connection, work_id: int,
                          gates: list[int]) -> list[dict]:
    """Bait questions at fixed positions. `{name}` is filled with a character
    the viewer has already met, so the question is answerable-sounding while
    its answer is still entirely in the future."""
    questions = []
    for gate in gates:
        known = con.execute(
            "SELECT name FROM entities WHERE work_id=? AND type='character' "
            "AND first_appearance_abs <= ? ORDER BY first_appearance_abs LIMIT 1",
            (work_id, gate),
        ).fetchone()
        for template in ADVERSARIAL:
            if "{name}" in template:
                if not known:
                    continue
                question = template.format(name=known[0])
            else:
                question = template
            questions.append({"kind": "adversarial", "question": question,
                              "gate_abs": gate, "entity": None,
                              "reveal_abs": None, "expect": "refuse"})
    return questions


def build(con: sqlite3.Connection, work_id: int, limit: int = 40) -> list[dict]:
    reveals = reveal_questions(con, work_id, limit)
    gates = sorted({q["gate_abs"] for q in reveals})
    sampled = gates[:: max(1, len(gates) // 3)][:3] if gates else []
    return reveals + adversarial_questions(con, work_id, sampled)
