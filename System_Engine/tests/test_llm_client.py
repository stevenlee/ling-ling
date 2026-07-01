"""Tests for the LLM-free logic inside services.llm_client.

We don't mock the actual provider here — we only exercise the pure helpers
(YAML parsing, file caching, digest formatting, fallbacks) that run alongside
the LLM call but don't require one.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

# Set provider before importing so __init__ doesn't trip on unknown provider.
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from core.utils import MtimeCache
from services.llm_client import LLMClient
from services.trace_store import TraceStore


# ── MtimeCache ──────────────────────────────────────────────────────

class TestMtimeCache:
    def test_caches_and_returns_content(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("hello", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "hello"
        # Confirm we don't re-read by overwriting at the OS level but keeping
        # the same mtime → cached value should still come back.
        # (We test mtime invalidation separately.)
        assert c.read(p) == "hello"

    def test_invalidates_on_mtime_change(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("v1", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "v1"

        # Bump mtime forward to force invalidation.
        new_mtime = p.stat().st_mtime + 10
        p.write_text("v2", encoding="utf-8")
        os.utime(p, (new_mtime, new_mtime))
        assert c.read(p) == "v2"

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nope.md"
        c = MtimeCache()
        assert c.read(p) == ""

    def test_deletion_after_cache_returns_empty(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("data", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "data"
        p.unlink()
        assert c.read(p) == ""


# ── _hybrid_parse ───────────────────────────────────────────────────

class TestHybridParse:
    def test_yaml_frontmatter(self):
        r = LLMClient._hybrid_parse(
            "---\ntitle: Hello\ntags: [a, b]\ntype: note\n---\n\nBody content"
        )
        assert r["title"] == "Hello"
        assert r["tags"] == ["a", "b"]
        assert r["type"] == "note"
        assert r["content"] == "Body content"

    def test_yaml_fenced(self):
        r = LLMClient._hybrid_parse(
            "```yaml\ntitle: Wrapped\ntags: [x]\n```\n\nBody"
        )
        assert r["title"] == "Wrapped"
        assert r["tags"] == ["x"]

    def test_h1_fallback_when_no_yaml(self):
        r = LLMClient._hybrid_parse("# Some Title\n\nBody")
        assert r["title"] == "Some Title"

    def test_default_when_no_signals(self):
        r = LLMClient._hybrid_parse("just prose")
        assert r["title"] == "Untitled"
        assert r["content"] == "just prose"

    def test_empty_input(self):
        r = LLMClient._hybrid_parse("")
        assert r["title"] == "Untitled"
        assert r["content"] == ""

    def test_yaml_with_pending_concepts(self):
        r = LLMClient._hybrid_parse(
            "---\ntitle: T\npending_concepts: [unfinished]\n---\nBody"
        )
        assert r.get("pending_concepts") == ["unfinished"]

    def test_outer_fence_wrap(self):
        text = "```markdown\n---\ntitle: Outer\ntags: [tag]\n---\nBody inside outer fence\n```"
        r = LLMClient._hybrid_parse(text)
        assert r["title"] == "Outer"
        assert r["tags"] == ["tag"]
        assert r["content"] == "Body inside outer fence"

    def test_inner_body_fence_wrap(self):
        text = "---\ntitle: Inner\ntags: [tag]\n---\n```markdown\nBody inside inner fence\n```"
        r = LLMClient._hybrid_parse(text)
        assert r["title"] == "Inner"
        assert r["tags"] == ["tag"]
        assert r["content"] == "Body inside inner fence"


# ── _strip_accidental_frontmatter ────────────────────────────────────

class TestStripAccidentalFrontmatter:
    def test_strips_markdown_fence(self):
        r = LLMClient._strip_accidental_frontmatter("```markdown\n# Hi\nbody\n```")
        assert r == "# Hi\nbody"

    def test_strips_md_fence(self):
        r = LLMClient._strip_accidental_frontmatter("```md\nx\n```")
        assert r == "x"

    def test_strips_body_frontmatter(self):
        r = LLMClient._strip_accidental_frontmatter("---\ntitle: x\n---\n\nBody")
        assert r == "Body"

    def test_empty_input(self):
        assert LLMClient._strip_accidental_frontmatter("") == ""
        assert LLMClient._strip_accidental_frontmatter(None) == ""

    def test_no_changes_when_clean(self):
        assert LLMClient._strip_accidental_frontmatter("# Clean") == "# Clean"


# ── Part digest helpers ─────────────────────────────────────────────

class TestPartDigest:
    def test_apply_defaults_fills_missing_keys(self):
        result = LLMClient._apply_part_digest_defaults({"thesis": "T"}, 3)
        assert result["part"] == 3
        assert result["title"] == "Part 3"
        assert result["thesis"] == "T"
        assert result["key_points"] == []
        assert result["evidence"] == []
        assert result["handoff"] == ""

    def test_apply_defaults_preserves_existing(self):
        given = {"part": 7, "title": "Custom", "key_points": ["a", "b"]}
        result = LLMClient._apply_part_digest_defaults(given, 99)
        assert result["part"] == 7
        assert result["title"] == "Custom"
        assert result["key_points"] == ["a", "b"]

    def test_format_string_passthrough(self):
        assert LLMClient.format_digest_for_prompt("raw text") == "raw text"

    def test_format_dict_emits_sections(self):
        digest = {
            "part": 2,
            "title": "Intro",
            "thesis": "Central claim.",
            "key_points": ["one", "two"],
            "evidence": ["e1"],
            "terms": [],
            "open_questions": [],
            "handoff": "next",
        }
        text = LLMClient.format_digest_for_prompt(digest)
        assert "### Part 2: Intro" in text
        assert "Thesis: Central claim." in text
        assert "- one" in text
        assert "- two" in text
        assert "- (none)" in text  # for the empty `terms`
        assert "next" in text

    def test_format_none_safe(self):
        assert "(empty digest)" in LLMClient.format_digest_for_prompt(None)
        assert "(empty digest)" not in LLMClient.format_digest_for_prompt("x")


# ── Language hint ───────────────────────────────────────────────────

class TestLanguageHint:
    def test_traditional_chinese_hint_is_explicit(self, monkeypatch):
        import services.llm_client as llm_mod

        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Traditional Chinese")
        client = LLMClient.__new__(LLMClient)
        hint = client._get_lang_hint()
        assert "Traditional Chinese" in hint
        assert "MUST NOT use Simplified Chinese" in hint

    def test_simplified_chinese_hint_is_explicit(self, monkeypatch):
        import services.llm_client as llm_mod

        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Simplified Chinese")
        client = LLMClient.__new__(LLMClient)
        hint = client._get_lang_hint()
        assert "Simplified Chinese" in hint
        assert "MUST NOT use Traditional Chinese" in hint

    def test_generic_chinese_warns_against_mixing(self, monkeypatch):
        import services.llm_client as llm_mod

        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Chinese")
        client = LLMClient.__new__(LLMClient)
        hint = client._get_lang_hint()
        assert "do NOT mix" in hint

    def test_localized_suffix_maps_language(self, monkeypatch):
        import services.llm_client as llm_mod
        client = LLMClient.__new__(LLMClient)
        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Traditional Chinese")
        assert client._localized_suffix() == ".zh"
        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Japanese")
        assert client._localized_suffix() == ".ja"
        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "English")
        assert client._localized_suffix() == ""

    def test_field_labels_localized_not_english_fallback(self, monkeypatch):
        # Regression: labels used to fall back to English because the map was
        # keyed on _get_lang_hint()'s long string. Now keyed on the suffix.
        import services.llm_client as llm_mod
        client = LLMClient.__new__(LLMClient)
        monkeypatch.setattr(llm_mod.settings, "OUTPUT_LANGUAGE", "Traditional Chinese")
        labels = llm_mod._LABELS_BY_SUFFIX.get(client._localized_suffix(), llm_mod._DEFAULT_LABELS)
        assert labels["file"] == "檔案名稱" and labels["content"] == "素材內容"


# ── answer_query prompt assembly ────────────────────────────────────

class TestAnswerQuery:
    def test_custom_instruction_includes_provided_context(self):
        client = LLMClient.__new__(LLMClient)
        client._build_system_prompt = lambda *a, **kw: ("system prompt", {})
        captured = {}

        def fake_complete(system_prompt, user_msg, **kwargs):
            captured["system_prompt"] = system_prompt
            captured["user_msg"] = user_msg
            captured["kwargs"] = kwargs
            return "answer"

        client._complete_text = fake_complete

        text = client.answer_query(
            query_content="compare the books",
            wiki_context="SOURCE BODY FROM VAULT",
            custom_instruction="answer from sources",
            operation="answer_from_sources",
            persona="none",
            forced_template="none",
        )

        assert text == "answer"
        assert "## User Directive\ncompare the books" in captured["user_msg"]
        assert "## Provided Source Text\nSOURCE BODY FROM VAULT" in captured["user_msg"]
        assert captured["kwargs"]["trace_context"]["operation"] == "answer_from_sources"

    def test_forced_template_routes_through_builder_without_custom_instruction(self):
        client = LLMClient.__new__(LLMClient)
        captured = {}

        def fake_build(instruction, **kwargs):
            captured["instruction"] = instruction
            captured["build_kwargs"] = kwargs
            return "system prompt", {"template": kwargs.get("forced_template")}

        client._build_system_prompt = fake_build
        client._complete_text = lambda system_prompt, user_msg, **kw: "answer"

        text = client.answer_query(
            query_content="fill in the disclosure",
            wiki_context="SOURCE BODY",
            forced_template="sw-inv-disclosure-rpt",
        )

        assert text == "answer"
        # No custom_instruction → builder is invoked with a generated task and
        # the template's own YAML schema is honored.
        assert captured["build_kwargs"]["forced_template"] == "sw-inv-disclosure-rpt"
        assert captured["build_kwargs"]["require_yaml_header"] is True

    def test_plain_qa_skips_builder(self):
        def _no_builder(*a, **kw):
            raise AssertionError("builder must not run for plain Q&A")

        client = LLMClient.__new__(LLMClient)
        client._build_system_prompt = _no_builder
        client._load_project_identity = lambda: "IDENTITY"
        client._get_lang_hint = lambda: "English"
        client._complete_text = lambda system_prompt, user_msg, **kw: "plain answer"

        text = client.answer_query(
            query_content="what is ling-ling?",
            wiki_context="",
            forced_template="none",
        )
        assert text == "plain answer"


# ── LLM tracing ─────────────────────────────────────────────────────

class _FakeUsage:
    prompt_tokens = 3
    completion_tokens = 5
    total_tokens = 8


class _FakeMessage:
    content = "traced response"


class _FakeChoice:
    message = _FakeMessage()


class _FakeCompletion:
    choices = [_FakeChoice()]
    usage = _FakeUsage()


class _FakeCompletions:
    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeCompletion()


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = _FakeChat()


class TestLLMTrace:
    def test_complete_text_records_call_inside_run(self, tmp_path):
        client = LLMClient.__new__(LLMClient)
        client.provider = "vllm"
        client.model = "fake-model"
        client.client = _FakeOpenAIClient()
        client.trace_store = TraceStore(tmp_path / "trace.sqlite")

        with client.trace_run(intent="test", agent="TestAgent") as run_id:
            text = client._complete_text(
                "system",
                "user",
                temperature=0.2,
                max_tokens=32,
                trace_context={"stage": "unit_stage", "operation": "critique"},
            )
            trace_ids = client.current_trace_ids()

        assert text == "traced response"
        assert len(trace_ids) == 1

        conn = client.trace_store._connect()
        try:
            call = conn.execute(
                "SELECT run_id, stage, operation, prompt_tokens, completion_tokens, total_tokens, status "
                "FROM llm_calls WHERE trace_id = ?",
                (trace_ids[0],),
            ).fetchone()
            run = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        finally:
            conn.close()

        assert call["run_id"] == run_id
        assert call["stage"] == "unit_stage"
        assert call["operation"] == "critique"
        assert call["prompt_tokens"] == 3
        assert call["completion_tokens"] == 5
        assert call["total_tokens"] == 8
        assert call["status"] == "succeeded"
        assert run["status"] == "succeeded"


class TestAssessFalsifiability:
    def _client(self, monkeypatch, response):
        client = LLMClient.__new__(LLMClient)
        monkeypatch.setattr(client, "_complete_text", lambda *a, **k: response)
        return client

    def test_parses_valid_json(self, monkeypatch):
        response = '```json\n{"score": 0.8, "falsifier": "find X"}\n```'
        res = self._client(monkeypatch, response).assess_falsifiability("claim")
        assert res["score"] == 0.8
        assert res["falsifier"] == "find X"

    def test_bilingual_falsifier_combined(self, monkeypatch):
        response = ('{"score": 1.0, "falsifier": "A documented counter-case.", '
                    '"falsifier_zh": "一個有紀錄的反例。"}')
        res = self._client(monkeypatch, response).assess_falsifiability("claim")
        assert res["falsifier"] == "A documented counter-case.（一個有紀錄的反例。）"

    def test_missing_zh_keeps_english_only(self, monkeypatch):
        response = '{"score": 0.5, "falsifier": "English only."}'
        res = self._client(monkeypatch, response).assess_falsifiability("claim")
        assert res["falsifier"] == "English only."

    def test_handles_missing_keys(self, monkeypatch):
        res = self._client(monkeypatch, '{}').assess_falsifiability("claim")
        assert res["score"] is None
        assert res["falsifier"] == ""

    def test_handles_invalid_json(self, monkeypatch):
        res = self._client(monkeypatch, 'garbage').assess_falsifiability("claim")
        assert res["score"] is None
        assert res["falsifier"] == ""


class TestTranslateTags:
    """translate_tags routes through _complete_json, inheriting transport
    retry + centralized tracing (audit C1). These exercise that wiring."""

    def _client(self, monkeypatch, responses):
        # `responses` is a list consumed one per _complete_text call so we can
        # simulate a transient failure followed by a good reply (the re-roll).
        client = LLMClient.__new__(LLMClient)
        seq = list(responses)

        def fake(*a, **k):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(client, "_complete_text", fake)
        return client

    def test_parses_mapping(self, monkeypatch):
        c = self._client(monkeypatch, ['{"機器學習": "Machine Learning"}'])
        assert c.translate_tags(["機器學習"]) == {"機器學習": "Machine Learning"}

    def test_parses_fenced_json(self, monkeypatch):
        c = self._client(monkeypatch, ['```json\n{"深度學習": "Deep Learning"}\n```'])
        assert c.translate_tags(["深度學習"]) == {"深度學習": "Deep Learning"}

    def test_transient_failure_is_retried(self, monkeypatch):
        # First call raises (e.g. 429); _complete_json re-rolls and succeeds —
        # the old hand-rolled impl returned {} on the first transient error.
        c = self._client(monkeypatch, [RuntimeError("429"), '{"a": "b"}'])
        assert c.translate_tags(["a"]) == {"a": "b"}

    def test_parse_miss_fails_open(self, monkeypatch):
        c = self._client(monkeypatch, ["not json", "still not json"])
        assert c.translate_tags(["x"]) == {}


class TestTranslateQuery:
    """translate_query widens the cross-lingual retrieval net; it routes through
    _complete_json (retry + trace) and caches per (text, langs)."""

    def _client(self, monkeypatch, responses):
        client = LLMClient.__new__(LLMClient)
        seq = list(responses)
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(client, "_complete_text", fake)
        client._call_count = calls
        return client

    def test_returns_requested_langs_only(self, monkeypatch):
        c = self._client(monkeypatch, ['{"en": "weak solution existence", "fr": "junk"}'])
        out = c.translate_query("弱解存在性", ["en"])
        assert out == {"en": "weak solution existence"}

    def test_drops_empty_values(self, monkeypatch):
        c = self._client(monkeypatch, ['{"en": "  ", "zh": "弱解"}'])
        assert c.translate_query("x", ["en", "zh"]) == {"zh": "弱解"}

    def test_caches_repeat_calls(self, monkeypatch):
        c = self._client(monkeypatch, ['{"en": "cached"}'])
        first = c.translate_query("查詢", ["en"])
        second = c.translate_query("查詢", ["en"])
        assert first == second == {"en": "cached"}
        assert c._call_count["n"] == 1  # second served from cache, no LLM call

    def test_empty_inputs_short_circuit(self, monkeypatch):
        c = self._client(monkeypatch, [])
        assert c.translate_query("", ["en"]) == {}
        assert c.translate_query("x", []) == {}


class TestExtractClaimsAppliesWhen:
    def _client(self, monkeypatch, response):
        client = LLMClient.__new__(LLMClient)
        monkeypatch.setattr(client, "_complete_text", lambda *a, **k: response)
        return client

    def test_parses_applies_when(self, monkeypatch):
        response = json.dumps([{
            "claim": "This claim is definitely long enough to pass.",
            "summary": "s",
            "applies_when": "condition A"
        }])
        res = self._client(monkeypatch, response).extract_claims("text")
        assert res[0]["applies_when"] == "condition A"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── _render_patent_table: LLM-ranking-failure fallback ──────────────

class TestRenderPatentTable:
    def _client(self):
        # Bypass __init__ (no provider/network); _render_patent_table only
        # needs the static _md_cell helper.
        return object.__new__(LLMClient)

    _PATENTS = [
        {"id": "US111", "title": "Alpha", "summary": "a summary", "url": "http://x/1"},
        {"id": "US222", "title": "Beta", "summary": "b summary", "url": "http://x/2"},
    ]

    def test_ranked_rows_render_normally(self):
        c = self._client()
        rows = [{"idx": 1, "relevance": "高", "zh_subject": "主旨", "zh_summary": "摘要"}]
        out = c._render_patent_table(rows, self._PATENTS)
        assert "US222" in out and "高" in out
        assert "US111" not in out            # only the ranked row

    def test_empty_rows_with_patents_falls_back_to_raw(self):
        c = self._client()
        out = c._render_patent_table([], self._PATENTS)
        assert "找不到相關的專利資料" not in out   # never the misleading old message
        assert "US111" in out and "US222" in out  # fetched patents preserved
        assert "未排序" in out                      # labelled as raw/unranked

    def test_empty_rows_no_patents_is_honest(self):
        c = self._client()
        out = c._render_patent_table([], [])
        assert "找不到相關的專利資料" not in out
        assert "查無符合的專利" in out
