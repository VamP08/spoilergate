"""Single-provider LLM call (Groq, OpenAI-compatible). Router with failover lands in M2.

Returns None when no key or the call fails — callers fall back to extractive mode,
so the tool stays usable with zero LLM.
"""
import os
import sys

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ.get("SPOILERGATE_MODEL", "openai/gpt-oss-120b")


def chat(messages: list[dict], max_tokens: int = 600) -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": MODEL, "messages": messages, "max_tokens": max_tokens,
                  "temperature": 0.2},
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content.strip() or None  # reasoning models can burn max_tokens and return ""
    except httpx.HTTPStatusError as e:
        print(f"llm: {e.response.status_code} {e.response.text[:200]}", file=sys.stderr)
        return None
    except (httpx.HTTPError, KeyError, IndexError) as e:
        print(f"llm: {type(e).__name__}: {e}", file=sys.stderr)
        return None
