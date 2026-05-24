"""Tests for services.pipeline_runner — pure logic, no LLM, no daemon."""
import os
import sys
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
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
        assert _eval_when({"var": "context.present", "op": "equals",
                            "value": "yes"}, self.ENV) is True
        assert _eval_when({"var": "context.present", "op": "not_equals",
                            "value": "no"}, self.ENV) is True


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
        f = _write_pipeline(tmp_path, """
            id: demo
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """)
        spec = load_pipeline(f)
        assert spec.id == "demo"
        assert len(spec.steps) == 1
        assert spec.steps[0].id == "a"
        assert spec.source_path == f

    def test_id_defaults_to_filename_stem(self, tmp_path):
        f = _write_pipeline(tmp_path, """
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """)
        spec = load_pipeline(f)
        assert spec.id == "p"  # filename stem

    def test_full_with_when_clause(self, tmp_path):
        f = _write_pipeline(tmp_path, """
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
        """)
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
        f = _write_pipeline(tmp_path, """
            id: x
            steps: []
        """)
        with pytest.raises(PipelineError, match="non-empty list"):
            load_pipeline(f)

    def test_duplicate_step_id_raises(self, tmp_path):
        f = _write_pipeline(tmp_path, """
            id: x
            steps:
              - {id: a, capability: c, adapter: ad}
              - {id: a, capability: c, adapter: ad}
        """)
        with pytest.raises(PipelineError, match="duplicate step id"):
            load_pipeline(f)

    def test_missing_capability_raises(self, tmp_path):
        f = _write_pipeline(tmp_path, """
            id: x
            steps:
              - {id: a, adapter: ad}
        """)
        with pytest.raises(PipelineError, match="missing 'capability'"):
            load_pipeline(f)

    def test_missing_adapter_raises(self, tmp_path):
        f = _write_pipeline(tmp_path, """
            id: x
            steps:
              - {id: a, capability: c}
        """)
        with pytest.raises(PipelineError, match="missing 'adapter'"):
            load_pipeline(f)

    def test_invalid_when_op_raises(self, tmp_path):
        f = _write_pipeline(tmp_path, """
            id: x
            steps:
              - id: a
                capability: c
                adapter: ad
                when:
                  var: foo
                  op: wat
        """)
        with pytest.raises(PipelineError, match="when.op"):
            load_pipeline(f)


# ── PipelineRunner end-to-end ───────────────────────────────────────


def _make_demo_spec() -> PipelineSpec:
    return PipelineSpec(
        id="demo",
        description="t",
        steps=(
            PipelineStep(
                id="synth", capability="synthesize", adapter="llm.synthesize",
                inputs={"title": "${context.title}"},
            ),
            PipelineStep(
                id="critique", capability="critique", adapter="llm.critique",
                inputs={"candidate": "${steps.synth.output}"},
                when={"var": "steps.synth.output", "op": "nonempty"},
            ),
        ),
    )


class TestPipelineRunnerEndToEnd:
    def test_happy_path_both_steps_run(self):
        registry = AdapterRegistry()
        registry.register("llm.synthesize",
                          lambda inp: {"output": f"SYN[{inp['title']}]"})
        registry.register("llm.critique",
                          lambda inp: {"output": f"CRT[{inp['candidate']}]"})
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
        registry.register("llm.synthesize",
                          lambda inp: (_ for _ in ()).throw(RuntimeError("boom")))
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
        assert len(trace.runs) == 1
        assert trace.runs[0]["intent"] == "pipeline:demo"
        assert trace.runs[0]["agent"] == "pipeline_runner"

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
        spec = load_pipeline_from_dict({
            "id": "in_memory",
            "steps": [
                {"id": "a", "capability": "synthesize", "adapter": "llm.synthesize"},
            ],
        })
        assert spec.id == "in_memory"
        assert spec.source_path is None
        assert len(spec.steps) == 1

    def test_default_id_used_when_missing(self):
        spec = load_pipeline_from_dict(
            {"steps": [
                {"id": "a", "capability": "c", "adapter": "ad"},
            ]},
            default_id="from_default",
        )
        assert spec.id == "from_default"

    def test_no_id_no_default_raises(self):
        with pytest.raises(PipelineError, match="missing 'id'"):
            load_pipeline_from_dict({
                "steps": [{"id": "a", "capability": "c", "adapter": "ad"}],
            })

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
        f = _write_pipeline(tmp_path, """
            id: demo
            steps:
              - id: a
                capability: synthesize
                adapter: llm.synthesize
        """)
        from_file = load_pipeline(f)
        from_dict = load_pipeline_from_dict(
            {"id": "demo", "steps": [
                {"id": "a", "capability": "synthesize", "adapter": "llm.synthesize"},
            ]},
        )
        assert from_file.id == from_dict.id
        assert from_file.steps == from_dict.steps


# ── builtin_adapters (Phase 5A: real LLM adapters) ──────────────────


class _FakeLLM:
    """Records calls without hitting any provider."""

    def __init__(self):
        self.synthesis_calls: list[dict] = []
        self.critique_calls: list[dict] = []

    def generate_synthesis(self, *, title, part_digests, final_concepts, template=None):
        self.synthesis_calls.append({
            "title": title,
            "part_digests": part_digests,
            "final_concepts": final_concepts,
            "template": template,
        })
        return f"SYNTH({title}|{len(part_digests)} parts)"

    def critique_text(self, *, candidate, sources, focus=None):
        self.critique_calls.append({
            "candidate": candidate,
            "sources": sources,
            "focus": focus,
        })
        return f"CRIT({len(candidate)} chars)"


class TestBuiltinAdapters:
    def test_names_are_stable(self):
        assert builtin_adapter_names() == ["llm.critique", "llm.synthesize"]

    def test_register_populates_registry(self):
        registry = AdapterRegistry()
        registered = register_builtin_adapters(registry, _FakeLLM())
        assert set(registered) == {"llm.synthesize", "llm.critique"}
        assert registry.has("llm.synthesize")
        assert registry.has("llm.critique")

    def test_synthesize_adapter_wires_arguments(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        synth = registry.get("llm.synthesize")

        out = synth({
            "title": "Hamlet",
            "part_digests": [{"part": 1}, {"part": 2}],
            "final_concepts": "carry over",
            "template": "wiki-note",
        })
        assert out == {"output": "SYNTH(Hamlet|2 parts)"}
        assert llm.synthesis_calls == [{
            "title": "Hamlet",
            "part_digests": [{"part": 1}, {"part": 2}],
            "final_concepts": "carry over",
            "template": "wiki-note",
        }]

    def test_critique_adapter_wires_arguments(self):
        registry = AdapterRegistry()
        llm = _FakeLLM()
        register_builtin_adapters(registry, llm)
        crit = registry.get("llm.critique")

        out = crit({"candidate": "ABCDEF", "sources": "src", "focus": "tone"})
        assert out == {"output": "CRIT(6 chars)"}
        assert llm.critique_calls == [{
            "candidate": "ABCDEF", "sources": "src", "focus": "tone",
        }]

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

    def test_demo_pipeline_runs_with_real_adapter_names(self):
        """The shipped demo YAML uses llm.* names. Registering builtin
        adapters against a fake LLM should let the pipeline execute end
        to end, exercising variable resolution + when clause + trace."""
        from core.config import WIKI_VAULT_DIR, OPERATIONS_DIR, SKILLS_DIR
        from services.capability_manager import CapabilityManager

        demo_path = (
            WIKI_VAULT_DIR / "Templates" / "Pipelines"
            / "synthesize_critique_demo.yml"
        )
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
        result = runner.run(spec, context={
            "title": "Hamlet",
            "part_digests": [{"part": 1, "thesis": "x"}],
            "part_digests_text": "(part 1 thesis: x)",
        })
        assert result.status == "succeeded"
        # synthesize fired once, critique fired once (synth output nonempty)
        assert len(llm.synthesis_calls) == 1
        assert len(llm.critique_calls) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
