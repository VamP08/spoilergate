"""The index endpoint is the one place an anonymous visitor can make us crawl
someone else's API, so the budget is worth a test."""
from server import app


def setup_function():
    app._index_times.clear()


def test_budget_allows_then_refuses():
    assert all(app.take_index_slot() for _ in range(app.INDEX_BUDGET))
    assert not app.take_index_slot()


def test_slots_come_back_once_the_window_passes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])

    for _ in range(app.INDEX_BUDGET):
        assert app.take_index_slot()
    assert not app.take_index_slot()

    clock[0] += app.INDEX_WINDOW + 1
    assert app.take_index_slot()
