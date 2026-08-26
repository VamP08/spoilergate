import httpx
import pytest

from ingest import http


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _: None)


def test_retries_past_a_blip(monkeypatch):
    calls = []

    def flaky(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectError("getaddrinfo failed")
        return "ok"

    monkeypatch.setattr(http.httpx, "get", flaky)
    assert http.get("https://example.test") == "ok"
    assert len(calls) == 3


def test_gives_up_rather_than_hiding_an_outage(monkeypatch):
    calls = []

    def always_down(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(http.httpx, "get", always_down)
    with pytest.raises(httpx.ConnectError):
        http.get("https://example.test")
    assert len(calls) == http.ATTEMPTS


def test_an_http_error_is_not_retried(monkeypatch):
    """A 404 is an answer, not a blip — retrying it just wastes time."""
    calls = []

    def not_found(url, **kwargs):
        calls.append(url)
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(http.httpx, "get", not_found)
    assert http.get("https://example.test").status_code == 404
    assert len(calls) == 1
