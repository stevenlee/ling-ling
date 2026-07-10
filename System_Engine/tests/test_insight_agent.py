"""Regression tests for the insight-mirror metadata bug.

When InsightAgent writes a report it also drops a copy in Insights/ for
Obsidian browsing. A regression introduced during refactor wrote the
Insights/ copy from just the report body, losing all the
title/type/version/date_created/stats frontmatter that _write_report
adds. For single-strategy runs the frontmatter was partial; for
generate_full_insight it was missing entirely.

These tests pin the contract: the Insights/ copy is byte-identical to
the canonical FROM_LLM_DIR report.
"""

import os
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from agents.insight_agent import InsightAgent
from services.capability_manager import CapabilitySpec


@pytest.fixture
def stub_agent(tmp_path, monkeypatch):
    """An InsightAgent skeleton with both report directories redirected to tmp_path."""
    from core import config

    insights_dir = tmp_path / "Insights"
    from_llm_dir = tmp_path / "fromLingLing"
    insights_dir.mkdir()
    from_llm_dir.mkdir()
    monkeypatch.setattr(config, "FROM_LLM_DIR", from_llm_dir)
    # base_agent imported FROM_LLM_DIR at module load — patch that binding too.
    import agents.base_agent as base_agent_mod

    monkeypatch.setattr(base_agent_mod, "FROM_LLM_DIR", from_llm_dir)

    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = _StubLLM()
    agent.rag = None
    agent.stats = {"input_chars": 0, "output_chars": 0}
    agent.insights_dir = insights_dir
    agent.skills_dir = tmp_path / "Skills"  # unused in these tests
    agent.strategies = {}
    return agent, from_llm_dir, insights_dir


class _StubLLM:
    """Minimal LLM stub: returns deterministic content; satisfies _self_correct calls."""

    model = "stub-model"

    def answer_query(self, *a, **kw):
        return "## Test Content\n\nBody."


class _PlannerCapMgr:
    def __init__(self):
        self._caps = [
            CapabilitySpec(
                name="synthesize",
                type="operation",
                source_path=Path("/fake/synthesize.md"),
                description="combine sources",
                expected_inputs=("title",),
                produces=("synthesis_text",),
                cost_class="medium",
            ),
            CapabilitySpec(
                name="critique",
                type="operation",
                source_path=Path("/fake/critique.md"),
                description="evaluate a candidate",
                expected_inputs=("candidate",),
                produces=("critique_findings",),
                cost_class="low",
            ),
            CapabilitySpec(
                name="answer_from_sources",
                type="operation",
                source_path=Path("/fake/answer_from_sources.md"),
                description="final answer",
                expected_inputs=("query", "sources"),
                produces=("final_answer",),
                cost_class="medium",
            ),
            CapabilitySpec(
                name="load_sources",
                type="operation",
                source_path=Path("/fake/load_sources.md"),
                description="load vault sources",
                expected_inputs=("titles",),
                produces=("source_text", "sources", "missing_titles"),
                cost_class="low",
            ),
            CapabilitySpec(
                name="digest_sources",
                type="operation",
                source_path=Path("/fake/digest_sources.md"),
                description="digest sources",
                expected_inputs=("query", "sources"),
                produces=("digest_text", "source_digests", "source_coverage", "warnings"),
                cost_class="medium",
            ),
        ]
        self._by_name = {c.name: c for c in self._caps}

    def all(self):
        return list(self._caps)

    def get(self, name):
        return self._by_name.get(name)


class _PlannerStubLLM(_StubLLM):
    def __init__(self):
        self.capability_manager = _PlannerCapMgr()
        self.calls = []
        self.synthesis_calls = []
        self.critique_calls = []
        self.answer_from_sources_calls = []

    def answer_query(self, query_content, wiki_context="", **kwargs):
        self.calls.append({"query": query_content, **kwargs})
        return """```json
{
  "id": "insight_compare_preview",
  "description": "Compare then critique",
  "summary": "Build an insight plan without executing it.",
  "steps": [
    {
      "id": "synth",
      "capability": "synthesize",
      "adapter": "llm.synthesize",
      "inputs": {"title": "${context.target_titles}"},
      "rationale": "Create a synthesis before critique."
    },
    {
      "id": "crit",
      "capability": "critique",
      "adapter": "llm.critique",
      "inputs": {"candidate": "${steps.synth.output}"},
      "when": {"var": "steps.synth.output", "op": "nonempty"},
      "rationale": "Check the synthesis for weaknesses."
    }
  ]
}
```"""

    def generate_synthesis(self, *, title, part_digests, final_concepts, template=None, **kwargs):
        self.synthesis_calls.append(
            {
                "title": title,
                "part_digests": part_digests,
                "final_concepts": final_concepts,
                "template": template,
                **kwargs,
            }
        )
        return "SYNTHESIS OUTPUT"

    def critique_text(self, *, candidate, sources, focus=None):
        self.critique_calls.append(
            {
                "candidate": candidate,
                "sources": sources,
                "focus": focus,
            }
        )
        return "CRITIQUE OUTPUT"


class _AnswerPlannerStubLLM(_PlannerStubLLM):
    def answer_query(self, query_content, wiki_context="", **kwargs):
        if kwargs.get("operation") == "answer_from_sources":
            self.answer_from_sources_calls.append(
                {
                    "query_content": query_content,
                    "wiki_context": wiki_context,
                    **kwargs,
                }
            )
            return "FINAL SOURCE-GROUNDED ANSWER"
        self.calls.append({"query": query_content, **kwargs})
        return """```json
{
  "id": "load_then_answer",
  "description": "Load and answer",
  "summary": "Load sources then produce a final answer.",
  "steps": [
    {
      "id": "answer",
      "capability": "answer_from_sources",
      "adapter": "llm.answer_from_sources",
      "inputs": {
        "query": "${context.user_directive}",
        "sources": "source text",
        "focus": "${context.focus}"
      },
      "rationale": "Answer directly from sources."
    }
  ]
}
```"""


class _LoadThenAnswerPlannerStubLLM(_AnswerPlannerStubLLM):
    def answer_query(self, query_content, wiki_context="", **kwargs):
        if kwargs.get("operation") == "answer_from_sources":
            self.answer_from_sources_calls.append(
                {
                    "query_content": query_content,
                    "wiki_context": wiki_context,
                    **kwargs,
                }
            )
            return f"FINAL ANSWER WITH {len(wiki_context)} SOURCE CHARS"
        self.calls.append({"query": query_content, **kwargs})
        return """```json
{
  "id": "load_then_answer",
  "description": "Load and answer",
  "summary": "Load sources then produce a final answer.",
  "steps": [
    {
      "id": "load_sources",
      "capability": "load_sources",
      "adapter": "vault.load_sources",
      "inputs": {
        "titles": "${context.target_titles}",
        "max_chars_per_source": 4
      },
      "rationale": "Load the referenced source."
    },
    {
      "id": "answer",
      "capability": "answer_from_sources",
      "adapter": "llm.answer_from_sources",
      "inputs": {
        "query": "${context.user_directive}",
        "sources": "${steps.load_sources.source_text}"
      },
      "when": {"var": "steps.load_sources.source_text", "op": "nonempty"},
      "rationale": "Answer from loaded source text."
    }
  ]
}
```"""


class _MissingContextPlannerStubLLM(_PlannerStubLLM):
    def answer_query(self, query_content, wiki_context="", **kwargs):
        self.calls.append({"query": query_content, **kwargs})
        return """```json
{
  "id": "needs_source_text",
  "description": "Needs source text",
  "summary": "This plan requires source_text from context.",
  "steps": [
    {
      "id": "crit",
      "capability": "critique",
      "adapter": "llm.critique",
      "inputs": {
        "candidate": "${context.candidate}",
        "sources": "${context.source_text}"
      },
      "rationale": "Critique against provided source text."
    }
  ]
}
```"""


class TestMirrorMetadata:
    def test_mirror_matches_canonical_report_bytes(self, stub_agent):
        """The Insights/ copy must be byte-identical to FROM_LLM_DIR."""
        agent, from_llm_dir, insights_dir = stub_agent

        path, full_markdown = agent._write_report(
            "Test Report",
            "Body of report.",
            "report_insight",
            {"exercise_strategy": "recency", "pipeline": "single"},
        )
        agent._mirror_to_insights(
            full_markdown,
            requested_cmd="insight-recency",
            related_titles=["Test Source"],
        )

        # Canonical report on disk equals the returned full_markdown.
        assert path.read_text(encoding="utf-8") == full_markdown

        # Insights/ copy equals the canonical report — same bytes.
        mirrored = [
            path
            for path in insights_dir.iterdir()
            if path.name.endswith("[Test Source][insight-recency].md")
        ]
        assert len(mirrored) == 1
        assert mirrored[0].read_text(encoding="utf-8") == full_markdown

    def test_mirror_includes_canonical_frontmatter_fields(self, stub_agent):
        """The mirror must keep the fields _write_report adds (title, type,
        version, date_created, input_chars, output_chars), not just the
        caller's partial metadata."""
        agent, _, insights_dir = stub_agent

        _, full_markdown = agent._write_report(
            "Test Report",
            "Body.",
            "report_insight",
            {"exercise_strategy": "recency"},
        )
        agent._mirror_to_insights(full_markdown, requested_cmd="insight-recency")

        mirror = next(insights_dir.iterdir()).read_text(encoding="utf-8")
        assert mirror.startswith("---\n"), "mirror should have YAML frontmatter"
        for field in (
            "title:",
            "type: report_insight",
            "engine_build:",
            "date_created:",
            "input_chars:",
            "output_chars:",
            "exercise_strategy: recency",
        ):
            assert field in mirror, f"mirror missing {field!r}"

    def test_full_insight_mirror_has_frontmatter(self, stub_agent):
        """REGRESSION: generate_full_insight previously passed metadata={} to
        the mirror helper, which fell through to writing the raw body with no
        frontmatter at all. The mirror must always include the canonical
        report frontmatter."""
        agent, _, insights_dir = stub_agent

        # Simulate the no-extra-metadata case generate_full_insight uses.
        _, full_markdown = agent._write_report(
            "全方位洞察報告",
            "Body content.",
            "report_insight_full",
        )
        agent._mirror_to_insights(
            full_markdown,
            requested_cmd="full-insight",
            related_titles=["A", "B"],
        )

        mirror = next(
            path for path in insights_dir.iterdir() if path.name.endswith("[A+B][full-insight].md")
        ).read_text(encoding="utf-8")
        assert mirror.startswith("---\n"), "full-insight mirror lost its frontmatter"
        assert "type: report_insight_full" in mirror
        assert "title:" in mirror
        assert "engine_build:" in mirror

    def test_mirror_filename_uses_datetime_related_doc_and_command(self, stub_agent):
        agent, _, insights_dir = stub_agent

        _, full_markdown = agent._write_report(
            "Test Report",
            "Body.",
            "report_insight",
        )
        agent._mirror_to_insights(
            full_markdown,
            requested_cmd="full-insight",
            related_titles=["Siddhartha", "妙法蓮華經"],
        )

        mirrored = list(insights_dir.iterdir())
        assert len(mirrored) == 1
        assert mirrored[0].name.endswith("[Siddhartha+妙法蓮華經][full-insight].md")

    def test_mirror_filename_uses_vault_when_no_related_doc(self, stub_agent):
        agent, _, insights_dir = stub_agent

        _, full_markdown = agent._write_report(
            "Test Report",
            "Body.",
            "report_insight",
        )
        agent._mirror_to_insights(full_markdown, requested_cmd="full-insight")

        mirrored = list(insights_dir.iterdir())
        assert len(mirrored) == 1
        assert mirrored[0].name.endswith("[Vault][full-insight].md")


class TestPlannerPreview:
    def test_execute_planner_mode_bypasses_existing_insight_pipelines(self, stub_agent):
        agent, _, insights_dir = stub_agent
        agent.llm = _PlannerStubLLM()

        def boom(*_args, **_kwargs):
            raise AssertionError("legacy insight pipeline should not run")

        agent._run_single = boom
        agent._run_montecarlo = boom

        full_markdown = agent.execute(
            {
                "planner_mode": True,
                "user_directive": "@ling-insight planner-mode compare [[A]] and [[B]]",
                "target_titles": ["A", "B"],
            }
        )

        assert "planner_mode: preview" in full_markdown
        assert "type: ins-plan-pre" in full_markdown
        assert "Planner mode preview contains a validated recommended plan" in full_markdown
        assert "## Readiness Check" in full_markdown
        assert "## Preview Handoff" in full_markdown
        assert "passed static readiness checks for guarded execution" in full_markdown
        assert "readiness_verdict: ready" in full_markdown
        assert "readiness_score: 100" in full_markdown
        assert "Step 1: `synth`" in full_markdown
        assert "Step 2: `crit`" in full_markdown
        assert "no pipeline steps were executed" in full_markdown
        mirrored = [
            path
            for path in insights_dir.iterdir()
            if path.name.endswith("[A+B][insight-plan-preview].md")
        ]
        assert len(mirrored) == 1
        assert mirrored[0].read_text(encoding="utf-8") == full_markdown

    def test_execute_flag_runs_when_readiness_is_clean(self, stub_agent):
        agent, _, _ = stub_agent
        llm = _PlannerStubLLM()
        agent.llm = llm

        full_markdown = agent.execute(
            {
                "planner_mode": True,
                "execute_plan": True,
                "user_directive": "@ling-insight planner-mode /execute compare [[A]] and [[B]]",
                "target_titles": ["A", "B"],
            }
        )

        assert "execute_requested: true" in full_markdown
        assert "planner_mode: execute" in full_markdown
        assert "type: ins-plan-exe" in full_markdown
        assert "execution_status: succeeded" in full_markdown
        assert "finality_status: critique_only" in full_markdown
        assert "## Execution Result" in full_markdown
        assert "## Final Step Output" in full_markdown
        assert "SYNTHESIS OUTPUT" in full_markdown
        assert "CRITIQUE OUTPUT" in full_markdown
        assert len(llm.synthesis_calls) == 1
        assert len(llm.critique_calls) == 1

    def test_execute_flag_runs_answer_from_sources_as_final_output(self, stub_agent):
        agent, _, _ = stub_agent
        llm = _AnswerPlannerStubLLM()
        agent.llm = llm

        full_markdown = agent.execute(
            {
                "planner_mode": True,
                "execute_plan": True,
                "user_directive": "@ling-insight planner-mode /execute compare [[A]]",
                "target_titles": ["A"],
            }
        )

        assert "planner_mode: execute" in full_markdown
        assert "finality_status: final_output" in full_markdown
        assert "FINAL SOURCE-GROUNDED ANSWER" in full_markdown
        assert len(llm.answer_from_sources_calls) == 1

    def test_execute_report_includes_loaded_source_appendix(
        self, stub_agent, tmp_path, monkeypatch
    ):
        import services.builtin_adapters as adapters_mod

        pages = tmp_path / "pages"
        book = pages / "Book A"
        book.mkdir(parents=True)
        (book / "Book A (Stitched).md").write_text("abcdef", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        agent, _, _ = stub_agent
        llm = _LoadThenAnswerPlannerStubLLM()
        agent.llm = llm

        full_markdown = agent.execute(
            {
                "planner_mode": True,
                "execute_plan": True,
                "user_directive": "@ling-insight planner-mode /execute compare [[Book A]]",
                "target_titles": ["Book A"],
            }
        )

        assert "planner_mode: execute" in full_markdown
        assert "## Source Appendix" in full_markdown
        assert "| Book A | stitched |" in full_markdown
        assert (
            "| Title | Kind | Loaded chars | Original chars | Truncated | Path |" in full_markdown
        )
        assert "yes" in full_markdown
        assert len(llm.answer_from_sources_calls) == 1
        assert "## Source: Book A" in llm.answer_from_sources_calls[0]["wiki_context"]

    def test_execute_flag_blocks_missing_context_keys(self, stub_agent):
        agent, _, _ = stub_agent
        llm = _MissingContextPlannerStubLLM()
        agent.llm = llm

        full_markdown = agent.execute(
            {
                "planner_mode": True,
                "execute_plan": True,
                "user_directive": "@ling-insight planner-mode /execute critique [[A]]",
                "target_titles": ["A"],
            }
        )

        assert "planner_mode: preview" in full_markdown
        assert "execution_status: blocked_by_execution_gate" in full_markdown
        assert "source_text" in full_markdown
        assert "## Execution Result" not in full_markdown
        assert len(llm.critique_calls) == 0

    def test_planner_preview_uses_plan_operation_axis(self, stub_agent):
        agent, _, _ = stub_agent
        llm = _PlannerStubLLM()
        agent.llm = llm

        agent.execute(
            {
                "planner_mode": True,
                "user_directive": "@ling-insight planner-mode compare notes",
            }
        )

        assert len(llm.calls) == 1
        assert llm.calls[0]["operation"] == "plan"
        assert llm.calls[0]["persona"] == "none"
        assert llm.calls[0]["forced_template"] == "none"


class _LoadDigestAnswerPlannerStubLLM(_AnswerPlannerStubLLM):
    def __init__(self):
        super().__init__()
        self.digest_calls = []

    def digest_sources(self, *, query, source_title, source_text, budget):
        self.digest_calls.append(
            {
                "query": query,
                "source_title": source_title,
                "source_text": source_text,
                "budget": budget,
            }
        )
        return f"DIGEST OF {source_title}"

    def answer_query(self, query_content, wiki_context="", **kwargs):
        if kwargs.get("operation") == "digest_sources":
            return "DIGEST OF BOOK"
        if kwargs.get("operation") == "answer_from_sources":
            self.answer_from_sources_calls.append(
                {
                    "query_content": query_content,
                    "wiki_context": wiki_context,
                    **kwargs,
                }
            )
            return f"FINAL ANSWER WITH {len(wiki_context)} CHARS"
        self.calls.append({"query": query_content, **kwargs})
        return """```json
{
  "id": "load_digest_answer",
  "description": "Load, digest and answer",
  "summary": "Load sources, digest, then produce a final answer.",
  "steps": [
    {
      "id": "load_sources",
      "capability": "load_sources",
      "adapter": "vault.load_sources",
      "inputs": {
        "titles": "${context.target_titles}",
        "max_chars_per_source": 40
      },
      "rationale": "Load the referenced source."
    },
    {
      "id": "digest_sources",
      "capability": "digest_sources",
      "adapter": "llm.digest_sources",
      "inputs": {
        "query": "${context.user_directive}",
        "sources": "${steps.load_sources.source_text}"
      },
      "when": {"var": "steps.load_sources.source_text", "op": "nonempty"},
      "rationale": "Digest the source."
    },
    {
      "id": "answer",
      "capability": "answer_from_sources",
      "adapter": "llm.answer_from_sources",
      "inputs": {
        "query": "${context.user_directive}",
        "sources": "${steps.digest_sources.digest_text}"
      },
      "when": {"var": "steps.digest_sources.digest_text", "op": "nonempty"},
      "rationale": "Answer from loaded source text."
    }
  ]
}
```"""


def test_execute_report_includes_digest_source_appendix(stub_agent, tmp_path, monkeypatch):
    import services.builtin_adapters as adapters_mod

    pages = tmp_path / "pages"
    book_a = pages / "Book A"
    book_a.mkdir(parents=True)
    (book_a / "Book A (Stitched).md").write_text("abcdef", encoding="utf-8")
    book_b = pages / "Book B"
    book_b.mkdir(parents=True)
    (book_b / "Book B (Stitched).md").write_text("ghijk", encoding="utf-8")
    monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

    agent, _, _ = stub_agent
    llm = _LoadDigestAnswerPlannerStubLLM()
    agent.llm = llm

    full_markdown = agent.execute(
        {
            "planner_mode": True,
            "execute_plan": True,
            "user_directive": "@ling-insight planner-mode /execute compare [[Book A]] and [[Book B]]",
            "target_titles": ["Book A", "Book B"],
        }
    )

    assert "planner_mode: execute" in full_markdown
    assert "## Source Appendix" in full_markdown
    assert (
        "| Title | Kind | Loaded chars | Original chars | Truncated | Digest chars | Coverage Warning | Path |"
        in full_markdown
    )
    assert "| Book A | stitched |" in full_markdown
    assert "| Book B | stitched |" in full_markdown
    assert "none" in full_markdown


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── R7-D: targeted-pair fallback must respect the exclude set ───────────


def test_targeted_pairs_fallback_respects_exclude():
    from agents.insight_agent import InsightAgent

    agent = InsightAgent.__new__(InsightAgent)
    T = {"title": "T"}
    A = {"title": "A"}
    # Isolate the pairing/fallback logic from target resolution.
    agent._resolve_target_doc = lambda title, all_docs, title_meta=None: T if title == "T" else None

    # The only possible partner pair (T, A) is excluded → fallback must NOT
    # re-emit it; return empty (caller's stop signal).
    out = agent._build_targeted_pairs(
        [T, A], ["T"], num_pairs=1, exclude={InsightAgent._pair_key(T, A)}
    )
    assert out == []

    # With nothing excluded, the fallback pairs the target with A.
    out2 = agent._build_targeted_pairs([T, A], ["T"], num_pairs=1, exclude=set())
    assert out2 == [(T, A)]


# ── Phase 6: insight learning-artifact auto-attach (flag-gated) ─────────


def test_maybe_artifact_off_returns_empty(monkeypatch):
    from agents.insight_agent import InsightAgent

    monkeypatch.setattr("core.config.settings.VISUAL_ROUTER_ENABLED", False)
    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = object()
    assert agent._maybe_artifact("some insight body") == ""


def test_maybe_artifact_on_wraps_section(monkeypatch):
    from agents.insight_agent import InsightAgent

    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = object()
    monkeypatch.setattr(
        "services.learning_artifacts.maybe_artifact_section",
        lambda llm, content: "## 🖼️ 學習輔助（flowchart）\n\nX\n\n",
    )
    out = agent._maybe_artifact("body")
    assert out.startswith("\n\n---\n\n") and "學習輔助" in out


def test_maybe_artifact_failopen(monkeypatch):
    from agents.insight_agent import InsightAgent

    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = object()

    def boom(llm, content):
        raise RuntimeError("nope")

    monkeypatch.setattr("services.learning_artifacts.maybe_artifact_section", boom)
    assert agent._maybe_artifact("body") == ""


# ── C-track: operation rotation + per-skill stage temperatures ──────────


class TestStageTemperatures:
    def test_defaults_when_frontmatter_absent(self):
        agent = InsightAgent.__new__(InsightAgent)
        assert agent._stage_temp({}, "temp_spark", 0.9) == 0.9

    def test_frontmatter_overrides_and_clamps(self):
        agent = InsightAgent.__new__(InsightAgent)
        assert agent._stage_temp({"temp_expand": 0.85}, "temp_expand", 0.5) == 0.85
        assert agent._stage_temp({"temp_expand": "0.7"}, "temp_expand", 0.5) == 0.7
        assert agent._stage_temp({"temp_expand": 99}, "temp_expand", 0.5) == 1.5  # clamp
        assert agent._stage_temp({"temp_expand": "hot"}, "temp_expand", 0.5) == 0.5  # invalid

    def test_generate_insight_sets_per_run_temps(self, stub_agent, monkeypatch):
        agent, _, _ = stub_agent
        monkeypatch.setattr(
            InsightAgent, "_check_skill_preconditions", lambda self, aw: [], raising=False
        )
        agent.strategies = {
            "hotop": {
                "name": "hotop",
                "description": "d",
                "pipeline": "single",
                "system_prompt": "lens",
                "temp_spark": 0.95,
                "temp_synthesize": 0.8,
            }
        }
        monkeypatch.setattr(
            InsightAgent, "_run_single", lambda self, c, u, t=None: "## body", raising=False
        )
        monkeypatch.setattr(InsightAgent, "_signals_meta", lambda self, c, t: {}, raising=False)
        monkeypatch.setattr(InsightAgent, "_maybe_artifact", lambda self, c: "", raising=False)
        agent.generate_insight("hotop")
        assert agent._temp_spark == 0.95
        assert agent._temp_expand == 0.5  # untouched → class default
        assert agent._temp_synthesize == 0.8


class TestOperationLens:
    def test_lens_from_skill_body(self):
        from agents.insight.monte_carlo import MonteCarloMixin

        lens = MonteCarloMixin._operation_lens({"system_prompt": "Act as a Fabulist."})
        assert "Operation Lens" in lens and "Fabulist" in lens
        assert MonteCarloMixin._operation_lens({}) == ""
        assert MonteCarloMixin._operation_lens(None) == ""

    def test_spark_prompt_carries_lens(self, stub_agent):
        agent, _, _ = stub_agent

        calls = []

        class _Recorder:
            model = "stub"

            def answer_query(self, query_content, wiki_context="", **kw):
                calls.append(kw)
                return '{"idea": "x", "novelty_score": 7, "reasoning": "r", "source_a": "A", "source_b": "B"}'

        agent.llm = _Recorder()
        doc = {"title": "A", "tags": [], "content": "aaa"}
        doc_b = {"title": "B", "tags": [], "content": "bbb"}
        agent._spark_seed(doc, doc_b, {"system_prompt": "Act as a Fabulist."})
        assert "Fabulist" in calls[0]["custom_instruction"]


class TestCreativeMode:
    """report_mode: creative — lens-first expand + lean report (fable/dialogue)."""

    def test_is_creative_flag(self):
        from agents.insight.monte_carlo import MonteCarloMixin

        assert MonteCarloMixin._is_creative({"report_mode": "creative"}) is True
        assert MonteCarloMixin._is_creative({"report_mode": "analytical"}) is False
        assert MonteCarloMixin._is_creative({}) is False
        assert MonteCarloMixin._is_creative(None) is False

    def _expand_agent(self, capture):
        agent = InsightAgent.__new__(InsightAgent)

        class _RAG:
            def query_similar_notes(self, idea, top_k=5):
                return []

        class _LLM:
            def answer_query(self, query_content, wiki_context="", **kw):
                capture["prompt"] = kw.get("custom_instruction", "")
                return "EXPANDED BODY"

        agent.rag = _RAG()
        agent.llm = _LLM()
        agent._load_prompt = lambda name, required=False: "BASE"
        agent._should_ground = lambda idea: False
        return agent

    def test_creative_expand_drops_analytical_scaffold(self):
        cap = {}
        agent = self._expand_agent(cap)
        agent._expand_seed(
            {"idea": "x", "source_a": "A", "source_b": "B", "reasoning": "r"},
            {"report_mode": "creative", "system_prompt": "Act as a Fabulist. ## Expansion 寫寓言"},
        )
        p = cap["prompt"]
        assert "Fabulist" in p  # lens present
        assert "thesis statement" not in p.lower()  # analytical scaffold dropped
        assert "Practical implications" not in p

    def test_analytical_expand_keeps_scaffold(self):
        cap = {}
        agent = self._expand_agent(cap)
        agent._expand_seed(
            {"idea": "x", "source_a": "A", "source_b": "B", "reasoning": "r"},
            {"system_prompt": "Act as an Epistemologist."},
        )
        p = cap["prompt"]
        assert "thesis statement" in p.lower()  # unchanged analytical path
        assert "Epistemologist" in p

    def test_creative_report_is_lean(self):
        agent = InsightAgent.__new__(InsightAgent)

        class _LLM:
            def answer_query(self, query_content, wiki_context="", **kw):
                return "CLOSING NOTE"

            def _get_lang_hint(self):
                return "Traditional Chinese"

        agent.llm = _LLM()
        rounds = [
            {
                "round": 1,
                "pairs_tried": 2,
                "seeds": 2,
                "expanded": [
                    {"expanded": "從前有一座晶片圖書館……", "source_a": "A", "source_b": "B"}
                ],
            }
        ]
        out = agent._synthesize_multi_round(
            rounds, {"name": "fable", "report_mode": "creative", "system_prompt": "Fabulist"}, ""
        )
        # No montecarlo scaffolding
        assert "Monte Carlo" not in out
        assert "Round Scorecard" not in out
        assert "生產力" not in out
        # Creative artifact emitted directly + light closing
        assert "從前有一座晶片圖書館" in out
        assert "綜合短評" in out and "CLOSING NOTE" in out

    def test_creative_closing_strips_headers(self):
        # The model sometimes ignores "2-3 sentences" and re-emits a whole
        # 火花/擴張/綜合 piece with headers — those must be stripped so the
        # closing can't reintroduce section scaffolding.
        agent = InsightAgent.__new__(InsightAgent)

        class _LLM:
            def answer_query(self, query_content, wiki_context="", **kw):
                # ensure the closing prompt no longer injects the full lens
                assert "## Operation Lens" not in kw.get("custom_instruction", "")
                return "## 編輯短評\n第 1 則最成功。\n## 火花\n不該出現的重跑段落。"

            def _get_lang_hint(self):
                return "Traditional Chinese"

        agent.llm = _LLM()
        note = agent._creative_closing(
            [{"source_a": "A", "source_b": "B"}],
            {"name": "fable", "description": "d", "system_prompt": "Fabulist ## Expansion ..."},
        )
        assert "#" not in note  # all header lines stripped
        assert "第 1 則最成功" in note


class TestRotation:
    def _strategies(self):
        return {n: {"name": n} for n in ("montecarlo", "counterfactual", "fable")}

    def test_deterministic_cycle_by_date(self, monkeypatch):
        from datetime import date

        from maintenance.daily_insight import pick_rotation_strategy

        monkeypatch.setattr(
            "core.config.settings.INSIGHT_ROTATION",
            "montecarlo,counterfactual,fable",
            raising=False,
        )
        picks = [
            pick_rotation_strategy(self._strategies(), today=date.fromordinal(730000 + i))
            for i in range(6)
        ]
        assert picks == picks[3:] + picks[:3] or len(set(picks[:3])) == 3  # full cycle, no repeat
        assert picks[0] == picks[3] and picks[1] == picks[4]  # period 3

    def test_unknown_names_skipped_and_fallback(self, monkeypatch):
        from maintenance.daily_insight import pick_rotation_strategy

        monkeypatch.setattr("core.config.settings.INSIGHT_ROTATION", "typo,fable", raising=False)
        assert pick_rotation_strategy(self._strategies()) == "fable"
        monkeypatch.setattr("core.config.settings.INSIGHT_ROTATION", "typo,also-bad", raising=False)
        assert pick_rotation_strategy(self._strategies()) == "montecarlo"
        monkeypatch.setattr("core.config.settings.INSIGHT_ROTATION", "", raising=False)
        assert pick_rotation_strategy(self._strategies()) == "montecarlo"
