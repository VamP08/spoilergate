import sqlite3

import pytest

from eval.question_gen import build, reveal_questions
from eval.score import score_records
from ingest.build_db import SCHEMA


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO works(id, title, tvmaze_id) VALUES(1, 'Test Show', 99)")
    rows = [
        ("Walter White", 1),      # episode 1: nothing before it to test
        ("Tuco Salamanca", 6),
        ("Jane Margolis", 17),
        ("Los Pollos", 22),
    ]
    for name, reveal in rows:
        con.execute(
            "INSERT INTO entities(work_id, name, type, first_appearance_abs) "
            "VALUES(1, ?, 'character', ?)", (name, reveal))
    return con


def test_reveal_questions_pair_each_entity_around_its_reveal(con):
    qs = reveal_questions(con, 1)
    tuco = [q for q in qs if q["entity"] == "Tuco Salamanca"]
    assert len(tuco) == 2
    before, after = sorted(tuco, key=lambda q: q["gate_abs"])
    assert (before["gate_abs"], before["expect"]) == (5, "refuse")
    assert (after["gate_abs"], after["expect"]) == (6, "answer")


def test_episode_one_entities_cannot_test_the_gate(con):
    assert not [q for q in reveal_questions(con, 1) if q["entity"] == "Walter White"]


def test_adversarial_questions_are_always_must_refuse(con):
    adv = [q for q in build(con, 1) if q["kind"] == "adversarial"]
    assert adv
    assert all(q["expect"] == "refuse" for q in adv)
    # the templated ones name a character the viewer has already met
    named = [q for q in adv if "Walter White" in q["question"]]
    assert named


def test_retrieval_recall_finds_the_reveal_episode(con):
    from eval.retrieval import recall_for_show

    for abs_order, text in [
        (1, "Walter White cooks in the desert."),
        (6, "Tuco Salamanca beats Jesse and steals the product."),
        (17, "Jane Margolis moves in next door."),
    ]:
        con.execute(
            "INSERT INTO units(work_id, grouping, number, abs_order, title, summary_text) "
            "VALUES(1, 1, ?, ?, '', ?)", (abs_order, abs_order, text))
        con.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)",
                    (con.execute("SELECT last_insert_rowid()").fetchone()[0], text))

    result = recall_for_show(con, 1)
    assert result["total"] == 3  # the episode-1 entity is skipped, but Los Pollos is not
    assert result["hits"] >= 2
    names = {name for name, _, _ in result["misses"]}
    assert "Tuco Salamanca" not in names and "Jane Margolis" not in names


def test_retrying_waits_out_a_throttle(monkeypatch):
    from eval import throttle

    monkeypatch.setattr(throttle.time, "sleep", lambda _: None)
    results = iter([None, None, "answer"])
    got = throttle.retrying(lambda: next(results), lambda r: r is None)
    assert got == "answer"


def test_retrying_gives_up_rather_than_looping(monkeypatch):
    from eval import throttle

    monkeypatch.setattr(throttle.time, "sleep", lambda _: None)
    calls = []
    got = throttle.retrying(lambda: calls.append(1) or None, lambda r: r is None)
    assert got is None
    assert len(calls) == len(throttle.WAITS) + 1  # one attempt per wait, plus the first


REFUSAL_TEXT = "I don't know that at your position in the show."


def record(kind, answer, blocked=(), mode="gated"):
    return {"kind": kind, "raw_answer": answer, "blocked": list(blocked),
            "mode": mode, "entity": "Gus Fring", "gate_abs": 10,
            "question": "Who is Gus Fring?"}


def test_refusal_before_reveal_is_not_a_leak():
    stats = score_records([record("before", REFUSAL_TEXT)])
    assert stats["before"]["refused"] == 1
    assert stats["before"]["leaked_unguarded"] == 0
    assert stats["before"]["leaked_guarded"] == 0


def test_named_leak_is_caught_by_the_guard_but_still_counted_unguarded():
    stats = score_records([record("before", "Gus Fring runs a restaurant.", ["Gus Fring"])])
    assert stats["before"]["leaked_unguarded"] == 1
    assert stats["before"]["leaked_guarded"] == 0  # gate 2 would have refused it


def test_unnamed_leak_slips_past_the_guard():
    """The honest failure mode: the character is described, never named."""
    stats = score_records([record("before", "He runs a fast-food chain and sells meth.")])
    assert stats["before"]["leaked_unguarded"] == 1
    assert stats["before"]["leaked_guarded"] == 1


def test_answer_after_reveal_counts_as_recall():
    stats = score_records([record("after", "Gus Fring runs Los Pollos Hermanos.")])
    assert stats["after"]["answered"] == 1
    stats = score_records([record("after", REFUSAL_TEXT)])
    assert stats["after"]["answered"] == 0


def test_judge_separates_a_leak_from_a_mere_inaccuracy():
    """Stating an in-universe rumour as fact is wrong but spoils nothing, and
    counting it as a leak would overstate the number that matters."""
    leak = record("adversarial", "He does not survive the finale.")
    leak["judge"] = {"verdict": "leak", "why": "depends on unwatched episodes"}
    wrong = record("adversarial", "Jesse kills Spooge.")
    wrong["judge"] = {"verdict": "inaccurate", "why": "the summaries say otherwise"}
    fine = record("adversarial", "So far Walt has been cooking with Jesse.")
    fine["judge"] = {"verdict": "ok", "why": "supported"}

    stats = score_records([leak, wrong, fine])
    assert stats["judged"] == {"total": 3, "leaks": 1, "inaccurates": 1, "unparsed": 0}


def test_unreadable_verdicts_are_not_counted_as_clean():
    rec = record("adversarial", "something")
    rec["judge"] = {"verdict": None, "why": "judge unavailable"}
    stats = score_records([rec])
    assert stats["judged"]["unparsed"] == 1
    assert stats["judged"]["leaks"] == 0


def test_throttled_runs_are_unusable_not_verdicts():
    stats = score_records([record("before", "AI is unavailable", mode="extractive")])
    assert stats["unusable"] == 1
    assert stats["before"]["total"] == 0
