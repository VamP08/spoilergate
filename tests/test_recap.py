import sqlite3

import pytest

from ingest.build_db import SCHEMA
from server import cache, core


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO works(id, title, tvmaze_id) VALUES(1, 'Test Show', 99)")
    for i in range(1, 31):
        con.execute(
            "INSERT INTO units(work_id, grouping, number, abs_order, title, summary_text) "
            "VALUES(1, ?, ?, ?, ?, ?)",
            (1 + (i - 1) // 10, i, i, f"Ep {i}", f"Episode {i} happens."),
        )
    return con


def test_recap_window_takes_most_recent_and_stays_gated(con):
    units = core.gated_units(con, 1, 28, limit=25)
    assert len(units) == 25
    assert units[0]["abs_order"] == 4 and units[-1]["abs_order"] == 28  # oldest first
    assert all(u["abs_order"] <= 28 for u in units)


def test_recap_before_the_start_is_empty(con):
    assert core.gated_units(con, 1, 0) == []


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOILERGATE_CACHE", str(tmp_path / "cache.db"))
    assert cache.get("recap:1:5:25") is None
    cache.put("recap:1:5:25", "the recap")
    assert cache.get("recap:1:5:25") == "the recap"
    cache.put("recap:1:5:25", "rewritten")
    assert cache.get("recap:1:5:25") == "rewritten"


def test_search_hides_shows_with_no_summaries(con):
    con.execute("INSERT INTO works(id, title, tvmaze_id, tier) VALUES(2, 'Test Empty', 98, 'empty')")
    titles = [w["title"] for w in core.search_works(con, "Test")]
    assert titles == ["Test Show"]
