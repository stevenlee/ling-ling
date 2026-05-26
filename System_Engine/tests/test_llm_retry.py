import os
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

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
    with client.trace_run(intent="test", agent="TestAgent") as run_id:
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
