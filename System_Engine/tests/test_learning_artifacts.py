"""Phase 6: learning-artifact router + @ling-visualize agent."""

import os

import pytest

os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.learning_artifacts import build_artifact, classify_structure


class FakeLLM:
    def __init__(self, classify=None, completion=""):
        self._classify = classify if classify is not None else {}
        self._completion = completion

    def _complete_json(self, *, kind, system_prompt, user_msg, **kw):
        return self._classify

    def complete(self, system_prompt, user_msg, **kw):
        return self._completion


# ── classify ───────────────────────────────────────────────────────────


def test_classify_valid_type():
    llm = FakeLLM(classify={"type": "timeline", "confidence": 0.9, "reason": "歷史演進"})
    out = classify_structure(llm, "1990... 2000... 2010...")
    assert out["type"] == "timeline" and out["confidence"] == 0.9


def test_classify_invalid_type_falls_to_none():
    llm = FakeLLM(classify={"type": "banana", "confidence": 0.9})
    assert classify_structure(llm, "x")["type"] == "none"


def test_table_renderer_rejects_reasoning_scratchpad():
    from services.learning_artifacts import _render_table

    llm = FakeLLM(completion="* Input: compare methods\n* Goal: create a table\n* Final check")

    assert _render_table(llm, "content") == ""
    with pytest.raises(ValueError, match="invalid comparison_table"):
        _render_table(llm, "content", strict=True)


def test_table_renderer_accepts_one_table_and_outer_markdown_fence():
    from services.learning_artifacts import _render_table

    llm = FakeLLM(completion="```markdown\n| Method | Bound |\n| :--- | ---: |\n| A | $|x|$ |\n```")

    assert _render_table(llm, "content") == ("| Method | Bound |\n| :--- | ---: |\n| A | $|x|$ |")


# ── classify top-2 (ranked) ──────────────────────────────────────────────


def test_classify_structures_ranked():
    from services.learning_artifacts import classify_structures

    llm = FakeLLM(
        classify={
            "ranked": [
                {"type": "flowchart", "confidence": 0.9, "reason": "流程"},
                {"type": "mindmap", "confidence": 0.7, "reason": "階層"},
            ]
        }
    )
    out = classify_structures(llm, "x")
    assert [c["type"] for c in out] == ["flowchart", "mindmap"]


def test_classify_structures_drops_none_when_real_present():
    from services.learning_artifacts import classify_structures

    llm = FakeLLM(
        classify={
            "ranked": [
                {"type": "timeline", "confidence": 0.8},
                {"type": "none", "confidence": 0.5},
            ]
        }
    )
    assert [c["type"] for c in classify_structures(llm, "x")] == ["timeline"]


def test_classify_structures_legacy_single_dict():
    # Back-compat: a legacy {"type": ...} reply still yields a 1-item ranking.
    from services.learning_artifacts import classify_structures

    llm = FakeLLM(classify={"type": "quadrant", "confidence": 0.8})
    assert [c["type"] for c in classify_structures(llm, "x")] == ["quadrant"]


def test_classify_structures_dedups():
    from services.learning_artifacts import classify_structures

    llm = FakeLLM(
        classify={
            "ranked": [
                {"type": "flowchart", "confidence": 0.9},
                {"type": "flowchart", "confidence": 0.6},
            ]
        }
    )
    assert [c["type"] for c in classify_structures(llm, "x")] == ["flowchart"]


def test_ontology_is_only_kept_as_high_confidence_primary():
    from services.learning_artifacts import classify_structures

    secondary = FakeLLM(
        classify={
            "ranked": [
                {"type": "mindmap", "confidence": 0.95},
                {"type": "ontology", "confidence": 0.99},
                {"type": "comparison_table", "confidence": 0.8},
            ]
        }
    )
    assert [c["type"] for c in classify_structures(secondary, "x", limit=3)] == [
        "mindmap",
        "comparison_table",
    ]

    weak_primary = FakeLLM(classify={"ranked": [{"type": "ontology", "confidence": 0.89}]})
    assert classify_structures(weak_primary, "x")[0]["type"] == "none"

    strong_primary = FakeLLM(classify={"ranked": [{"type": "ontology", "confidence": 0.9}]})
    assert classify_structures(strong_primary, "x")[0]["type"] == "ontology"


# ── build_artifact ───────────────────────────────────────────────────────


def test_forced_table_renders_table():
    llm = FakeLLM(completion="| A | B |\n|---|---|\n| 1 | 2 |")
    out = build_artifact(llm, "compare A and B", forced_type="comparison_table")
    assert out["type"] == "comparison_table"
    assert "| A | B |" in out["artifact"]


def test_none_yields_no_artifact():
    llm = FakeLLM(classify={"type": "none", "confidence": 0.0, "reason": "散文"})
    out = build_artifact(llm, "some rambling prose with no structure")
    assert out["type"] == "none" and out["artifact"] == ""


def test_mermaid_block_extracted_and_kept():
    llm = FakeLLM(
        classify={"type": "flowchart", "confidence": 0.8},
        completion='這是說明\n```mermaid\nflowchart TD\n  A["start"] --> B["end"]\n```\n後記',
    )
    out = build_artifact(llm, "first do A then B")
    assert out["type"] == "flowchart"
    assert out["artifact"].startswith("```mermaid") and "flowchart TD" in out["artifact"]


def test_mermaid_failure_returns_empty_artifact():
    # No mermaid block in the reply → validation drops it (no broken diagram).
    llm = FakeLLM(classify={"type": "mindmap", "confidence": 0.8}, completion="sorry no diagram")
    out = build_artifact(llm, "decompose this topic")
    assert out["type"] == "mindmap" and out["artifact"] == ""


def test_empty_content_is_none():
    out = build_artifact(FakeLLM(), "   ")
    assert out["type"] == "none"


def test_mermaid_kind_mismatch_rejected():
    # Asked for a mindmap, model returned a flowchart → validation drops it
    # rather than emitting a diagram of the wrong type.
    llm = FakeLLM(
        classify={"type": "mindmap", "confidence": 0.8},
        completion="```mermaid\nflowchart TD\n  A --> B\n```",
    )
    out = build_artifact(llm, "decompose this topic")
    assert out["type"] == "mindmap" and out["artifact"] == ""


def test_mermaid_kind_match_accepted():
    llm = FakeLLM(
        classify={"type": "mindmap", "confidence": 0.8},
        completion='```mermaid\nmindmap\n  root(("主題"))\n    分支A\n```',
    )
    out = build_artifact(llm, "decompose this topic")
    assert out["artifact"].startswith("```mermaid") and "mindmap" in out["artifact"]


def test_validate_mermaid_rejects_single_line():
    from services.learning_artifacts import _validate_mermaid

    assert _validate_mermaid("```mermaid\nflowchart TD\n```", "flowchart") is False


def test_validate_mermaid_rejects_leaked_metatext():
    from services.learning_artifacts import _validate_mermaid

    block = (
        "```mermaid\nclassDiagram\n    class A\n"
        "    ModelFree^... (Wait, I'll just write the final code block)\n```"
    )
    assert _validate_mermaid(block, "ontology") is False


def test_metatext_detector_high_signal_only():
    from services.learning_artifacts import mermaid_has_metatext

    # real leaks are caught
    assert mermaid_has_metatext("x (Wait, I'll just ...)")
    assert mermaid_has_metatext("here's the final code block")
    assert mermaid_has_metatext("As an AI, I cannot draw this")
    # ordinary diagram content is NOT flagged
    assert not mermaid_has_metatext('A["使用者請求"] --> B["最終結果"]')
    assert not mermaid_has_metatext('class Willingness["意願"]')


# ── VisualizeAgent ───────────────────────────────────────────────────────


def test_load_note_resolution(tmp_path, monkeypatch):
    import agents.visualize_agent as va

    pages = tmp_path / "pages"
    (pages / "MyDoc").mkdir(parents=True)
    (pages / "MyDoc" / "MyDoc (Synthesis).md").write_text("synthesis body", encoding="utf-8")
    monkeypatch.setattr(va, "PAGES_DIR", pages)
    monkeypatch.setattr(va, "WIKI_VAULT_DIR", tmp_path)
    agent = va.VisualizeAgent.__new__(va.VisualizeAgent)
    text, src = agent._load_note("MyDoc")
    assert text == "synthesis body" and "Synthesis" in src


def test_execute_parses_wikilink_and_forced_type(tmp_path, monkeypatch):
    import agents.visualize_agent as va

    agent = va.VisualizeAgent.__new__(va.VisualizeAgent)
    agent.llm = object()
    agent._load_note = lambda title: ("doc text", "src")
    agent._write_report = lambda t, body, rtype, meta=None: (None, body)
    captured = {}
    monkeypatch.setattr(
        va,
        "build_artifact",
        lambda llm, text, forced_type=None: (
            captured.update(forced=forced_type)
            or {"type": forced_type or "none", "reason": "r", "artifact": "ART"}
        ),
    )
    out = agent.execute({"user_directive": "@ling-visualize [[Some Doc]] as timeline"})
    assert captured["forced"] == "timeline"
    assert "ART" in out


def test_render_none_explains(tmp_path):
    from agents.visualize_agent import VisualizeAgent

    agent = VisualizeAgent.__new__(VisualizeAgent)
    body = agent._render("X", "src", {"type": "none", "reason": "散文", "artifact": ""})
    assert "沒有明顯的視覺結構" in body


# ── auto-attach (flag-gated) ─────────────────────────────────────────────


def test_maybe_section_off_by_default(monkeypatch):
    from services.learning_artifacts import maybe_artifact_section

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", False)
    called = {"n": 0}

    class L:
        def _complete_json(self, **kw):
            called["n"] += 1
            return {"type": "flowchart", "confidence": 0.9}

        def complete(self, *a, **k):
            called["n"] += 1
            return "x"

    # Flag off → empty string AND zero LLM calls (byte-identical callers).
    assert maybe_artifact_section(L(), "some content") == ""
    assert called["n"] == 0


def test_maybe_section_on_attaches(monkeypatch):
    from services.learning_artifacts import maybe_artifact_section

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", True)
    llm = FakeLLM(completion="| A | B |\n|---|---|\n| 1 | 2 |")
    # force-via-classify: classify returns comparison_table, render returns table
    llm._classify = {"type": "comparison_table", "confidence": 0.9}
    section = maybe_artifact_section(llm, "compare A and B")
    assert section.startswith("## 🖼️ 學習輔助")
    assert "| A | B |" in section


def test_maybe_section_none_attaches_nothing(monkeypatch):
    from services.learning_artifacts import maybe_artifact_section

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", True)
    llm = FakeLLM(classify={"type": "none", "confidence": 0.0})
    assert maybe_artifact_section(llm, "unstructured prose") == ""


def test_maybe_section_emits_two_when_two_render(monkeypatch):
    import services.learning_artifacts as la

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", True)
    monkeypatch.setattr(
        la,
        "build_artifact_section_outcome",
        lambda llm, content, **_: la.ArtifactSectionOutcome(
            "complete",
            section=(
                "## 🖼️ 學習輔助（flowchart）\n```mermaid\nflowchart TD\nA-->B\n```\n"
                "## 🖼️ 學習輔助（mindmap）\n```mermaid\nmindmap\n  root((X))\n```\n"
            ),
            artifact_types=("flowchart", "mindmap"),
        ),
    )
    section = la.maybe_artifact_section(object(), "content")
    assert section.count("## 🖼️ 學習輔助") == 2
    assert "（flowchart）" in section and "（mindmap）" in section


def test_auto_attach_prefers_table_as_complement_and_caps_at_two(monkeypatch):
    import services.learning_artifacts as la

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", True)
    llm = FakeLLM(
        classify={
            "ranked": [
                {"type": "mindmap", "confidence": 0.95},
                {"type": "argument_map", "confidence": 0.9},
                {"type": "comparison_table", "confidence": 0.85},
            ]
        }
    )
    monkeypatch.setattr(
        la,
        "_render_for_type",
        lambda llm, content, artifact_type, strict=False: f"rendered {artifact_type}",
    )

    outcome = la.maybe_artifact_section(llm, "content", limit=2, return_outcome=True)

    assert outcome.status == "complete"
    assert outcome.artifact_types == ("mindmap", "comparison_table")
    assert "argument_map" not in outcome.section


def test_operational_artifact_failure_is_deferred_not_skipped(monkeypatch):
    import services.learning_artifacts as la

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", True)

    class Broken:
        def _complete_json(self, **kwargs):
            raise TimeoutError("provider timeout")

    outcome = la.maybe_artifact_section(Broken(), "content", return_outcome=True)

    assert outcome.status == "deferred"
    assert "provider timeout" in outcome.detail


def test_artifact_job_budget_keeps_completed_primary_and_defers_complement(monkeypatch):
    import services.learning_artifacts as la

    ranked = [
        {"type": "mindmap", "confidence": 0.95},
        {"type": "comparison_table", "confidence": 0.9},
    ]
    monkeypatch.setattr(la, "classify_structures", lambda *args, **kwargs: ranked)
    rendered = []

    def render(_llm, _content, artifact_type, *, strict=False):
        rendered.append(artifact_type)
        return f"rendered {artifact_type}"

    ticks = iter((0.0, 1.0, 601.0))
    monkeypatch.setattr(la, "_render_for_type", render)
    monkeypatch.setattr(la.time, "monotonic", lambda: next(ticks))

    outcome = la.build_artifact_section_outcome(object(), "content", limit=2)

    assert outcome.status == "complete"
    assert outcome.artifact_types == ("mindmap",)
    assert rendered == ["mindmap"]
    assert "comparison_table: deferred after 601.0s job budget" in outcome.detail
