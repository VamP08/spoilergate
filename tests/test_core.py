import sqlite3

import pytest

from ingest.build_db import SCHEMA
from ingest.wikipedia import match_to_spine, parse_summaries
from server.core import fts_query, future_entities, gated_retrieve, guard_leaks


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO works(id, title, tvmaze_id) VALUES(1, 'Test Show', 99)")
    eps = [
        (1, 1, 1, 1, "Pilot", "Walter meets Jesse and they cook together."),
        (2, 1, 2, 2, "Two", "Jesse hides the evidence in the desert."),
        (3, 2, 1, 3, "Three", "Gus offers Walter a deal at the restaurant."),
    ]
    for id_, season, num, abs_, title, summary in eps:
        con.execute(
            "INSERT INTO units(id, work_id, grouping, number, abs_order, title, summary_text) "
            "VALUES(?,1,?,?,?,?,?)", (id_, season, num, abs_, title, summary))
        con.execute("INSERT INTO units_fts(rowid, summary_text) VALUES(?,?)", (id_, summary))
    con.execute("INSERT INTO entities(work_id, name, aliases, first_appearance_abs) "
                "VALUES(1, 'Gus', 'Gustavo Fring|the Chicken Man', 3)")
    return con


def test_gate_excludes_future_episodes(con):
    hits = gated_retrieve(con, 1, 2, "Who is Gus?")
    assert all(h["abs_order"] <= 2 for h in hits)
    hits = gated_retrieve(con, 1, 3, "Who is Gus?")
    assert any(h["abs_order"] == 3 for h in hits)


def test_gate_at_zero_returns_nothing(con):
    assert gated_retrieve(con, 1, 0, "Walter desert deal") == []


def test_future_entities_blocklist(con):
    assert "Gus" in future_entities(con, 1, 2)
    assert "Gustavo Fring" in future_entities(con, 1, 2)
    assert future_entities(con, 1, 3) == []


def test_guard_catches_leak_word_boundary():
    block = ["Gus", "Gustavo Fring"]
    assert guard_leaks("Later, Gus appears.", block) == ["Gus"]
    assert guard_leaks("He felt disgusted.", block) == []  # 'gus' inside a word
    assert guard_leaks("GUSTAVO FRING arrives", block) == ["Gustavo Fring"]


def test_fts_query_survives_punctuation():
    q = fts_query('Who is "Gus"? (and why-)')
    assert '"' + "Gus" + '"' in q
    assert "OR" in q
    assert fts_query("!!!") == '""'


WIKITEXT = """
{{Episode list
 | EpisodeNumber = 1
 | Title = Pilot
 | ShortSummary = Walter, a chemistry teacher, starts cooking.
}}
{{Episode list
 | EpisodeNumber = 2
 | Title = Cat's in the Bag...
 | ShortSummary = [[Jesse]] disposes of the body.
}}
"""


def test_parse_summaries_strips_markup():
    rows = parse_summaries(WIKITEXT)
    assert len(rows) == 2
    assert rows[0]["title"] == "Pilot"
    assert rows[1]["summary"] == "Jesse disposes of the body."


def test_match_by_title_when_counts_differ():
    spine = [
        {"grouping": 1, "number": 1, "abs_order": 1, "title": "Pilot", "release_date": ""},
        {"grouping": 1, "number": 2, "abs_order": 2, "title": "Cat's in the Bag...", "release_date": ""},
        {"grouping": 1, "number": 3, "abs_order": 3, "title": "Missing Ep", "release_date": ""},
    ]
    out = match_to_spine(spine, parse_summaries(WIKITEXT))
    assert out[1]["summary"].startswith("Jesse")
    assert out[2]["summary"] == ""
