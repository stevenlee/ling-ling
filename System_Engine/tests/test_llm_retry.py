import os
import time
import pytest


# Ensure environment is set
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.llm_client import (
    LLMClient,
    _is_transient_llm_error,
    _is_non_retryable_llm_error,
)
from services.trace_store import TraceStore


# Custom mock exceptions to test duck typing classification
class FakeRateLimitError(Exception):
    pass


class FakeAuthenticationError(Exception):
    pass


class GenericHTTPError(Exception):
    def __init__(self, code, msg="error"):
        super().__init__(msg)
        self.status_code = code


def test_exception_classification():
    # Transient class name
    assert _is_transient_llm_error(FakeRateLimitError("Rate limit exceeded"))
    assert not _is_non_retryable_llm_error(FakeRateLimitError("Rate limit exceeded"))

    # Non-transient class name
    assert _is_non_retryable_llm_error(FakeAuthenticationError("Invalid credentials"))
    assert not _is_transient_llm_error(FakeAuthenticationError("Invalid credentials"))

    # HTTP codes
    assert _is_transient_llm_error(GenericHTTPError(408))
    assert _is_transient_llm_error(GenericHTTPError(429))
    assert _is_transient_llm_error(GenericHTTPError(503))
    assert _is_non_retryable_llm_error(GenericHTTPError(401))
    assert _is_non_retryable_llm_error(GenericHTTPError(403))

    # Error message keywords
    assert _is_transient_llm_error(Exception("Connection reset by peer"))
    assert _is_transient_llm_error(Exception("Timeout waiting for response"))
    assert not _is_transient_llm_error(Exception("SyntaxError: invalid syntax"))


def test_transient_retry_success(tmp_path, monkeypatch):
    # Disable time.sleep in retries
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda d: sleep_calls.append(d))

    client = LLMClient.__new__(LLMClient)
    client.provider = "vllm"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")

    calls = 0

    def mock_complete_once(self_ref, sys_p, usr_m, temp, max_t):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GenericHTTPError(429, "Too many requests")
        return "success response", 5, 10, 15

    monkeypatch.setattr(LLMClient, "_complete_provider_text_once", mock_complete_once)

    # Execute
    with client.trace_run(intent="test", agent="TestAgent"):
        res = client._complete_text("system", "user")

    assert res == "success response"
    assert calls == 2
    assert len(sleep_calls) == 1

    # Check database traces (must be exactly 1 succeeded trace)
    conn = client.trace_store._connect()
    try:
        rows = conn.execute("SELECT * FROM llm_calls").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    call = dict(rows[0])
    assert call["status"] == "succeeded"
    assert call["prompt_tokens"] == 5
    assert call["completion_tokens"] == 10
    assert call["total_tokens"] == 15

    import json

    metadata = json.loads(call["metadata_json"])
    assert metadata["retry_attempts"] == 2
    assert metadata["retry_transient"] is True
    assert "Too many requests" in metadata["retry_last_error"]


def test_response_channel_provenance_is_recorded(tmp_path, monkeypatch):
    client = LLMClient.__new__(LLMClient)
    client.provider = "vllm"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")

    monkeypatch.setattr(
        LLMClient,
        "_complete_provider_text_once",
        lambda *args: (
            "contract-shaped response",
            1,
            2,
            3,
            {"response_channel": "reasoning", "finish_reason": "stop"},
        ),
    )

    client._complete_text("system", "user")

    import json

    with client.trace_store._connect() as conn:
        row = conn.execute("SELECT metadata_json FROM llm_calls").fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata["response_channel"] == "reasoning"
    assert metadata["finish_reason"] == "stop"


def test_non_transient_fails_immediately(tmp_path, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda d: sleep_calls.append(d))

    client = LLMClient.__new__(LLMClient)
    client.provider = "vllm"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")

    calls = 0

    def mock_complete_once(self_ref, sys_p, usr_m, temp, max_t):
        nonlocal calls
        calls += 1
        raise FakeAuthenticationError("Invalid API Key")

    monkeypatch.setattr(LLMClient, "_complete_provider_text_once", mock_complete_once)

    # Execute should raise immediately
    with pytest.raises(FakeAuthenticationError):
        with client.trace_run(intent="test", agent="TestAgent"):
            client._complete_text("system", "user")

    assert calls == 1
    assert len(sleep_calls) == 0

    # Check database traces (must be exactly 1 failed trace)
    conn = client.trace_store._connect()
    try:
        rows = conn.execute("SELECT * FROM llm_calls").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    call = dict(rows[0])
    assert call["status"] == "failed"
    assert "Invalid API Key" in call["error"]

    import json

    metadata = json.loads(call["metadata_json"])
    assert metadata["retry_attempts"] == 1
    assert metadata["retry_transient"] is False


def test_exhausted_transient_retries(tmp_path, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda d: sleep_calls.append(d))

    client = LLMClient.__new__(LLMClient)
    client.provider = "vllm"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")

    calls = 0

    def mock_complete_once(self_ref, sys_p, usr_m, temp, max_t):
        nonlocal calls
        calls += 1
        raise GenericHTTPError(503, "Service unavailable")

    monkeypatch.setattr(LLMClient, "_complete_provider_text_once", mock_complete_once)

    # Execute should raise GenericHTTPError after 3 tries
    with pytest.raises(GenericHTTPError) as exc_info:
        with client.trace_run(intent="test", agent="TestAgent"):
            client._complete_text("system", "user")

    assert exc_info.value.status_code == 503
    assert calls == 3
    assert len(sleep_calls) == 2

    # Check database traces (must be exactly 1 failed trace)
    conn = client.trace_store._connect()
    try:
        rows = conn.execute("SELECT * FROM llm_calls").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    call = dict(rows[0])
    assert call["status"] == "failed"
    assert "Service unavailable" in call["error"]

    import json

    metadata = json.loads(call["metadata_json"])
    assert metadata["retry_attempts"] == 3
    assert metadata["retry_transient"] is True
    assert "Service unavailable" in metadata["retry_last_error"]


def test_call_site_can_bound_transport_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    client = LLMClient.__new__(LLMClient)
    client.provider = "vllm"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")
    calls = 0

    def fail(*args):
        nonlocal calls
        calls += 1
        raise GenericHTTPError(503, "busy")

    monkeypatch.setattr(LLMClient, "_complete_provider_text_once", fail)

    with pytest.raises(GenericHTTPError):
        client._complete_text("system", "user", retries=2)

    assert calls == 2


def test_openai_transport_disables_hidden_sdk_retries():
    from services.llm.transport import build_client

    client, _model = build_client("vllm")

    assert client.max_retries == 0


def test_ollama_endpoint_prefers_reachable_primary(monkeypatch):
    from services.llm import transport

    monkeypatch.setattr(transport, "_ollama_endpoint_reachable", lambda _url, _timeout: True)

    selected = transport._select_ollama_base_url(
        "http://192.168.1.103:11434/v1",
        "http://ollama-fallback.example:11434/v1",
        0.25,
    )

    assert selected == "http://192.168.1.103:11434/v1"


def test_ollama_endpoint_uses_fallback_when_primary_is_unreachable(monkeypatch):
    from services.llm import transport

    monkeypatch.setattr(transport, "_ollama_endpoint_reachable", lambda _url, _timeout: False)

    selected = transport._select_ollama_base_url(
        "http://192.168.1.103:11434/v1",
        "http://ollama-fallback.example:11434/v1",
        0.25,
    )

    assert selected == "http://ollama-fallback.example:11434/v1"


def test_ollama_endpoint_without_fallback_does_not_probe(monkeypatch):
    from services.llm import transport

    monkeypatch.setattr(
        transport,
        "_ollama_endpoint_reachable",
        lambda *_args: pytest.fail("primary should not be probed without a fallback"),
    )

    assert (
        transport._select_ollama_base_url("http://localhost:11434/v1", "", 0.25)
        == "http://localhost:11434/v1"
    )


def test_artifact_reasoning_effort_is_forwarded_and_traced(tmp_path, monkeypatch):
    import json

    client = LLMClient.__new__(LLMClient)
    client.provider = "ollama"
    client.model = "fake-model"
    client.trace_store = TraceStore(tmp_path / "trace.sqlite")
    captured = []

    def mock_complete_once(self_ref, sys_p, usr_m, temp, max_t, reasoning_effort=None):
        captured.append(reasoning_effort)
        return "| A | B |\n|---|---|\n| 1 | 2 |", 1, 2, 3

    monkeypatch.setattr(LLMClient, "_complete_provider_text_once", mock_complete_once)

    client._complete_text(
        "system",
        "user",
        reasoning_effort="none",
        trace_context={"stage": "artifact_table", "metadata": {}},
    )

    assert captured == ["none"]
    with client.trace_store._connect() as conn:
        metadata_json = conn.execute("SELECT metadata_json FROM llm_calls").fetchone()[0]
    metadata = json.loads(metadata_json)
    assert metadata["reasoning_effort"] == "none"
    assert metadata["request_priority"] == "enrichment"


def test_ollama_transport_sends_reasoning_effort_only_when_requested():
    from types import SimpleNamespace

    from services.llm.transport import openai_chat

    captured = []

    class Completions:
        def create(self, **kwargs):
            captured.append(kwargs)
            message = SimpleNamespace(content='{"ok": true}')
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    openai_chat("ollama", client, "model", "system", "user", 0.0, 128, "none")
    openai_chat("vllm", client, "model", "system", "user", 0.0, 128, "none")

    assert captured[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in captured[1]
