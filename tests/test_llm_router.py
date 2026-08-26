import httpx
import pytest

from server import llm


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for _, env_key, _, _ in llm.PROVIDERS:
        monkeypatch.delenv(env_key, raising=False)


def fake_calls(monkeypatch, behaviour: dict[str, object]):
    """behaviour: base_url substring -> str to return, or Exception to raise."""
    seen = []

    def call(base, key, model, messages, max_tokens):
        seen.append(base)
        for marker, result in behaviour.items():
            if marker in base:
                if isinstance(result, Exception):
                    raise result
                return result
        return None

    monkeypatch.setattr(llm, "call_provider", call)
    return seen


def test_no_keys_returns_none(monkeypatch):
    seen = fake_calls(monkeypatch, {})
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert seen == []  # never touches the network without a key


def test_first_keyed_provider_wins(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = fake_calls(monkeypatch, {"groq": "from groq", "googleapis": "from gemini"})
    assert llm.chat([]) == "from groq"
    assert len(seen) == 1  # no pointless second call


def test_failover_past_error_and_empty(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("CEREBRAS_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = fake_calls(monkeypatch, {
        "groq": httpx.ConnectError("down"),
        "cerebras": None,  # empty content, e.g. reasoning burned the token budget
        "googleapis": "from gemini",
    })
    assert llm.chat([]) == "from gemini"
    assert len(seen) == 3


def test_all_providers_failing_returns_none(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    fake_calls(monkeypatch, {"groq": httpx.ReadTimeout("slow")})
    assert llm.chat([]) is None


def test_model_override_by_env(monkeypatch):
    monkeypatch.setenv("SPOILERGATE_MODEL_GROQ", "my-model")
    assert llm._model("groq", "default") == "my-model"
    assert llm._model("cerebras", "default") == "default"
