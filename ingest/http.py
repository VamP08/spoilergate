"""HTTP with a short retry, shared by the ingest sources.

A DNS hiccup partway through a rebuild cost 552 of 832 shows in one run. The
resume logic meant nothing was lost permanently — a rerun retried exactly those
— but an hour was, and on the on-demand endpoint the same blip is an error a
person sees. Three attempts covers a blip without papering over an outage:
if the network is really gone, this still gives up and the caller records the
failure.
"""
import time

import httpx

ATTEMPTS = 3
BACKOFF = (1, 4)


def get(url: str, **kwargs) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            return httpx.get(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.RemoteProtocolError) as e:
            last = e
            if attempt < len(BACKOFF):
                time.sleep(BACKOFF[attempt])
    raise last
