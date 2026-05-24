"""Phase 5A built-in adapters: thin wrappers binding LLMClient methods to
the PipelineRunner adapter contract.

An adapter is `Callable[[dict], dict]` — inputs as a single dict, outputs
keyed by name. Pipeline YAML references adapters by name; this module is
the ONLY place where capability metadata becomes executable Python (see
[[adapter_layer_constraint]]).

Phase 5A registers the minimum set needed to run the synthesize → critique
demo against the real LLM. Add new adapters here as capabilities are added
to CapabilityManager; never import private methods from production agents.

Usage:

    from services.builtin_adapters import register_builtin_adapters
    register_builtin_adapters(adapter_registry, llm_client)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.llm_client import LLMClient
    from services.pipeline_runner import AdapterRegistry


def _make_synthesize(llm: "LLMClient"):
    """Adapter for the `synthesize` capability (Operations/synthesize.md)."""

    def synthesize(inputs: dict) -> dict:
        title = inputs.get("title", "")
        part_digests = inputs.get("part_digests") or []
        final_concepts = inputs.get("final_concepts") or ""
        template = inputs.get("template")
        text = llm.generate_synthesis(
            title=title,
            part_digests=part_digests,
            final_concepts=final_concepts,
            template=template,
        )
        return {"output": text}

    return synthesize


def _make_critique(llm: "LLMClient"):
    """Adapter for the `critique` capability (Operations/critique.md)."""

    def critique(inputs: dict) -> dict:
        candidate = inputs.get("candidate", "")
        sources = inputs.get("sources", "")
        focus = inputs.get("focus")
        text = llm.critique_text(
            candidate=candidate,
            sources=sources,
            focus=focus,
        )
        return {"output": text}

    return critique


# Public name → adapter-factory map. Each factory takes an LLMClient and
# returns a callable matching the Adapter contract.
_BUILTIN_FACTORIES = {
    "llm.synthesize": _make_synthesize,
    "llm.critique":   _make_critique,
}


def builtin_adapter_names() -> list[str]:
    return sorted(_BUILTIN_FACTORIES)


def register_builtin_adapters(
    registry: "AdapterRegistry",
    llm: "LLMClient",
) -> list[str]:
    """Register every built-in adapter against `llm`. Returns the names."""
    registered: list[str] = []
    for name, factory in _BUILTIN_FACTORIES.items():
        registry.register(name, factory(llm))
        registered.append(name)
    return registered
