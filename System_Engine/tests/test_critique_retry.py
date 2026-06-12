"""Batch-2 T1: synthesis critique retry loop (_synthesize_with_critique_retry)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from unittest.mock import MagicMock, patch
import pytest

from services.ingestion_pipeline import IngestionPipeline


class _RetryStubLLM:
    """Stub yielding queued synthesis texts and critique responses in order."""

    def __init__(self, synthesis_texts, critiques):
        self.synthesis_texts = list(synthesis_texts)
        self.critiques = list(critiques)
        self.synthesis_calls = []

    @staticmethod
    def _format_part_digest_for_prompt(digest):
        return f"DIGEST::{digest}"

    def generate_synthesis(self, title, part_digests, final_concepts,
                           template=None, persona=None, critique_feedback=None):
        self.synthesis_calls.append({"critique_feedback": critique_feedback})
        return self.synthesis_texts.pop(0)

    def critique_text(self, candidate, sources, focus=None):
        return self.critiques.pop(0)


def _make_pipe(llm):
    pipe = IngestionPipeline.__new__(IngestionPipeline)
    pipe.llm = llm
    return pipe


_PART_STATE = {"part_digests": ["d1"], "pending_concepts": ""}


def _verdict(v):
    return f"* [major] finding\n\n**Overall Verdict**: {v}. Reason."


def _run(pipe):
    return pipe._synthesize_with_critique_retry("Doc", _PART_STATE, "wiki-note", "none")


def test_keep_verdict_no_retry(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1"], [_verdict("keep")])
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v1"
    assert out["verdict"] == "keep"
    assert out["attempts"] == 1
    assert out["verdict_history"] == ["keep"]
    assert len(llm.synthesis_calls) == 1
    assert llm.synthesis_calls[0]["critique_feedback"] is None


def test_revise_then_keep_adopts_retry(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1", "text v2"], [_verdict("revise"), _verdict("keep")])
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v2"
    assert out["verdict"] == "keep"
    assert out["attempts"] == 2
    assert out["verdict_history"] == ["revise", "keep"]
    # The retry call carried the first critique's findings as feedback.
    feedback = llm.synthesis_calls[1]["critique_feedback"]
    assert feedback is not None
    assert "Overall Verdict" in feedback


def test_retry_not_better_keeps_original(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1", "text v2"], [_verdict("revise"), _verdict("reject")])
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v1"
    assert out["verdict"] == "revise"
    assert out["attempts"] == 2
    assert out["verdict_history"] == ["revise", "reject"]


def test_retry_equal_verdict_keeps_original(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1", "text v2"], [_verdict("revise"), _verdict("revise")])
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v1"
    assert out["verdict"] == "revise"
    assert out["verdict_history"] == ["revise", "revise"]


def test_zero_retries_matches_status_quo(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 0)
    llm = _RetryStubLLM(["text v1"], [_verdict("revise")])
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v1"
    assert out["verdict"] == "revise"
    assert out["attempts"] == 1
    assert out["verdict_history"] == ["revise"]
    assert len(llm.synthesis_calls) == 1


def test_unparseable_verdict_never_retries(monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1"], ["* [minor] something, but no verdict line"])
    out = _run(_make_pipe(llm))

    assert out["verdict"] is None
    assert out["attempts"] == 1
    assert len(llm.synthesis_calls) == 1


# ── verdict parsing: prose-wrapped keywords (observed live) ──────────


def test_parse_verdict_prose_wrapped_zh():
    # Live gemma output: keyword not flush against the colon.
    assert IngestionPipeline._parse_verdict(
        "**Overall Verdict**: 應修正 (revise)。存在一個關鍵的數值錯誤。"
    ) == "revise"


def test_parse_verdict_prose_wrapped_en():
    assert IngestionPipeline._parse_verdict(
        "**Overall Verdict**: I would revise this synthesis because of X."
    ) == "revise"


def test_parse_verdict_negated_revise_is_keep():
    assert IngestionPipeline._parse_verdict(
        "**Overall Verdict**: 不需修正，內容忠於來源。"
    ) == "keep"


def test_parse_verdict_keyword_beyond_gap_is_none():
    filler = "x" * 60
    assert IngestionPipeline._parse_verdict(
        f"**Overall Verdict**: {filler} revise"
    ) is None


# ── generate_synthesis prompt: critique_feedback path ────────────────


@pytest.fixture
def llm_client():
    from services.llm_client import LLMClient
    with patch("services.llm_client.LLM_PROVIDER", "gemini"):
        with patch("services.llm_client._genai", MagicMock()):
            client = LLMClient()
            client._complete_text = MagicMock(return_value="synthesis body")
            client._get_lang_hint = MagicMock(return_value="English")
            client._build_system_prompt = MagicMock(return_value=("SYS", {}))
            return client


def _captured_prompt(client):
    args, kwargs = client._complete_text.call_args
    # generate_synthesis passes (system_prompt, prompt) positionally.
    return args[1]


def test_prompt_without_feedback_is_byte_identical(llm_client):
    llm_client.generate_synthesis("T", ["d1"], "concepts")
    legacy_prompt = _captured_prompt(llm_client)

    llm_client._complete_text.reset_mock()
    llm_client.generate_synthesis("T", ["d1"], "concepts", critique_feedback=None)
    none_prompt = _captured_prompt(llm_client)

    assert none_prompt == legacy_prompt
    assert "Previous attempt was critiqued" not in none_prompt


def test_prompt_with_feedback_inserts_block_before_task(llm_client):
    llm_client.generate_synthesis("T", ["d1"], "concepts", critique_feedback="[major] fix X")
    prompt = _captured_prompt(llm_client)

    marker = "Previous attempt was critiqued. Address these findings:\n[major] fix X"
    assert marker in prompt
    assert prompt.index(marker) < prompt.index("Task:\n")
