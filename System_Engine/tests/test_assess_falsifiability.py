from unittest.mock import MagicMock, patch
import json
import pytest

from services.llm_client import LLMClient
from core import config


@pytest.fixture
def llm():
    # Use a fake provider to bypass real initialization
    with patch("services.llm_client.LLM_PROVIDER", "gemini"):
        with patch("services.llm_client._genai", MagicMock()):
            client = LLMClient()
            client._complete_text = MagicMock()
            return client


def mock_responses(llm_client, responses: list[dict | None]):
    """Helper to queue up a sequence of parseable and unparseable JSON responses."""

    def side_effect(*args, **kwargs):
        if not responses:
            return ""
        val = responses.pop(0)
        if val is None:
            return "not json"
        return json.dumps(val)

    llm_client._complete_text.side_effect = side_effect


def test_assess_falsifiability_median_odd(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 3)
    mock_responses(
        llm,
        [
            {"score": 0.0, "falsifier": "f_0"},
            {"score": 0.5, "falsifier": "f_05"},
            {"score": 1.0, "falsifier": "f_1"},
        ],
    )

    result = llm.assess_falsifiability("Claim 1")
    assert result["score"] == 0.5
    assert result["falsifier"] == "f_05"


def test_assess_falsifiability_majority(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 3)
    mock_responses(
        llm,
        [
            {"score": 1.0, "falsifier": "f_1a"},
            {"score": 1.0, "falsifier": "f_1b"},
            {"score": 0.0, "falsifier": "f_0"},
        ],
    )

    result = llm.assess_falsifiability("Claim 2")
    assert result["score"] == 1.0
    # Both 1a and 1b have distance 0 from 1.0. Tie-breaker prefers the first one.
    assert result["falsifier"] == "f_1a"


def test_assess_falsifiability_median_even(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 3)
    # One parse failure -> 2 valid samples
    # _assess_falsifiability_once retries twice per sample, so we need to fail twice to yield a None score.
    mock_responses(
        llm,
        [
            None,
            None,  # First sample fails both attempts
            {"score": 0.5, "falsifier": "f_05"},  # Second sample succeeds on first attempt
            {"score": 1.0, "falsifier": "f_1"},  # Third sample succeeds on first attempt
        ],
    )

    result = llm.assess_falsifiability("Claim 3")
    assert result["score"] == 0.75
    # Tie-breaker for 0.75: distance to 0.5 is 0.25, distance to 1.0 is 0.25.
    # First one arrived is f_05.
    assert result["falsifier"] == "f_05"


def test_assess_falsifiability_single_valid(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 3)
    mock_responses(
        llm,
        [
            None,
            None,
            None,
            None,
            {"score": 0.5, "falsifier": "f_only"},
        ],
    )

    result = llm.assess_falsifiability("Claim 4")
    assert result["score"] == 0.5
    assert result["falsifier"] == "f_only"


def test_assess_falsifiability_all_fail(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 3)
    # 3 samples * 2 attempts each = 6 failures
    mock_responses(llm, [None] * 6)

    result = llm.assess_falsifiability("Claim 5")
    assert result["score"] is None
    assert result["falsifier"] == ""


def test_assess_falsifiability_samples_1_zero_cost(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 1)
    mock_responses(llm, [{"score": 1.0, "falsifier": "f_1"}])

    result = llm.assess_falsifiability("Claim 6")
    assert result["score"] == 1.0
    assert result["falsifier"] == "f_1"
    # Should only call _complete_text once if it succeeds
    assert llm._complete_text.call_count == 1


def test_assess_falsifiability_samples_1_retry(llm, monkeypatch):
    monkeypatch.setattr(config, "CORTEX_FALSIFY_SAMPLES", 1)
    # samples=1 仍保留「解析失敗重試一次」的既有行為
    mock_responses(llm, [None, {"score": 0.5, "falsifier": "f_retry"}])

    result = llm.assess_falsifiability("Claim 7")
    assert result["score"] == 0.5
    assert result["falsifier"] == "f_retry"
    assert llm._complete_text.call_count == 2
