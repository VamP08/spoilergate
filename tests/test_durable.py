"""The export/replay round trip, tested without a Postgres anywhere near it.

Postgres only stores the JSON and hands it back; the part that can actually be
wrong is turning a show into that JSON and rebuilding it in a fresh index.
"""
import sqlite3

import pytest

from ingest.build_db import SCHEMA
from server import core, durable


def make_index() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@pytest.fixture
def source():
    con = make_index()
    con.execute("INSERT INTO works(id, title, tvmaze_id, wikipedia_page, tier) "
                "VALUES(1, 'This Country', 4242, 'This Country', 'shallow')")
    for i, text in enumerate(["Kerry and Kurtan celebrate.", "Mandy returns."], start=1):
        con.execute(
            "INSERT INTO units(work_id, grouping, number, abs_order, title, "
            "release_date, summary_text, source_url) VALUES(1,1,?,?,?,'2017',?,'u')",
            (i, i, f"Episode {i}", text))
        con.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)",
                    (con.execute("SELECT last_insert_rowid()").fetchone()[0], text))
    con.execute("INSERT INTO entities(work_id, name, aliases, type, first_appearance_abs) "
                "VALUES(1, 'Mandy', 'Mandy Harris', 'character', 2)")
    con.execute("INSERT INTO artwork(work_id, poster_url, network, premiered, genres, "
                "rating, tvmaze_url) VALUES(1, 'https://img/p.jpg', 'BBC', '2017', "
                "'Comedy', 8.1, 'https://tvmaze/x')")
    con.commit()
    return con


def test_round_trip_rebuilds_the_show(source):
    payload = durable.export_show(source, 1)
    fresh = make_index()
    durable.apply_show(fresh, payload)

    work = fresh.execute("SELECT * FROM works WHERE tvmaze_id=4242").fetchone()
    assert work["title"] == "This Country"
    assert work["tier"] == "shallow"
    units = fresh.execute("SELECT * FROM units ORDER BY abs_order").fetchall()
    assert [u["summary_text"] for u in units] == [
        "Kerry and Kurtan celebrate.", "Mandy returns."]
    ent = fresh.execute("SELECT * FROM entities").fetchone()
    assert (ent["name"], ent["first_appearance_abs"]) == ("Mandy", 2)
    assert fresh.execute("SELECT poster_url FROM artwork").fetchone()[0] == "https://img/p.jpg"


def test_replayed_show_is_searchable_and_gated(source):
    """The rebuilt show has to work like any other, FTS index included —
    without it the show exists and answers nothing."""
    fresh = make_index()
    durable.apply_show(fresh, durable.export_show(source, 1))
    work_id = fresh.execute("SELECT id FROM works").fetchone()[0]

    assert core.search_works(fresh, "This Country")
    # Mandy turns up in episode 2, so the replayed show has to hide her at 1 and
    # find her at 2 — if the FTS rows were lost, both would come back empty.
    assert core.gated_retrieve(fresh, work_id, 1, "Mandy") == []
    assert [u["abs_order"] for u in core.gated_retrieve(fresh, work_id, 2, "Mandy")] == [2]
    assert core.future_entities(fresh, work_id, 1) == ["Mandy", "Mandy Harris"]


def test_replay_is_idempotent(source):
    fresh = make_index()
    payload = durable.export_show(source, 1)
    durable.apply_show(fresh, payload)
    durable.apply_show(fresh, payload)
    assert fresh.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
    assert fresh.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 2
    assert fresh.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1


def test_disabled_without_a_database_url(monkeypatch, source):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not durable.enabled()
    assert durable.remember(source, 1) is False   # no store, no crash
    assert durable.restore(source) == 0


def test_a_broken_database_url_does_not_take_the_app_down(monkeypatch, source):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert durable.enabled()
    assert durable.remember(source, 1) is False
    assert durable.restore(source) == 0
