"""Gemini finish reasons reach the trace, so M1 can see truncation."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

genai_types = pytest.importorskip("google.genai.types")

from maintenance.self_assessment import RED, run_self_assessment  # noqa: E402
from services.llm.transport import gemini_finish_reason  # noqa: E402
from services.llm_client import LLMClient  # noqa: E402
from services.trace_store import TraceStore  # noqa: E402


def _response(finish_reason=None, candidates=True, text="partial"):
    if not candidates:
        return genai_types.GenerateContentResponse(candidates=[])
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                finish_reason=finish_reason,
                content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
            )
        ]
    )


def test_max_tokens_is_normalized_to_length():
    assert gemini_finish_reason(_response(genai_types.FinishReason.MAX_TOKENS)) == "length"


def test_stop_is_normalized_to_stop():
    assert gemini_finish_reason(_response(genai_types.FinishReason.STOP)) == "stop"


def test_missing_candidates_or_reason_is_none():
    assert gemini_finish_reason(_response(candidates=False)) is None
    assert gemini_finish_reason(genai_types.GenerateContentResponse()) is None
    assert gemini_finish_reason(_response(finish_reason=None)) is None


class _FakeModels:
    def __init__(self, response):
        self._response = response

    def generate_content(self, **kwargs):
        return self._response


class _FakeGeminiClient:
    def __init__(self, response):
        self.models = _FakeModels(response)


def test_gemini_max_tokens_turns_m1_llm_health_red(tmp_path):
    trace = TraceStore(tmp_path / "trace.sqlite", retention_days=0)
    client = LLMClient.__new__(LLMClient)
    client.provider = "gemini"
    client.model = "gemini-test"
    client.client = _FakeGeminiClient(_response(genai_types.FinishReason.MAX_TOKENS))
    client.trace_store = trace

    for _ in range(5):
        client._complete_text(
            system_prompt="s",
            user_msg="u",
            trace_context={"stage": "extract_claims", "metadata": {}},
        )

    health = trace.llm_call_health(since_days=1)
    assert health["failed"] == 0
    assert health["by_stage"]["extract_claims"]["truncated"] == 5

    result = run_self_assessment(
        trace,
        cortex_dir=tmp_path / "cortex",
        insights_dir=tmp_path / "insights",
        bench_history_file=tmp_path / "bench.json",
        decay_file=tmp_path / "decay.json",
        ledger_file=tmp_path / "ledger.json",
        insight_signals_file=tmp_path / "insight_signals.json",
        cortex_state_file=tmp_path / "cortex_state.json",
        report_dir=tmp_path / "out",
        log_path=tmp_path / "maint.log.md",
        history_file=tmp_path / "assessment_history.json",
    )
    ax = next(a for a in result.axes if a.name == "LLM 健康")
    assert ax.lamp == RED
    assert any("extract_claims" in o and "截斷" in o for o in result.observations)
