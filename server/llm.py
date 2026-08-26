"""LLM router over stacked free tiers.

Every provider here speaks the OpenAI chat-completions shape, so one request
body works for all of them. Order is the failover order: first provider that
has a key and answers wins. Returns None when they all fail — callers fall
back to extractive mode, so the tool stays usable with no keys at all.

Model names drift as free tiers churn (Groq retired llama-3.3-70b-versatile
under us). Each is overridable by env var without a code change.
"""
import os
import sys

import httpx

PROVIDERS = [
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "llama-3.3-70b"),
    ("gemini", "GEMINI_API_KEY",
     "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
     "meta-llama/llama-3.3-70b-instruct:free"),
]


def _model(name: str, default: str) -> str:
    return os.environ.get(f"SPOILERGATE_MODEL_{name.upper()}", default)


def call_provider(base: str, key: str, model: str, messages: list[dict],
                  max_tokens: int) -> str | None:
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens,
              "temperature": 0.2},
        timeout=60,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    # A reasoning model can spend the whole budget thinking and return "".
    return content.strip() or None


def chat(messages: list[dict], max_tokens: int = 1500) -> str | None:
    """max_tokens has to cover reasoning too: gpt-oss spends most of the budget
    thinking, so a tight cap truncates the answer mid-sentence."""
    for name, env_key, base, default_model in PROVIDERS:
        key = os.environ.get(env_key)
        if not key:
            continue
        try:
            answer = call_provider(base, key, _model(name, default_model),
                                   messages, max_tokens)
            if answer:
                return answer
            print(f"llm: {name} returned empty content", file=sys.stderr)
        except httpx.HTTPStatusError as e:
            print(f"llm: {name} {e.response.status_code} {e.response.text[:200]}",
                  file=sys.stderr)
        except (httpx.HTTPError, KeyError, IndexError) as e:
            print(f"llm: {name} {type(e).__name__}: {e}", file=sys.stderr)
    return None
