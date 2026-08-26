"""Waiting out a per-minute cap.

A web request must fail fast — a viewer will not sit through a minute of
backoff, which is why the server falls straight to extractive mode. A batch job
is the opposite: it has nowhere to be, and giving up turns a rate limit into a
missing measurement. The first judge run lost all 19 verdicts this way, having
started the moment the eval run drained every model's budget.
"""
import time

WAITS = (20, 45, 90)


def retrying(call, unusable):
    """Call until `unusable(result)` is false or the waits run out."""
    for wait in (*WAITS, None):
        result = call()
        if not unusable(result) or wait is None:
            return result
        time.sleep(wait)
    raise AssertionError("unreachable")
