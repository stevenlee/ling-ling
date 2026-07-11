"""Tests for services.pipeline_runner — pure logic, no LLM, no daemon."""

import os
from pathlib import Path
from textwrap import dedent

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.pipeline_runner import (
    AdapterRegistry,
    PipelineError,
    PipelineRunner,
    PipelineSpec,
    PipelineStep,
    load_pipeline,
    load_pipeline_from_dict,
    _eval_when,
    _resolve_value,
    _resolve_path,
    _MISSING,
)
from services.builtin_adapters import (
    builtin_adapter_names,
    register_builtin_adapters,
)


# ── Helpers ──────────────────────────────────────────────────────────


class _FakeCapMgr:
    """Stand-in for CapabilityManager: only `get(name)` is needed."""

    def __init__(self, known: set[str]):
        self._known = set(known)

    def get(self, name):
        return object() if name in self._known else None


class _FakeArtifact:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeTraceStore:
    """Records run() opens and artifact writes for assertions."""

    def __init__(self):
        self.runs: list[dict] = []
        self.artifacts: list[dict] = []

    def run(self, **kwargs):
        self.runs.append(kwargs)
        return _FakeRunCtx(f"run_{len(self.runs)}")

    def record_artifact(self, **kwargs):
        self.artifacts.append(kwargs)
        return f"artifact_{len(self.artifacts)}"


class _FakeRunCtx:
    def __init__(self, run_id: str):
        self.run_id = run_id

    def __enter__(self):
        return self.run_id

    def __exit__(self, *exc):
        return None


def _write_pipeline(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "p.yml"
    f.write_text(dedent(content).lstrip("\n"), encoding="utf-8")
    return f


# ── _resolve_value / _resolve_path ──────────────────────────────────


class TestResolveValue:
    ENV = {
        "context": {"title": "Hamlet", "n": 42, "nested": {"k": "v"}},
        "steps": {"synth": {"output": "fake text"}},
    }

    def test_plain_string_passes_through(self):
        assert _resolve_value("just a string", self.ENV) == "just a string"

    def test_non_string_passes_through(self):
        assert _resolve_value(42, self.ENV) == 42
        assert _resolve_value([1, 2], self.ENV) == [1, 2]
        assert _resolve_value(None, self.ENV) is None

    def test_context_placeholder(self):
        assert _resolve_value("${context.title}", self.ENV) == "Hamlet"

    def test_step_output_placeholder(self):
        assert _resolve_value("${steps.synth.output}", self.ENV) == "fake text"

    def test_preserves_non_string_value_type(self):
        # 42 is int in env; placeholder should resolve to int, not "42"
        assert _resolve_value("${context.n}", self.ENV) == 42

    def test_nested_path(self):
        assert _resolve_value("${context.nested.k}", self.ENV) == "v"

    def test_missing_path_raises(self):
        with pytest.raises(PipelineError, match="not found"):
            _resolve_value("${context.does_not_exist}", self.ENV)

    def test_partial_interpolation_not_supported(self):
        # "hello ${name}" — not a pure placeholder, passes through verbatim.
        assert _resolve_value("hello ${context.title}", self.ENV) == "hello ${context.title}"

    def test_path_walks_missing_returns_sentinel(self):
        assert _resolve_path("context.does_not_exist", self.ENV) is _MISSING


# ── _eval_when ──────────────────────────────────────────────────────


class TestEvalWhen:
    ENV = {
        "context": {"present": "yes", "blank": "", "list": [], "filled": [1]},
        "steps": {"a": {"output": "data"}, "b": {"output": ""}},
    }

    def test_none_when_runs(self):
        assert _eval_when(None, self.ENV) is True

    def test_exists(self):
        assert _eval_when({"var": "context.present", "op": "exists"}, self.ENV) is True
        assert _eval_when({"var": "context.absent", "op": "exists"}, self.ENV) is False

    def test_missing(self):
        assert _eval_when({"var": "context.absent", "op": "missing"}, self.ENV) is True
        assert _eval_when({"var": "context.present", "op": "missing"}, self.ENV) is False

    def test_nonempty_string(self):
        assert _eval_when({"var": "context.present", "op": "nonempty"}, self.ENV) is True
        assert _eval_when({"var": "context.blank", "op": "nonempty"}, self.ENV) is False
        assert _eval_when({"var": "context.absent", "op": "nonempty"}, self.ENV) is False

    def test_nonempty_list(self):
        assert _eval_when({"var": "context.filled", "op": "nonempty"}, self.ENV) is True
        assert _eval_when({"var": "context.list", "op": "nonempty"}, self.ENV) is False

    def test_empty(self):
        assert _eval_when({"var": "context.blank", "op": "empty"}, self.ENV) is True
        assert _eval_when({"var": "context.present", "op": "empty"}, self.ENV) is False

    def test_equals_and_not_equals(self):
        assert (
            _eval_when({"var": "context.present", "op": "equals", "value": "yes"}, self.ENV) is True
        )
        assert (
            _eval_when({"var": "context.present", "op": "not_equals", "value": "no"}, self.ENV)
            is True
        )


# ── AdapterRegistry ─────────────────────────────────────────────────


class TestAdapterRegistry:
    def test_register_and_get(self):
        r = AdapterRegistry()
        r.register("x", lambda i: i)
        assert r.has("x")
        assert r.get("x") is not None

    def test_get_unknown_returns_none(self):
        assert AdapterRegistry().get("nope") is None

    def test_register_non_callable_raises(self):
        with pytest.raises(TypeError):
            AdapterRegistry().register("x", "not callable")

    def test_names_sorted(self):
        r = AdapterRegistry()
        r.register("b", lambda i: i)
        r.register("a", lambda i: i)
        assert r.names() == ["a", "b"]


# ── load_pipeline ───────────────────────────────────────────────────


class TestLoadPipeline:
    def test_minimal_valid(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: demo
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """,
        )
        spec = load_pipeline(f)
        assert spec.id == "demo"
        assert len(spec.steps) == 1
        assert spec.steps[0].id == "a"
        assert spec.source_path == f

    def test_id_defaults_to_filename_stem(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """,
        )
        spec = load_pipeline(f)
        assert spec.id == "p"  # filename stem

    def test_full_with_when_clause(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: full
            description: example
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
                inputs:
                  title: "${context.title}"
              - id: b
                capability: critique
                adapter: llm.critique
                when:
                  var: steps.a.output
                  op: nonempty
        """,
        )
        spec = load_pipeline(f)
        assert spec.description == "example"
        assert spec.steps[1].when == {"var": "steps.a.output", "op": "nonempty"}

    def test_malformed_yaml_raises(self, tmp_path):
        f = _write_pipeline(tmp_path, "::: not yaml :::")
        with pytest.raises(PipelineError, match="malformed YAML"):
            load_pipeline(f)

    def test_top_level_must_be_mapping(self, tmp_path):
        f = _write_pipeline(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(PipelineError, match="must be a mapping"):
            load_pipeline(f)

    def test_empty_steps_raises(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: x
            steps: []
        """,
        )
        with pytest.raises(PipelineError, match="non-empty list"):
            load_pipeline(f)

    def test_duplicate_step_id_raises(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: x
            steps:
              - {id: a, capability: c, adapter: ad}
              - {id: a, capability: c, adapter: ad}
        """,
        )
        with pytest.raises(PipelineError, match="duplicate step id"):
            load_pipeline(f)

    def test_missing_capability_raises(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: x
            steps:
              - {id: a, adapter: ad}
        """,
        )
        with pytest.raises(PipelineError, match="missing 'capability'"):
            load_pipeline(f)

    def test_missing_adapter_raises(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: x
            steps:
              - {id: a, capability: c}
        """,
        )
        with pytest.raises(PipelineError, match="missing 'adapter'"):
            load_pipeline(f)

    def test_invalid_when_op_raises(self, tmp_path):
        f = _write_pipeline(
            tmp_path,
            """
            id: x
            steps:
              - id: a
                capability: c
                adapter: ad
                when:
                  var: foo
                  op: wat
        """,
        )
        with pytest.raises(PipelineError, match="when.op"):
            load_pipeline(f)


# ── PipelineRunner end-to-end ───────────────────────────────────────


def _make_demo_spec() -> PipelineSpec:
    return PipelineSpec(
        id="demo",
        description="t",
        steps=(
            PipelineStep(
                id="synth",
                capability="synthesize",
                adapter="llm.synthesize",
                inputs={"title": "${context.title}"},
            ),
            PipelineStep(
                id="critique",
                capability="critique",
                adapter="llm.critique",
                inputs={"candidate": "${steps.synth.output}"},
                when={"var": "steps.synth.output", "op": "nonempty"},
            ),
        ),
    )


class TestPipelineRunnerEndToEnd:
    def test_happy_path_both_steps_run(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": f"SYN[{inp['title']}]"})
        registry.register("llm.critique", lambda inp: {"output": f"CRT[{inp['candidate']}]"})
        trace = _FakeTraceStore()
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
            trace_store=trace,
        )

        result = runner.run(_make_demo_spec(), context={"title": "Hamlet"})
        assert result.status == "succeeded"
        assert result.steps["synth"].status == "succeeded"
        assert result.steps["synth"].output == {"output": "SYN[Hamlet]"}
        assert result.steps["critique"].status == "succeeded"
        assert result.steps["critique"].output == {"output": "CRT[SYN[Hamlet]]"}

    def test_when_false_skips_dependent_step(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": ""})
        registry.register("llm.critique", lambda inp: {"output": "should not run"})
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
        )

        result = runner.run(_make_demo_spec(), context={"title": "X"})
        assert result.status == "succeeded"
        assert result.steps["synth"].status == "succeeded"
        assert result.steps["critique"].status == "skipped"

    def test_unknown_capability_fails_before_any_step(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": "x"})
        registry.register("llm.critique", lambda inp: {"output": "y"})
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize"}),  # critique missing
            adapter_registry=registry,
        )
        with pytest.raises(PipelineError, match="unknown capability"):
            runner.run(_make_demo_spec(), context={"title": "X"})

    def test_unregistered_adapter_fails_before_any_step(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": "x"})
        # llm.critique NOT registered
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
        )
        with pytest.raises(PipelineError, match="unregistered adapter"):
            runner.run(_make_demo_spec(), context={"title": "X"})

    def test_adapter_exception_aborts_pipeline(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: (_ for _ in ()).throw(RuntimeError("boom")))
        registry.register("llm.critique", lambda inp: {"output": "should not run"})
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
        )

        result = runner.run(_make_demo_spec(), context={"title": "X"})
        assert result.status == "failed"
        assert result.steps["synth"].status == "failed"
        assert "boom" in result.steps["synth"].error
        assert "critique" not in result.steps  # not even attempted

    def test_input_resolution_missing_path_fails_step(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": "x"})
        registry.register("llm.critique", lambda inp: {"output": "y"})
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
        )
        # context.title is missing
        result = runner.run(_make_demo_spec(), context={})
        assert result.status == "failed"
        assert result.steps["synth"].status == "failed"
        assert "not found" in result.steps["synth"].error


# ── Trace integration ──────────────────────────────────────────────


class TestTraceIntegration:
    def test_run_id_propagates(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": "syn"})
        registry.register("llm.critique", lambda inp: {"output": "crt"})
        trace = _FakeTraceStore()
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
            trace_store=trace,
        )
        result = runner.run(_make_demo_spec(), context={"title": "X"})
        assert result.run_id == "run_1"
        # Phase 5C: 1 parent run for the pipeline + 1 child run per
        # executed step. Skipped steps do NOT open a child run.
        assert len(trace.runs) == 3
        intents = [r["intent"] for r in trace.runs]
        assert intents == ["pipeline:demo", "step:synth", "step:critique"]
        # Children carry the step metadata so SQL queries can filter.
        assert trace.runs[1]["metadata"]["capability"] == "synthesize"
        assert trace.runs[2]["metadata"]["adapter"] == "llm.critique"

    def test_skipped_step_does_not_open_child_run(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": ""})
        registry.register("llm.critique", lambda inp: {"output": "crt"})
        trace = _FakeTraceStore()
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
            trace_store=trace,
        )
        runner.run(_make_demo_spec(), context={"title": "X"})
        # Parent + 1 child for synth (which ran). The critique step is
        # skipped via the when:nonempty gate, so it gets an artifact but
        # no child run.
        assert len(trace.runs) == 2
        intents = [r["intent"] for r in trace.runs]
        assert intents == ["pipeline:demo", "step:synth"]

    def test_each_step_records_an_artifact(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": "syn"})
        registry.register("llm.critique", lambda inp: {"output": "crt"})
        trace = _FakeTraceStore()
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
            trace_store=trace,
        )
        runner.run(_make_demo_spec(), context={"title": "X"})
        assert len(trace.artifacts) == 2
        first = trace.artifacts[0]
        assert first["artifact_type"] == "pipeline_step_output"
        assert first["title"] == "synth"
        assert first["quality_verdict"] == "succeeded"
        assert first["metadata"]["capability"] == "synthesize"
        assert first["metadata"]["adapter"] == "llm.synthesize"

    def test_skipped_step_records_artifact_with_skipped_verdict(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda inp: {"output": ""})
        registry.register("llm.critique", lambda inp: {"output": "crt"})
        trace = _FakeTraceStore()
        runner = PipelineRunner(
            capability_manager=_FakeCapMgr({"synthesize", "critique"}),
            adapter_registry=registry,
            trace_store=trace,
        )
        runner.run(_make_demo_spec(), context={"title": "X"})
        critique_art = next(a for a in trace.artifacts if a["title"] == "critique")
        assert critique_art["quality_verdict"] == "skipped"


# ── Demo pipeline file loads ────────────────────────────────────────


class TestShippedDemoPipeline:
    def test_demo_pipeline_file_loads_and_validates(self):
        """The .yml that ships under Templates/Pipelines/ must parse cleanly
        and reference real capabilities."""
        from core.config import WIKI_VAULT_DIR, OPERATIONS_DIR, SKILLS_DIR
        from services.capability_manager import CapabilityManager

        demo_path = WIKI_VAULT_DIR / "Templates" / "Pipelines" / "synthesize_critique_demo.yml"
        spec = load_pipeline(demo_path)
        assert spec.id == "synthesize_critique_demo"
        assert {s.capability for s in spec.steps} == {"synthesize", "critique"}

        # Real CapabilityManager: synthesize + critique exist as Operations.
        cap_mgr = CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)
        registry = AdapterRegistry()
        registry.register("llm.synthesize", lambda i: {"output": "syn"})
        registry.register("llm.critique", lambda i: {"output": "crt"})
        runner = PipelineRunner(
            capability_manager=cap_mgr,
            adapter_registry=registry,
        )
        # Validation must pass with the production registry.
        runner.validate(spec)


# ── load_pipeline_from_dict (Phase 5A: lets Planner feed JSON) ──────


class TestLoadPipelineFromDict:
    def test_minimal_valid_dict(self):
        spec = load_pipeline_from_dict(
            {
                "id": "in_memory",
                "steps": [
                    {"id": "a", "capability": "synthesize", "adapter": "llm.synthesize"},
                ],
            }
        )
        assert spec.id == "in_memory"
        assert spec.source_path is None
        assert len(spec.steps) == 1

    def test_default_id_used_when_missing(self):
        spec = load_pipeline_from_dict(
            {
                "steps": [
                    {"id": "a", "capability": "c", "adapter": "ad"},
                ]
            },
            default_id="from_default",
        )
        assert spec.id == "from_default"

    def test_no_id_no_default_raises(self):
        with pytest.raises(PipelineError, match="missing 'id'"):
            load_pipeline_from_dict(
                {
                    "steps": [{"id": "a", "capability": "c", "adapter": "ad"}],
                }
            )

    def test_json_compatible_dict_works(self):
        # JSON-style dict (no YAML-only features) — same loader, no parsing.
        json_like = {
            "id": "from_json",
            "description": "produced by Planner",
            "steps": [
                {
                    "id": "synth",
                    "capability": "synthesize",
                    "adapter": "llm.synthesize",
                    "inputs": {"title": "${context.title}"},
                },
                {
                    "id": "crit",
                    "capability": "critique",
                    "adapter": "llm.critique",
                    "when": {"var": "steps.synth.output", "op": "nonempty"},
                    "inputs": {"candidate": "${steps.synth.output}"},
                },
            ],
        }
        spec = load_pipeline_from_dict(json_like)
        assert len(spec.steps) == 2
        assert spec.steps[1].when == {"var": "steps.synth.output", "op": "nonempty"}

    def test_load_pipeline_delegates_to_from_dict(self, tmp_path):
        # File loader and dict loader produce equivalent specs (except source_path).
        f = _write_pipeline(
            tmp_path,
            """
            id: demo
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """,
        )
        from_file = load_pipeline(f)
        from_dict = load_pipeline_from_dict(
            {
                "id": "demo",
                "steps": [
                    {"id": "a", "capability": "synthesize", "adapter": "llm.synthesize"},
                ],
            },
        )
        assert from_file.id == from_dict.id
        assert from_file.steps == from_dict.steps


# ── builtin_adapters (Phase 5A: real LLM adapters) ──────────────────


class _FakeLLM:
    """Records calls without hitting any provider."""

    def __init__(self):
        self.synthesis_calls: list[dict] = []
        self.critique_calls: list[dict] = []
        self.answer_calls: list[dict] = []
        self.digest_calls: list[dict] = []

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
        return f"SYNTH({title}|{len(part_digests)} parts)"

    def critique_text(self, *, candidate, sources, focus=None):
        self.critique_calls.append(
            {
                "candidate": candidate,
                "sources": sources,
                "focus": focus,
            }
        )
        return f"CRIT({len(candidate)} chars)"

    def answer_query(self, query_content, wiki_context="", **kwargs):
        self.answer_calls.append(
            {
                "query_content": query_content,
                "wiki_context": wiki_context,
                **kwargs,
            }
        )
        return f"ANSWER({len(query_content)}|{len(wiki_context)})"

    def digest_sources(self, *, query, source_title, source_text, budget):
        self.digest_calls.append(
            {
                "query": query,
                "source_title": source_title,
                "source_text": source_text,
                "budget": budget,
            }
        )
        return f"DIGEST({source_title}|{len(source_text)} chars)"


class TestBuiltinAdapters:
    def test_names_are_stable(self):
        assert builtin_adapter_names() == [
            "llm.answer_from_sources",
            "llm.critique",
            "llm.digest_sources",
            "llm.synthesize",
            "vault.load_sources",
            "web.scout_digest",
        ]

    def test_register_populates_registry(self):
        registry = AdapterRegistry()
        registered = register_builtin_adapters(registry, _FakeLLM())
        assert set(registered) == {
            "llm.answer_from_sources",
            "llm.synthesize",
            "llm.critique",
            "llm.digest_sources",
            "vault.load_sources",
            "web.scout_digest",
        }
        assert registry.has("llm.answer_from_sources")
        assert registry.has("llm.synthesize")
        assert registry.has("llm.critique")
        assert registry.has("llm.digest_sources")
        assert registry.has("vault.load_sources")
        assert registry.has("web.scout_digest")

    def test_synthesize_adapter_wires_arguments(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        synth = registry.get("llm.synthesize")

        out = synth(
            {
                "title": "Hamlet",
                "part_digests": [{"part": 1}, {"part": 2}],
                "final_concepts": "carry over",
                "template": "wiki-note",
            }
        )
        assert out == {"output": "SYNTH(Hamlet|2 parts)"}
        assert llm.synthesis_calls == [
            {
                "title": "Hamlet",
                "part_digests": [{"part": 1}, {"part": 2}],
                "final_concepts": "carry over",
                "template": "wiki-note",
            }
        ]

    def test_critique_adapter_wires_arguments(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        crit = registry.get("llm.critique")

        out = crit({"candidate": "ABCDEF", "sources": "src", "focus": "tone"})
        assert out == {"output": "CRIT(6 chars)"}
        assert llm.critique_calls == [
            {
                "candidate": "ABCDEF",
                "sources": "src",
                "focus": "tone",
            }
        ]

    def test_answer_from_sources_adapter_writes_final_answer(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        answer = registry.get("llm.answer_from_sources")

        out = answer({"query": "compare", "sources": "source text", "focus": "actions"})

        assert out == {"output": "ANSWER(23|11)", "final_answer": "ANSWER(23|11)"}
        assert llm.answer_calls[0]["operation"] == "answer_from_sources"
        assert "Focus: actions" in llm.answer_calls[0]["query_content"]

    def test_synthesize_adapter_supplies_defaults_for_missing_inputs(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        synth = registry.get("llm.synthesize")
        # Pipeline DSL may legitimately omit optional inputs; adapter must
        # not KeyError out.
        synth({"title": "X"})
        call = llm.synthesis_calls[0]
        assert call["part_digests"] == []
        assert call["final_concepts"] == ""
        assert call["template"] is None

    def test_load_sources_adapter_resolves_wikilink_titles(self, tmp_path, monkeypatch):
        import services.builtin_adapters as adapters_mod

        pages = tmp_path / "pages"
        book = pages / "Book A"
        book.mkdir(parents=True)
        (book / "Book A (Stitched).md").write_text("stitched source body", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        registry = AdapterRegistry()
        register_builtin_adapters(registry, _FakeLLM())
        load_sources = registry.get("vault.load_sources")

        out = load_sources({"titles": ["[[Book A]]", "[[Missing Book]]"]})

        assert "## Source: Book A" in out["source_text"]
        assert "stitched source body" in out["source_text"]
        assert out["sources"][0]["title"] == "Book A"
        assert out["sources"][0]["source_kind"] == "stitched"
        assert out["sources"][0]["truncated"] is False
        assert out["missing_titles"] == ["Missing Book"]

    def test_load_sources_adapter_reports_truncation_metadata(self, tmp_path, monkeypatch):
        import services.builtin_adapters as adapters_mod

        pages = tmp_path / "pages"
        book = pages / "Long Book"
        book.mkdir(parents=True)
        (book / "Long Book (Stitched).md").write_text("abcdef", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        registry = AdapterRegistry()
        register_builtin_adapters(registry, _FakeLLM())
        load_sources = registry.get("vault.load_sources")

        out = load_sources({"titles": ["Long Book"], "max_chars_per_source": 3})

        source = out["sources"][0]
        assert source["original_chars"] == 6
        assert source["loaded_chars"] > 3  # includes the truncation marker
        assert source["max_chars"] == 3
        assert source["truncated"] is True
        assert "<!-- truncated by vault.load_sources -->" in out["source_text"]

    def test_load_sources_aggregates_parts_when_no_stitched(self, tmp_path, monkeypatch):
        import services.builtin_adapters as adapters_mod

        pages = tmp_path / "pages"
        book = pages / "Book B"
        book.mkdir(parents=True)
        # Create parts, but NO stitched file
        (book / "Book B (Part 1).md").write_text("part 1 text", encoding="utf-8")
        (book / "Book B (Part 2).md").write_text("part 2 text", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        registry = AdapterRegistry()
        register_builtin_adapters(registry, _FakeLLM())
        load_sources = registry.get("vault.load_sources")

        out = load_sources({"titles": ["Book B"]})
        assert "## Source: Book B" in out["source_text"]
        assert "part 1 text" in out["source_text"]
        assert "part 2 text" in out["source_text"]

        source_meta = out["sources"][0]
        assert source_meta["title"] == "Book B"
        assert source_meta["source_kind"] == "parts_aggregated"
        assert source_meta["part_count"] == 2
        assert len(source_meta["paths"]) == 2

    def test_load_sources_prefers_parts_over_synthesis_when_no_stitched(
        self, tmp_path, monkeypatch
    ):
        import services.builtin_adapters as adapters_mod

        pages = tmp_path / "pages"
        book = pages / "Book C"
        book.mkdir(parents=True)
        (book / "Book C (Synthesis).md").write_text("summary only", encoding="utf-8")
        (book / "Book C (Part 1).md").write_text("part 1 text", encoding="utf-8")
        (book / "Book C (Part 2).md").write_text("part 2 text", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        registry = AdapterRegistry()
        register_builtin_adapters(registry, _FakeLLM())
        load_sources = registry.get("vault.load_sources")

        out = load_sources({"titles": ["Book C"]})

        assert "part 1 text" in out["source_text"]
        assert "part 2 text" in out["source_text"]
        assert "summary only" not in out["source_text"]
        assert out["sources"][0]["source_kind"] == "parts_aggregated"

    def test_digest_sources_calls_llm_per_source(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        digest = registry.get("llm.digest_sources")

        # Two source sections in source_text
        source_text = "## Source: Book A\n\ncontent A\n\n---\n\n## Source: Book B\n\ncontent B"
        out = digest({"query": "analyze themes", "sources": source_text, "digest_budget": 100})

        assert "## Digest: Book A" in out["digest_text"]
        assert "DIGEST(Book A|9 chars)" in out["digest_text"]
        assert "## Digest: Book B" in out["digest_text"]
        assert "DIGEST(Book B|9 chars)" in out["digest_text"]
        assert len(llm.digest_calls) == 2
        assert llm.digest_calls[0]["source_title"] == "Book A"
        assert llm.digest_calls[1]["source_title"] == "Book B"
        assert out["source_coverage"][0]["title"] == "Book A"
        assert out["source_coverage"][1]["title"] == "Book B"

    def test_digest_sources_preserves_original_chars_when_truncating(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        digest = registry.get("llm.digest_sources")

        source_text = "## Source: Long Book\n\nabcdef"
        out = digest({"query": "digest", "sources": source_text, "max_source_chars": 3})

        assert llm.digest_calls[0]["source_text"] == "abc"
        assert out["source_digests"][0]["original_chars"] == 6
        assert out["source_digests"][0]["digested_chars"] == 3
        assert out["source_coverage"][0]["original_chars"] == 6
        assert out["source_coverage"][0]["digested_chars"] == 3
        assert out["source_coverage"][0]["truncated_for_digest"] is True

    def test_digest_sources_all_failures_return_empty_digest_text(self):
        class FailingLLM(_FakeLLM):
            def digest_sources(self, *, query, source_title, source_text, budget):
                raise RuntimeError("boom")

        registry = AdapterRegistry()
        register_builtin_adapters(registry, FailingLLM())
        digest = registry.get("llm.digest_sources")

        out = digest({"query": "digest", "sources": "## Source: Book A\n\ncontent"})

        assert out["digest_text"] == ""
        assert out["source_coverage"][0]["has_digest"] is False
        assert "digest failed" in out["warnings"][0]

    def test_demo_pipeline_runs_with_real_adapter_names(self):
        """The shipped demo YAML uses llm.* names. Registering builtin
        adapters against a fake LLM should let the pipeline execute end
        to end, exercising variable resolution + when clause + trace."""
        from core.config import WIKI_VAULT_DIR, OPERATIONS_DIR, SKILLS_DIR
        from services.capability_manager import CapabilityManager

        demo_path = WIKI_VAULT_DIR / "Templates" / "Pipelines" / "synthesize_critique_demo.yml"
        spec = load_pipeline(demo_path)
        cap_mgr = CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)

        runner = PipelineRunner(
            capability_manager=cap_mgr,
            adapter_registry=registry,
            trace_store=_FakeTraceStore(),
        )
        result = runner.run(
            spec,
            context={
                "title": "Hamlet",
                "part_digests": [{"part": 1, "thesis": "x"}],
                "part_digests_text": "(part 1 thesis: x)",
            },
        )
        assert result.status == "succeeded"
        # synthesize fired once, critique fired once (synth output nonempty)
        assert len(llm.synthesis_calls) == 1
        assert len(llm.critique_calls) == 1

    def test_load_sources_critique_demo_pipeline_runs(self, tmp_path, monkeypatch):
        import services.builtin_adapters as adapters_mod
        from core.config import WIKI_VAULT_DIR, OPERATIONS_DIR, SKILLS_DIR
        from services.capability_manager import CapabilityManager

        pages = tmp_path / "pages"
        book = pages / "Book A"
        book.mkdir(parents=True)
        (book / "Book A (Stitched).md").write_text("source body for critique", encoding="utf-8")
        monkeypatch.setattr(adapters_mod, "PAGES_DIR", pages)

        demo_path = WIKI_VAULT_DIR / "Templates" / "Pipelines" / "load_sources_critique_demo.yml"
        spec = load_pipeline(demo_path)
        cap_mgr = CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)

        runner = PipelineRunner(
            capability_manager=cap_mgr,
            adapter_registry=registry,
            trace_store=_FakeTraceStore(),
        )
        result = runner.run(
            spec,
            context={
                "target_titles": ["[[Book A]]"],
                "candidate": "Compare this claim against source.",
                "focus": "source-grounding",
            },
        )

        assert result.status == "succeeded"
        assert result.steps["load_sources"].output["missing_titles"] == []
        assert len(llm.critique_calls) == 1
        assert "source body for critique" in llm.critique_calls[0]["sources"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
