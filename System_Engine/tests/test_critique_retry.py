"""Batch-2 T1: synthesis critique retry loop (_synthesize_with_critique_retry)."""

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
    def format_digest_for_prompt(digest):
        return f"DIGEST::{digest}"

    def generate_synthesis(
        self,
        title,
        part_digests,
        final_concepts,
        template=None,
        persona=None,
        critique_feedback=None,
    ):
        self.synthesis_calls.append({"critique_feedback": critique_feedback})
        return self.synthesis_texts.pop(0)

    def critique_text(self, candidate, sources, focus=None):
        result = self.critiques.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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


def test_unparseable_with_section_retries_then_adopts_parseable(monkeypatch):
    # 2026-07-12 audit: a critique that RAN but carried no parseable verdict used
    # to ship ungated (no retry). Now it retries — and adopts a parseable verdict
    # from the retry rather than silently passing the unparseable original.
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(
        ["text v1", "text v2"],
        ["* [minor] something, but no verdict line", _verdict("keep")],
    )
    out = _run(_make_pipe(llm))

    assert out["text"] == "text v2"
    assert out["verdict"] == "keep"
    assert out["attempts"] == 2
    assert out["verdict_history"] == [None, "keep"]
    assert len(llm.synthesis_calls) == 2


def test_unparseable_persists_ships_but_stays_visible(monkeypatch):
    # If the retry ALSO can't parse a verdict, the synthesis still ships, but the
    # final verdict stays None (→ recorded/counted as unparseable, never a silent
    # "keep"). Bounded by max_retries.
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(
        ["text v1", "text v2"],
        ["* [minor] no verdict", "* [minor] still no verdict"],
    )
    out = _run(_make_pipe(llm))

    assert out["verdict"] is None
    assert out["attempts"] == 2  # it DID retry (bounded), unlike the old behavior
    assert out["verdict_history"] == [None, None]


def test_empty_critique_is_error_without_regeneration(monkeypatch):
    # Empty enabled review is incomplete, not disabled. Do not regenerate an
    # entire synthesis when the reviewer has no usable response.
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 1)
    llm = _RetryStubLLM(["text v1"], [""])
    out = _run(_make_pipe(llm))

    assert out["critique_status"] == "error"
    assert "審查未完成" in out["section"]
    assert out["verdict"] is None
    assert out["attempts"] == 1
    assert len(llm.synthesis_calls) == 1


# ── verdict parsing: prose-wrapped keywords (observed live) ──────────


def test_parse_verdict_prose_wrapped_zh():
    # Live gemma output: keyword not flush against the colon.
    assert (
        IngestionPipeline._parse_verdict(
            "**Overall Verdict**: 應修正 (revise)。存在一個關鍵的數值錯誤。"
        )
        == "revise"
    )


def test_parse_verdict_prose_wrapped_en():
    assert (
        IngestionPipeline._parse_verdict(
            "**Overall Verdict**: I would revise this synthesis because of X."
        )
        == "revise"
    )


def test_parse_verdict_negated_revise_is_keep():
    assert (
        IngestionPipeline._parse_verdict("**Overall Verdict**: 不需修正，內容忠於來源。") == "keep"
    )


def test_parse_verdict_keyword_beyond_gap_is_none():
    filler = "x" * 60
    assert IngestionPipeline._parse_verdict(f"**Overall Verdict**: {filler} revise") is None


# ── verdict parsing: fully localized section shape (observed live) ───

# Verbatim from the published cloud_act (Synthesis).md critique — gemma
# localized the whole "Overall Verdict" section into a heading + bold line,
# which the pre-fix parser missed (verdict=None → reject shipped as-is).
_CLOUD_ACT_CRITIQUE = """### 缺陷清單

- `[critical] 2.3 生效觀察期 → 錯誤地標註為「1180 天」，根據來源應為「180 天」 → 修正為「180 天」`

### 總體判定

**拒絕 (Reject)**。該文件包含兩項關鍵的事實錯誤（認證對象錯誤與生效天數錯誤），\
這對於一份旨在解析法律框架的專業報告而言是致命的。"""


def test_parse_verdict_zh_section_heading_reject():
    assert IngestionPipeline._parse_verdict(_CLOUD_ACT_CRITIQUE) == "reject"


def test_parse_verdict_zh_section_heading_keep():
    assert (
        IngestionPipeline._parse_verdict("### 總體判定\n\n**保留 (Keep)**。內容忠於來源。")
        == "keep"
    )


def test_parse_verdict_zh_bold_header_same_line():
    assert IngestionPipeline._parse_verdict("**總體判定**：應修正 (revise)。") == "revise"


def test_parse_verdict_zh_section_negated_revise_is_keep():
    assert (
        IngestionPipeline._parse_verdict("### 總體判定\n\n**不需修正**，內容忠於來源。") == "keep"
    )


def test_parse_verdict_zh_bold_header_colon_inside_bold_next_line():
    # Verbatim shape from the post-fix cloud_act re-run: yet another header
    # variant (評定, colon inside the bold) with the keyword on the next line.
    critique = (
        "- `[major] 2.1 節 → 遺漏 14 日時限 → 應補充`\n\n"
        "**總體評定：**\n"
        "應進行修改 (Revise)。雖然結構出色，但存在關鍵的程序性細節遺漏。"
    )
    assert IngestionPipeline._parse_verdict(critique) == "revise"


def test_parse_verdict_zh_xiugai_keyword_maps_to_revise():
    assert IngestionPipeline._parse_verdict("### 總體評定\n\n建議修改。") == "revise"


def test_parse_verdict_zh_heading_mentioned_mid_prose_is_none():
    # A heading followed by prose whose keyword sits beyond the gap must not match.
    filler = "這份文件整體而言相當完整，" * 4
    assert IngestionPipeline._parse_verdict(f"### 總體判定\n\n{filler}建議修正。") is None


# ── 2026-07-12 audit: real "unparseable" critique headers gemma emitted ──
# 3 of 4 unparseable syntheses had a clear verdict under 總體結論 / 總體裁定,
# which the old header class ([判評评][定价價]) didn't cover → the quality gate
# silently failed and un-revised syntheses shipped.


def test_parse_verdict_zh_bold_ruling_header_revise():
    # #2 Effective harnesses: "**總體裁定：應修改**" (裁定 = ruling).
    assert IngestionPipeline._parse_verdict("**總體裁定：應修改**") == "revise"


def test_parse_verdict_zh_conclusion_heading_revise():
    # #3 Knowledge distillation: "## 總體結論\n\n**建議修改 (Revise)**".
    assert (
        IngestionPipeline._parse_verdict("## 總體結論\n\n**建議修改 (Revise)**。該候選文本…")
        == "revise"
    )


def test_parse_verdict_zh_conclusion_heading_keep():
    # #4 Fine-tuning: "## 總體結論\n\n保留。…".
    assert IngestionPipeline._parse_verdict("## 總體結論\n\n保留。該候選文本極高品質。") == "keep"


def test_parse_verdict_zh_simplified_conclusion_heading():
    assert IngestionPipeline._parse_verdict("## 总体结论\n\n建议修改。") == "revise"


# ── 2026-07-24 obs-window re-audit: the REQUIRED bilingual header ────────
# bb811db made critique.md mandate `**總體判定 (Overall Verdict)**` — gemma then
# complied verbatim, but neither regex accepted the parenthesized English tail,
# so 11/14 window syntheses parsed to None. The format the prompt REQUIRES must
# always parse. Shapes below are verbatim from 2026-07-24 critique traces.


def test_parse_verdict_bilingual_header_backticked_keep():
    critique = (
        "- `[minor] Mermaid 圖表節點 → 標籤未依規範 → 修正`\n\n"
        "**總體判定 (Overall Verdict)**\n"
        "`keep`\n\n"
        "該文件極度忠實於原始資料，僅有微小瑕疵。"
    )
    assert IngestionPipeline._parse_verdict(critique) == "keep"


def test_parse_verdict_bilingual_header_plain_revise():
    critique = (
        "- `[major] Mermaid 圖表節點 E → 誤標示為「增加量」 → 修正`\n\n"
        "**總體判定 (Overall Verdict)**\n"
        "revise\n\n"
        "此候選文本結構極佳，然而圖表存在一處重大錯誤。"
    )
    assert IngestionPipeline._parse_verdict(critique) == "revise"


def test_parse_verdict_bilingual_header_same_line_colon():
    assert (
        IngestionPipeline._parse_verdict("**總體判定 (Overall Verdict)**: reject。含關鍵事實錯誤。")
        == "reject"
    )


def test_parse_verdict_bilingual_header_fullwidth_parens():
    assert (
        IngestionPipeline._parse_verdict("### 總體判定（Overall Verdict）\n\n**保留 (Keep)**。")
        == "keep"
    )


def test_parse_verdict_summary_section_still_none():
    # #1 Algorithms4Decision: model wrote 總結與建議 (summary), no verdict — the
    # "保留" in the suggestions is about keeping content, NOT a verdict. 總結 lacks
    # 體 so the header must NOT match; this stays correctly unparseable.
    txt = "### 💡 總結與建議\n\n**建議：**\n1. 建議保留部分具體的應用案例以增強說服力。"
    assert IngestionPipeline._parse_verdict(txt) is None


# ── verdict → publication status (_write_synthesis) ──────────────────


class _WriteStubLLM:
    """Bare LLM stub: no trace_store / current_trace_ids, so the trace
    branches in _write_synthesis are skipped."""

    model = "stub-model"


def _write_synthesis(tmp_path, monkeypatch, outcome, *, enabled=True, recorded=None):
    from services.ingest.part_state import PartState

    monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", enabled)
    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", False)
    monkeypatch.setattr(
        "services.learning_artifacts.maybe_artifact_section",
        lambda *a, **k: "",
    )

    pipe = IngestionPipeline.__new__(IngestionPipeline)
    pipe.llm = _WriteStubLLM()
    pipe.rag = MagicMock()
    pipe._synthesize_with_critique_retry = lambda *a, **k: outcome
    if recorded is not None:

        def record(path, kind, title, metadata, quality_verdict=None):
            recorded.append(
                {"kind": kind, "metadata": metadata, "quality_verdict": quality_verdict}
            )

        pipe._record_artifact = record

    path = pipe._write_synthesis(
        base_title="Doc",
        content="src content",
        chunks=["c1"],
        source_spans=[],
        part_state=PartState(part_digests=[{"part": 1, "thesis": "t"}]),
    )
    return path.read_text(encoding="utf-8")


def _outcome(verdict, section):
    return {
        "text": "synthesis body",
        "fixes": [],
        "section": section,
        "verdict": verdict,
        "attempts": 1,
        "verdict_history": [verdict],
    }


_REJECT_SECTION = (
    "## 🔍 Quality Critique\n\n"
    "- `[critical] 生效觀察期 → 1180 天應為 180 天 → 修正`\n"
    "- `[minor] 圖表 → 引號 → 修正`\n\n"
    "### 總體判定\n\n**拒絕 (Reject)**。含關鍵事實錯誤。\n\n"
)


def test_reject_verdict_publishes_needs_review_with_warning(tmp_path, monkeypatch):
    text = _write_synthesis(tmp_path, monkeypatch, _outcome("reject", _REJECT_SECTION))

    assert "#NeedsReview" in text
    assert "#PerfectPitch" not in text
    assert "quality_verdict: reject" in text
    # The warning callout sits before the Executive Summary and excerpts
    # the critical finding; minor findings stay in the appendix only.
    assert text.index("[!warning]") < text.index("## 📝 Executive Summary")
    assert text.count("[critical] 生效觀察期") == 2  # callout + appendix
    assert "[minor]" not in text.split("## 📝 Executive Summary")[0]


def test_keep_verdict_publishes_perfect_pitch_without_warning(tmp_path, monkeypatch):
    section = "## 🔍 Quality Critique\n\n**Overall Verdict**: keep. Clean.\n\n"
    text = _write_synthesis(tmp_path, monkeypatch, _outcome("keep", section))

    assert "#PerfectPitch" in text
    assert "#NeedsReview" not in text
    assert "quality_verdict: keep" in text
    assert "[!warning]" not in text


def test_unparseable_verdict_with_critique_text_needs_review(tmp_path, monkeypatch):
    # The cloud_act failure mode: critique ran and found defects, but the
    # verdict line was not parseable — must not ship as #PerfectPitch.
    section = "## 🔍 Quality Critique\n\n- `[critical] 錯誤` 但沒有判定行\n\n"
    text = _write_synthesis(tmp_path, monkeypatch, _outcome(None, section))

    assert "#NeedsReview" in text
    assert "quality_verdict: unparseable" in text
    assert "[!warning]" in text


def test_disabled_critique_keeps_legacy_publication_behavior(tmp_path, monkeypatch):
    text = _write_synthesis(tmp_path, monkeypatch, _outcome(None, ""), enabled=False)

    assert "#PerfectPitch" in text
    assert "quality_verdict" not in text
    assert "[!warning]" not in text


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


@pytest.mark.parametrize(
    "response", [ConnectionError("offline"), "Critique failed: timeout", "", None, 42]
)
def test_enabled_review_failure_publishes_needs_review_and_traces_error(
    tmp_path, monkeypatch, response
):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 3)
    llm = _RetryStubLLM(["candidate"], [response])
    outcome = _run(_make_pipe(llm))
    assert len(llm.synthesis_calls) == 1
    assert outcome["attempts"] == 1 and outcome["verdict"] is None
    assert outcome["critique_status"] == "error"
    assert outcome["critique_error"]
    assert outcome["verdict_history"] == [None]
    assert outcome["critique_status_history"] == ["error"]
    recorded = []
    text = _write_synthesis(tmp_path, monkeypatch, outcome, recorded=recorded)
    assert "#NeedsReview" in text and "#PerfectPitch" not in text
    assert "quality_verdict: error" in text and "critique_status: error" in text
    assert "審查未完成" in text
    assert "發現未解決的缺陷" not in text  # failure is not a content judgment
    assert text.index("[!warning]") < text.index("## 📝 Executive Summary")
    assert recorded[0]["quality_verdict"] == "error"
    assert recorded[0]["metadata"]["critique_status"] == "error"


def test_legacy_empty_outcome_cannot_silently_pass_enabled_gate(tmp_path, monkeypatch):
    text = _write_synthesis(tmp_path, monkeypatch, _outcome(None, ""))
    assert "#NeedsReview" in text and "#PerfectPitch" not in text
    assert "quality_verdict: error" in text and "審查未完成" in text


@pytest.mark.parametrize("first", ["revise", "reject", None])
def test_retry_outage_keeps_original_and_stops_regeneration(monkeypatch, first):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 3)
    original = _verdict(first) if first else "Findings without a verdict."
    llm = _RetryStubLLM(
        ["reviewed original", "unreviewed retry"], [original, ConnectionError("down")]
    )
    result = _run(_make_pipe(llm))
    assert result["text"] == "reviewed original"
    assert result["verdict"] == first
    assert len(llm.synthesis_calls) == 2
    assert result["attempts"] == 2
    assert result["critique_status"] == ("succeeded" if first else "unparseable")
    assert result["critique_status_history"][-1] == "error"
    assert "down" in result["critique_error_history"][-1]
    assert result["section"].endswith(original + "\n\n")


def test_disabled_gate_is_explicit_and_does_not_call_reviewer(tmp_path, monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)
    llm = _RetryStubLLM(["candidate"], [])
    result = _run(_make_pipe(llm))
    assert result["critique_status"] == "disabled"
    assert result["section"] == "" and result["verdict"] is None
    text = _write_synthesis(tmp_path, monkeypatch, result, enabled=False)
    assert "critique_status: disabled" in text
    assert "quality_verdict" not in text
    assert "#PerfectPitch" in text and "[!warning]" not in text


def test_real_client_exhausted_empty_content_is_error_without_synthesis_retry(monkeypatch):
    from services.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)
    calls = []
    client._get_lang_hint = lambda: "English"
    client._build_system_prompt = lambda *a, **k: ("fixed critique", {})
    client.format_digest_for_prompt = lambda d: "source"
    client.generate_synthesis = lambda *a, **k: "candidate"

    def empty(*a, **kwargs):
        calls.append(kwargs)
        return ""

    client._complete_text = empty
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 3)
    result = _run(_make_pipe(client))
    assert len(calls) == 3  # existing bounded content rerolls, no whole-page regeneration
    assert [c["trace_context"]["metadata"]["content_attempt"] for c in calls] == [1, 2, 3]
    assert result["attempts"] == 1 and result["critique_status"] == "error"


def test_real_client_transport_failure_does_not_multiply_content_retries(monkeypatch):
    from services.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)
    calls = []
    client._get_lang_hint = lambda: "English"
    client._build_system_prompt = lambda *a, **k: ("fixed critique", {})
    client.format_digest_for_prompt = lambda d: "source"
    client.generate_synthesis = lambda *a, **k: "candidate"

    def failed(*a, **kwargs):
        calls.append(kwargs)
        raise ConnectionError("transport already exhausted")

    client._complete_text = failed
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    result = _run(_make_pipe(client))
    assert len(calls) == 1
    assert result["critique_status"] == "error" and result["verdict"] is None


def test_llm_fixture_trace_store_is_under_tmp_path(llm_client, tmp_path):
    assert llm_client.trace_store.db_path.is_relative_to(tmp_path)


def test_source_formatting_error_is_visible_and_does_not_call_reviewer():
    from services.ingest.critique_loop import SynthesisCritiqueLoop

    class BrokenSources:
        def format_digest_for_prompt(self, digest):
            raise ValueError("bad digest")

        def critique_text(self, **kwargs):
            raise AssertionError("reviewer must not be called without sources")

    result = SynthesisCritiqueLoop(BrokenSources()).critique_result(
        "Doc",
        "candidate",
        ["bad"],
        enabled=True,
    )
    assert result["critique_status"] == "error"
    assert result["verdict"] is None and "bad digest" in result["critique_error"]


def test_retry_error_history_is_preserved_in_published_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
    monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_MAX_RETRIES", 2)
    llm = _RetryStubLLM(["original", "retry"], [_verdict("revise"), ConnectionError("offline")])
    outcome = _run(_make_pipe(llm))
    recorded = []
    text = _write_synthesis(tmp_path, monkeypatch, outcome, recorded=recorded)
    assert "quality_verdict: revise" in text and "#NeedsReview" in text
    assert "critique_error_history:" in text
    metadata = recorded[0]["metadata"]
    assert metadata["critique_status"] == "succeeded"  # selected original was reviewed
    assert metadata["critique_status_history"] == ["succeeded", "error"]
    assert "offline" in metadata["critique_error_history"][-1]
