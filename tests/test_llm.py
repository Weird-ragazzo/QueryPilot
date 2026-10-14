import pytest

from app.config import Settings
from app import llm
from app.llm import GeminiKeysUnavailable, _with_key_fallback
from app.workflow import safe_error


def test_settings_loads_numbered_keys_before_legacy_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY1", " key-one ")
    monkeypatch.setenv("GEMINI_API_KEY2", "key-two")
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")

    assert Settings().gemini_api_keys == ["key-one", "key-two", "legacy-key"]


def test_rate_limited_key_falls_back_to_next_key(monkeypatch):
    monkeypatch.setattr(llm, "_preferred_api_key", None)
    attempted = []

    def generate(key):
        attempted.append(key)
        if key == "key-one":
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return "generated response"

    result = _with_key_fallback(["key-one", "key-two"], generate)

    assert result == "generated response"
    assert attempted == ["key-one", "key-two"]
    assert llm._preferred_api_key == "key-two"


def test_invalid_key_also_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_preferred_api_key", None)
    attempted = []

    def generate(key):
        attempted.append(key)
        if key == "key-one":
            raise RuntimeError("401 invalid API key")
        return "ok"

    assert _with_key_fallback(["key-one", "key-two"], generate) == "ok"
    assert attempted == ["key-one", "key-two"]


def test_gemini_deadline_timeout_falls_back_to_next_key(monkeypatch):
    monkeypatch.setattr(llm, "_preferred_api_key", None)
    attempted = []

    def generate(key):
        attempted.append(key)
        if key == "key-one":
            raise TimeoutError("DeadlineExceeded: deadline expired")
        return "generated response"

    assert _with_key_fallback(["key-one", "key-two"], generate) == "generated response"
    assert attempted == ["key-one", "key-two"]
    assert llm._preferred_api_key == "key-two"


def test_all_rate_limited_keys_return_sanitized_error():
    attempted = []

    def generate(key):
        attempted.append(key)
        raise RuntimeError(f"429 quota exceeded for {key}")

    with pytest.raises(GeminiKeysUnavailable) as error:
        _with_key_fallback(["secret-one", "secret-two"], generate)

    assert attempted == ["secret-one", "secret-two"]
    assert "secret" not in str(error.value)


def test_non_credential_errors_do_not_rotate():
    attempted = []

    def generate(key):
        attempted.append(key)
        raise RuntimeError("structured output could not be parsed")

    with pytest.raises(RuntimeError, match="could not be parsed"):
        _with_key_fallback(["key-one", "key-two"], generate)

    assert attempted == ["key-one"]


def test_key_pool_errors_are_clear_and_do_not_expose_credentials():
    assert "GEMINI_API_KEY1" in safe_error(RuntimeError("No Gemini API key is configured."))
    message = safe_error(GeminiKeysUnavailable("All configured Gemini API keys are rate limited, unavailable, or timed out."))
    assert "rate limited, unavailable, or timed out" in message
    assert "secret" not in message
