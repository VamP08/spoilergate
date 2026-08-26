import httpx
import pytest

from server import llm


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for _, env_key, _, _ in llm.PROVIDERS:
        monkeypatch.delenv(env_key, raising=False)


def fake_calls(monkeypatch, behaviour: dict[str, object]):
    """behaviour: substring of the model name -> str to return, or Exception to
    raise. Dispatching on model, not base URL, because several rows share a base:
    Groq's per-model TPM budgets are the whole reason it appears more than once.
    """
    seen = []

    def call(base, key, model, messages, max_tokens):
        seen.append(model)
        for marker, result in behaviour.items():
            if marker in model:
                if isinstance(result, Exception):
                    raise result
                return result
        return None

    monkeypatch.setattr(llm, "call_provider", call)
    return seen


def rate_limited(base: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "429", request=httpx.Request("POST", base),
        response=httpx.Response(429, text="rate limit reached"))


def test_no_keys_returns_none(monkeypatch):
    seen = fake_calls(monkeypatch, {})
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert seen == []  # never touches the network without a key


def test_first_model_wins_and_reply_names_it(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = fake_calls(monkeypatch, {"gpt-oss-120b": "from the big one",
                                    "gemini": "from gemini"})
    reply = llm.chat([])
    assert reply.text == "from the big one"
    assert reply.model == "openai/gpt-oss-120b"
    assert len(seen) == 1  # no pointless second call


def test_groq_models_are_separate_budgets(monkeypatch):
    """A 429 is per model, so the next Groq row is a fresh budget on one key."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    seen = fake_calls(monkeypatch, {"gpt-oss-120b": rate_limited(llm.GROQ),
                                    "qwen": "from the fallback"})
    reply = llm.chat([])
    assert reply.text == "from the fallback"
    assert seen[0] == "openai/gpt-oss-120b"
    assert reply.model.startswith("qwen")


def test_failover_past_error_and_empty(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = fake_calls(monkeypatch, {
        "gpt-oss-120b": httpx.ConnectError("down"),
        "qwen": None,        # empty content: reasoning burned the token budget
        "gpt-oss-20b": None,
        "gemini": "from gemini",
    })
    assert llm.chat([]).text == "from gemini"
    assert len(seen) == 4


def test_all_providers_failing_returns_none(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    fake_calls(monkeypatch, {"": httpx.ReadTimeout("slow")})
    assert llm.chat([]) is None


def test_model_override_by_env(monkeypatch):
    monkeypatch.setenv("SPOILERGATE_MODEL_GROQ_QWEN", "my-model")
    assert llm._model("groq_qwen", "default") == "my-model"
    assert llm._model("groq", "default") == "default"  # rows override independently
