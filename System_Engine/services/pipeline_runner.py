"""Phase 4.5 PipelineRunner — dry-run skeleton.

A declarative pipeline executor that:

  1. Reads a YAML pipeline spec (`lings-desktop/Templates/Pipelines/*.yml`).
  2. Validates that each step's `capability:` exists in CapabilityManager
     and its `adapter:` exists in the AdapterRegistry — BEFORE any step
     runs (fail-fast).
  3. Resolves `${context.X}` / `${steps.<id>.<key>}` placeholders into
     adapter inputs.
  4. Evaluates structured `when:` conditions ({var, op, value?}) to decide
     whether each step runs or is skipped.
  5. Invokes the adapter callable for each step and records per-step
     telemetry as artifacts under a single `pipeline_run_id` in the
     TraceStore.

Architectural constraint (see [[adapter_layer_constraint]]): PipelineRunner
NEVER calls production private methods. The only way a capability becomes
executable is by registering an explicit adapter in the runtime registry.
This keeps the runner an experimental rig — no implicit coupling to the
ingestion main flow.

Phase 4.5 ships fixture-only adapter support. Real adapters land in 4.6.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml


# ─── Errors ───────────────────────────────────────────────────────────


class PipelineError(Exception):
    """Raised for any pipeline-level problem (spec invalid, step failed)."""


# ─── Adapter registry ────────────────────────────────────────────────


Adapter = Callable[[dict], dict]


class AdapterRegistry:
    """Named registry mapping `adapter:` strings to runtime callables.

    The registry is the ONLY place where capability metadata becomes
    executable Python. Nothing in the runner looks up callables by class
    + method name.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, name: str, fn: Adapter) -> None:
        if not callable(fn):
            raise TypeError(f"adapter {name!r} must be callable")
        self._adapters[name] = fn

    def get(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def has(self, name: str) -> bool:
        return name in self._adapters

    def names(self) -> list[str]:
        return sorted(self._adapters)


# ─── Pipeline spec dataclasses ───────────────────────────────────────


@dataclass(frozen=True)
class PipelineStep:
    id: str
    capability: str
    adapter: str
    inputs: dict[str, Any] = field(default_factory=dict)
    when: dict | None = None


@dataclass(frozen=True)
class PipelineSpec:
    id: str
    description: str
    steps: tuple[PipelineStep, ...]
    source_path: Path | None = None


# ─── YAML loader ─────────────────────────────────────────────────────


_VALID_WHEN_OPS = frozenset({"exists", "missing", "nonempty", "empty",
                              "equals", "not_equals"})


def load_pipeline_from_dict(
    data: dict,
    *,
    source_path: Path | None = None,
    default_id: str | None = None,
) -> PipelineSpec:
    """Build a PipelineSpec from already-parsed pipeline data.

    Used by `load_pipeline()` after reading a YAML file, and directly by
    the future Planner agent when it produces a pipeline spec as JSON.
    `default_id` is used when the data omits `id:` (typically the source
    file stem); for in-memory plans Planner should always supply `id:`.
    """
    label = (source_path.name if source_path else "<in-memory>")

    if not isinstance(data, dict):
        raise PipelineError(f"pipeline {label}: top-level must be a mapping")

    pipeline_id = data.get("id") or default_id
    if not pipeline_id:
        raise PipelineError(f"pipeline {label}: missing 'id'")
    description = data.get("description") or ""
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PipelineError(f"pipeline {label}: 'steps' must be a non-empty list")

    seen_ids: set[str] = set()
    steps: list[PipelineStep] = []
    for idx, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PipelineError(
                f"pipeline {label}: step #{idx} is not a mapping"
            )
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise PipelineError(
                f"pipeline {label}: step #{idx} missing string 'id'"
            )
        if step_id in seen_ids:
            raise PipelineError(
                f"pipeline {label}: duplicate step id {step_id!r}"
            )
        seen_ids.add(step_id)

        capability = raw.get("capability")
        if not isinstance(capability, str) or not capability:
            raise PipelineError(
                f"pipeline {label}: step {step_id!r} missing 'capability'"
            )
        adapter = raw.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise PipelineError(
                f"pipeline {label}: step {step_id!r} missing 'adapter'"
            )

        inputs = raw.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise PipelineError(
                f"pipeline {label}: step {step_id!r} 'inputs' must be a mapping"
            )

        when = raw.get("when")
        if when is not None:
            _validate_when(when, label, step_id)

        steps.append(PipelineStep(
            id=step_id,
            capability=capability,
            adapter=adapter,
            inputs=inputs,
            when=when,
        ))

    return PipelineSpec(
        id=pipeline_id,
        description=description,
        steps=tuple(steps),
        source_path=source_path,
    )


def load_pipeline(path: Path | str) -> PipelineSpec:
    """Parse a pipeline YAML (or JSON, since JSON is YAML) file into a spec.

    Never executes. Delegates structural validation to
    `load_pipeline_from_dict` so the Planner can reuse the same loader for
    in-memory JSON plans.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PipelineError(f"cannot read pipeline file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PipelineError(f"pipeline {path.name}: malformed YAML: {e}") from e

    return load_pipeline_from_dict(
        data,
        source_path=path,
        default_id=path.stem,
    )


def _validate_when(when: Any, pipeline_name: str, step_id: str) -> None:
    if not isinstance(when, dict):
        raise PipelineError(
            f"pipeline {pipeline_name}: step {step_id!r} 'when' must be a mapping"
        )
    if not isinstance(when.get("var"), str) or not when["var"]:
        raise PipelineError(
            f"pipeline {pipeline_name}: step {step_id!r} 'when.var' must be a string path"
        )
    op = when.get("op")
    if op not in _VALID_WHEN_OPS:
        raise PipelineError(
            f"pipeline {pipeline_name}: step {step_id!r} 'when.op' "
            f"must be one of {sorted(_VALID_WHEN_OPS)}, got {op!r}"
        )


# ─── Variable resolution ─────────────────────────────────────────────


# Pure-placeholder form: the entire value is a single ${path} reference.
# We intentionally do NOT support string interpolation in 4.5 ("hello
# ${name}") — it complicates type preservation (a placeholder may resolve
# to a dict or a list) and is unnecessary for the current demo. Add later
# if real workloads need it.
_PLACEHOLDER_RE = re.compile(r"^\$\{([a-zA-Z0-9_.]+)\}$")


_MISSING = object()


def _resolve_path(path: str, env: dict) -> Any:
    """Walk a dotted path through nested mappings. Returns _MISSING if any
    segment doesn't exist, so callers can distinguish 'missing' from
    'present but None'."""
    current: Any = env
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def _resolve_value(value: Any, env: dict) -> Any:
    """Resolve a single ${path} placeholder. Non-string values pass through.

    Plain strings (no ${...}) pass through unchanged. A pure placeholder
    string resolves to whatever type lives at that path (preserves dict /
    list / int — not just str).
    """
    if not isinstance(value, str):
        return value
    m = _PLACEHOLDER_RE.match(value)
    if not m:
        return value
    resolved = _resolve_path(m.group(1), env)
    if resolved is _MISSING:
        raise PipelineError(f"placeholder ${{{m.group(1)}}} not found in context")
    return resolved


def _resolve_inputs(inputs: dict[str, Any], env: dict) -> dict[str, Any]:
    return {key: _resolve_value(val, env) for key, val in inputs.items()}


# ─── Condition evaluation ────────────────────────────────────────────


def _eval_when(when: dict | None, env: dict) -> bool:
    """Evaluate a structured `when:` clause. None → always run."""
    if when is None:
        return True
    path = when["var"]
    op = when["op"]
    resolved = _resolve_path(path, env)

    if op == "exists":
        return resolved is not _MISSING
    if op == "missing":
        return resolved is _MISSING
    if op == "nonempty":
        if resolved is _MISSING or resolved is None:
            return False
        if isinstance(resolved, (str, list, tuple, dict, set)):
            return len(resolved) > 0
        return bool(resolved)
    if op == "empty":
        if resolved is _MISSING or resolved is None:
            return True
        if isinstance(resolved, (str, list, tuple, dict, set)):
            return len(resolved) == 0
        return not bool(resolved)
    if op == "equals":
        return resolved == when.get("value")
    if op == "not_equals":
        return resolved != when.get("value")
    # _validate_when should have caught this, but defend anyway.
    raise PipelineError(f"unknown when op: {op!r}")


# ─── Run result ──────────────────────────────────────────────────────


@dataclass
class StepResult:
    id: str
    status: str                  # "succeeded" | "failed" | "skipped"
    output: Any = None
    error: str | None = None
    duration_ms: int | None = None


@dataclass
class PipelineRunResult:
    pipeline_id: str
    run_id: str | None
    status: str                  # "succeeded" | "failed"
    steps: dict[str, StepResult] = field(default_factory=dict)
    error: str | None = None


# ─── Runner ──────────────────────────────────────────────────────────


class PipelineRunner:
    def __init__(
        self,
        *,
        capability_manager,
        adapter_registry: AdapterRegistry,
        trace_store=None,
    ) -> None:
        self.capability_manager = capability_manager
        self.adapter_registry = adapter_registry
        self.trace_store = trace_store

    def register_adapter(self, name: str, fn: Adapter) -> None:
        self.adapter_registry.register(name, fn)

    def validate(self, spec: PipelineSpec) -> None:
        """Fail-fast checks before any step runs."""
        for step in spec.steps:
            if self.capability_manager.get(step.capability) is None:
                raise PipelineError(
                    f"pipeline {spec.id!r}: step {step.id!r} references "
                    f"unknown capability {step.capability!r}"
                )
            if not self.adapter_registry.has(step.adapter):
                raise PipelineError(
                    f"pipeline {spec.id!r}: step {step.id!r} references "
                    f"unregistered adapter {step.adapter!r}"
                )

    def run(self, spec: PipelineSpec, context: dict | None = None) -> PipelineRunResult:
        """Execute a parsed pipeline spec against an initial context.

        The whole pipeline shares ONE TraceStore.run() — every LLM call
        invoked by adapters (in 4.6+) will inherit the run_id via the
        ContextVar in trace_store. In 4.5 fixture adapters don't call
        LLMs, but each step still records a `pipeline_step_output`
        artifact with its inputs and outputs.
        """
        self.validate(spec)
        env: dict = {"context": dict(context or {}), "steps": {}}

        run_ctx = (
            self.trace_store.run(
                intent=f"pipeline:{spec.id}",
                agent="pipeline_runner",
                trigger_type="programmatic",
                metadata={
                    "source_path": str(spec.source_path) if spec.source_path else None,
                    "step_ids": [s.id for s in spec.steps],
                },
            )
            if self.trace_store is not None
            else _null_context()
        )

        with run_ctx as run_id:
            result = PipelineRunResult(
                pipeline_id=spec.id,
                run_id=run_id,
                status="succeeded",
            )
            for step in spec.steps:
                step_result = self._run_step(step, env)
                result.steps[step.id] = step_result
                if step_result.status == "failed":
                    result.status = "failed"
                    result.error = step_result.error
                    break
                if step_result.status == "succeeded":
                    env["steps"][step.id] = (
                        step_result.output
                        if isinstance(step_result.output, dict)
                        else {"output": step_result.output}
                    )
                else:  # skipped
                    env["steps"][step.id] = {}
            return result

    def _run_step(self, step: PipelineStep, env: dict) -> StepResult:
        # Evaluate `when:` first; skipped steps don't resolve inputs.
        try:
            should_run = _eval_when(step.when, env)
        except PipelineError as e:
            self._record_step_artifact(
                step, status="failed",
                metadata={"error": f"when-eval: {e}"},
            )
            return StepResult(id=step.id, status="failed", error=str(e))

        if not should_run:
            self._record_step_artifact(
                step, status="skipped",
                metadata={"reason": "when_false"},
            )
            logging.debug(f"pipeline step {step.id!r} skipped (when=false)")
            return StepResult(id=step.id, status="skipped")

        # Resolve inputs, dispatch to adapter.
        try:
            resolved_inputs = _resolve_inputs(step.inputs, env)
        except PipelineError as e:
            self._record_step_artifact(
                step, status="failed",
                metadata={"error": f"input-resolve: {e}"},
            )
            return StepResult(id=step.id, status="failed", error=str(e))

        adapter = self.adapter_registry.get(step.adapter)
        # validate() already checked this, but defend in case of
        # post-validate de-registration.
        if adapter is None:
            err = f"adapter {step.adapter!r} not registered"
            self._record_step_artifact(step, status="failed",
                                        metadata={"error": err})
            return StepResult(id=step.id, status="failed", error=err)

        started = time.perf_counter()
        try:
            output = adapter(resolved_inputs)
        except Exception as e:
            duration = int(round((time.perf_counter() - started) * 1000))
            self._record_step_artifact(
                step, status="failed",
                metadata={
                    "inputs": resolved_inputs,
                    "error": str(e),
                    "duration_ms": duration,
                },
            )
            return StepResult(id=step.id, status="failed",
                              error=str(e), duration_ms=duration)

        duration = int(round((time.perf_counter() - started) * 1000))
        self._record_step_artifact(
            step, status="succeeded",
            metadata={
                "inputs": resolved_inputs,
                "output": output,
                "duration_ms": duration,
            },
        )
        return StepResult(id=step.id, status="succeeded",
                          output=output, duration_ms=duration)

    def _record_step_artifact(
        self,
        step: PipelineStep,
        *,
        status: str,
        metadata: dict,
    ) -> None:
        if self.trace_store is None:
            return
        full_meta = {
            "step_id": step.id,
            "capability": step.capability,
            "adapter": step.adapter,
            "status": status,
            **metadata,
        }
        try:
            self.trace_store.record_artifact(
                path=None,
                artifact_type="pipeline_step_output",
                title=step.id,
                metadata=full_meta,
                quality_verdict=status,
            )
        except Exception as e:
            logging.debug(f"pipeline trace write failed for {step.id}: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────


class _null_context:
    """Stand-in for TraceStore.run() when no trace store is provided."""
    def __enter__(self) -> None:
        return None
    def __exit__(self, *exc) -> None:
        return None
