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

import re
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import LOAD_SOURCES_MAX_CHARS_PER_SOURCE, PAGES_DIR

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


def _make_answer_from_sources(llm: "LLMClient"):
    """Adapter for final source-grounded answers from loaded source text."""

    def answer_from_sources(inputs: dict) -> dict:
        query = inputs.get("query") or inputs.get("directive") or ""
        sources = inputs.get("sources") or inputs.get("source_text") or ""
        focus = inputs.get("focus")
        if focus:
            query = f"{query}\n\nFocus: {focus}"
        text = llm.answer_query(
            query_content=query,
            wiki_context=sources,
            custom_instruction=(
                "Write a final, source-grounded answer to the user's directive. "
                "Use the provided source text as evidence. Do not critique the prompt; "
                "produce the requested comparison, critique angles, and action guidance directly."
            ),
            forced_template="none",
            persona="none",
            operation="answer_from_sources",
        )
        return {"output": text, "final_answer": text}

    return answer_from_sources


_WIKILINK_RE = re.compile(r"^\[\[(.*?)\]\]$")


def _clean_title(value: str) -> str:
    value = str(value or "").strip()
    match = _WIKILINK_RE.match(value)
    if match:
        value = match.group(1)
    return value.split("|", 1)[0].strip()


def _resolve_source_path(title: str, prefer: str = "stitched") -> Path | None:
    title = _clean_title(title)
    if not title:
        return None

    folder = PAGES_DIR / title
    if folder.is_dir():
        preferred_suffixes = (
            ["Stitched", "Synthesis", "Part 1"]
            if prefer == "stitched"
            else ["Synthesis", "Stitched", "Part 1"]
        )
        for suffix in preferred_suffixes:
            candidate = folder / f"{title} ({suffix}).md"
            if candidate.exists():
                return candidate
        exact = folder / f"{title}.md"
        if exact.exists():
            return exact
        markdown_files = sorted(folder.glob("*.md"))
        if markdown_files:
            return markdown_files[0]

    direct = PAGES_DIR / f"{title}.md"
    if direct.exists():
        return direct

    matches = sorted(PAGES_DIR.rglob(f"*{title}*.md"))
    return matches[0] if matches else None


def _make_load_sources(_llm: "LLMClient"):
    """Deterministic adapter for the `load_sources` capability."""

    def load_sources(inputs: dict) -> dict:
        raw_titles = inputs.get("titles") or inputs.get("target_titles") or []
        if isinstance(raw_titles, str):
            raw_titles = [raw_titles]
        max_chars = int(inputs.get("max_chars_per_source") or LOAD_SOURCES_MAX_CHARS_PER_SOURCE)
        prefer = str(inputs.get("prefer") or "stitched").lower()

        loaded: list[dict] = []
        missing: list[str] = []
        sections: list[str] = []
        for raw_title in raw_titles:
            title = _clean_title(raw_title)
            path = _resolve_source_path(title, prefer=prefer)
            if path is None:
                missing.append(title)
                continue
            text = path.read_text(encoding="utf-8")
            original_chars = len(text)
            truncated = max_chars > 0 and original_chars > max_chars
            if truncated:
                text = text[:max_chars] + "\n\n<!-- truncated by vault.load_sources -->"
            loaded.append({
                "title": title,
                "path": str(path),
                "chars": len(text),
                "original_chars": original_chars,
                "loaded_chars": len(text),
                "max_chars": max_chars,
                "truncated": truncated,
                "source_kind": _source_kind(path),
            })
            sections.append(f"## Source: {title}\n\n{text}")

        return {
            "source_text": "\n\n---\n\n".join(sections),
            "sources": loaded,
            "missing_titles": missing,
        }

    return load_sources


def _source_kind(path: Path) -> str:
    stem = path.stem
    if stem.endswith(" (Stitched)"):
        return "stitched"
    if stem.endswith(" (Synthesis)"):
        return "synthesis"
    if re.search(r" \(Part \d+\)$", stem):
        return "part"
    return "direct"


# Public name → adapter-factory map. Each factory takes an LLMClient and
# returns a callable matching the Adapter contract.
_BUILTIN_FACTORIES = {
    "vault.load_sources": _make_load_sources,
    "llm.answer_from_sources": _make_answer_from_sources,
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
