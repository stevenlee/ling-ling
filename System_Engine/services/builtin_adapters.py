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

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import (
    DIGEST_SOURCES_BUDGET_PER_SOURCE,
    DIGEST_SOURCES_MAX_SOURCE_CHARS,
    LOAD_SOURCES_MAX_CHARS_PER_SOURCE,
    PAGES_DIR,
)

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
                "Use the provided source material as evidence. It may be raw source "
                "text or source digests. If it is a digest, preserve coverage "
                "warnings and do not pretend it is complete original text. "
                "Do not critique the prompt; produce the requested comparison, "
                "critique angles, and action guidance directly."
            ),
            forced_template="none",
            persona="none",
            operation="answer_from_sources",
        )
        return {"output": text, "final_answer": text}

    return answer_from_sources


_WIKILINK_RE = re.compile(r"^\[\[(.*?)\]\]$")
_PART_NUM_RE = re.compile(r" \(Part (\d+)\)$")
_SOURCE_SECTION_RE = re.compile(r"^## Source: (.+)$", re.MULTILINE)


def _clean_title(value: str) -> str:
    value = str(value or "").strip()
    match = _WIKILINK_RE.match(value)
    if match:
        value = match.group(1)
    return value.split("|", 1)[0].strip()


def _resolve_source_paths(
    title: str,
    prefer: str = "stitched",
    include_parts: bool = True,
    max_parts_per_source: int | None = None,
) -> list[tuple[Path, str]]:
    """Resolve a vault title to one or more (path, source_kind) pairs.

    Priority order for the default stitched preference:
      1. Stitched
      2. Parts aggregated in numeric order (if include_parts is True)
      3. Synthesis
      4. Direct / fuzzy match

    When prefer != "stitched", Synthesis is checked before Stitched.
    Returns an empty list when nothing is found.
    """
    title = _clean_title(title)
    if not title:
        return []

    folder = PAGES_DIR / title
    if folder.is_dir():
        if prefer == "stitched":
            stitched = folder / f"{title} (Stitched).md"
            if stitched.exists():
                return [(stitched, "stitched")]

            if include_parts:
                part_paths = _collect_parts(folder, title, max_parts_per_source)
                if part_paths:
                    return [(p, "part") for p in part_paths]

            synthesis = folder / f"{title} (Synthesis).md"
            if synthesis.exists():
                return [(synthesis, "synthesis")]
        else:
            for suffix in ("Synthesis", "Stitched"):
                candidate = folder / f"{title} ({suffix}).md"
                if candidate.exists():
                    kind = "synthesis" if suffix == "Synthesis" else "stitched"
                    return [(candidate, kind)]

            if include_parts:
                part_paths = _collect_parts(folder, title, max_parts_per_source)
                if part_paths:
                    return [(p, "part") for p in part_paths]

        # ── Exact-name / first-match fallback ───────────────────────
        exact = folder / f"{title}.md"
        if exact.exists():
            return [(exact, "direct")]
        markdown_files = sorted(folder.glob("*.md"))
        if markdown_files:
            return [(markdown_files[0], _source_kind(markdown_files[0]))]

    # Not a folder — try direct file.
    direct = PAGES_DIR / f"{title}.md"
    if direct.exists():
        return [(direct, "direct")]

    matches = sorted(PAGES_DIR.rglob(f"*{title}*.md"))
    if matches:
        return [(matches[0], _source_kind(matches[0]))]
    return []


def _collect_parts(
    folder: Path,
    title: str,
    max_parts: int | None = None,
) -> list[Path]:
    """Collect Part N files sorted by part number."""
    parts: list[tuple[int, Path]] = []
    for md in folder.glob("*.md"):
        m = _PART_NUM_RE.search(md.stem)
        if m:
            parts.append((int(m.group(1)), md))
    parts.sort(key=lambda t: t[0])
    if max_parts is not None and max_parts > 0:
        parts = parts[:max_parts]
    return [p for _, p in parts]


# Keep the old single-path API as a thin wrapper for backward compat.
def _resolve_source_path(title: str, prefer: str = "stitched") -> Path | None:
    resolved = _resolve_source_paths(title, prefer=prefer, include_parts=False)
    return resolved[0][0] if resolved else None


def _make_load_sources(_llm: "LLMClient"):
    """Deterministic adapter for the `load_sources` capability.

    Phase 0.3.1 adds part aggregation: when no Stitched file exists, the
    adapter merges Part 1…N into a single source_text section instead of
    returning only Part 1.
    """

    def load_sources(inputs: dict) -> dict:
        raw_titles = inputs.get("titles") or inputs.get("target_titles") or []
        if isinstance(raw_titles, str):
            raw_titles = [raw_titles]
        flat_titles = []
        for rt in raw_titles:
            if isinstance(rt, str) and "," in rt:
                flat_titles.extend([t.strip() for t in rt.split(",")])
            else:
                flat_titles.append(rt)
        raw_titles = flat_titles
        max_chars = int(inputs.get("max_chars_per_source") or LOAD_SOURCES_MAX_CHARS_PER_SOURCE)
        prefer = str(inputs.get("prefer") or "stitched").lower()
        include_parts = bool(inputs.get("include_parts", True))
        max_parts_per_source = inputs.get("max_parts_per_source")
        if max_parts_per_source is not None:
            max_parts_per_source = int(max_parts_per_source)

        loaded: list[dict] = []
        missing: list[str] = []
        sections: list[str] = []
        for raw_title in raw_titles:
            title = _clean_title(raw_title)
            resolved = _resolve_source_paths(
                title,
                prefer=prefer,
                include_parts=include_parts,
                max_parts_per_source=max_parts_per_source,
            )
            if not resolved:
                missing.append(title)
                continue

            # Merge text from all resolved paths (usually 1; >1 for parts).
            path_strs: list[str] = []
            text_parts: list[str] = []
            for path, _ in resolved:
                path_strs.append(str(path))
                text_parts.append(path.read_text(encoding="utf-8"))
            text = "\n\n".join(text_parts)
            original_chars = len(text)
            truncated = max_chars > 0 and original_chars > max_chars
            if truncated:
                text = text[:max_chars] + "\n\n<!-- truncated by vault.load_sources -->"

            # Determine source_kind from resolved entries.
            if len(resolved) > 1:
                source_kind = "parts_aggregated"
            else:
                source_kind = resolved[0][1]

            meta: dict = {
                "title": title,
                "path": path_strs[0] if len(path_strs) == 1 else path_strs[0],
                "chars": len(text),
                "original_chars": original_chars,
                "loaded_chars": len(text),
                "max_chars": max_chars,
                "truncated": truncated,
                "source_kind": source_kind,
            }
            if source_kind == "parts_aggregated":
                meta["part_count"] = len(resolved)
                meta["paths"] = path_strs
            loaded.append(meta)
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
    if _PART_NUM_RE.search(stem):
        return "part"
    return "direct"


# ── Digest adapter ────────────────────────────────────────────────────


def _split_source_sections(source_text: str) -> list[tuple[str, str]]:
    """Split concatenated source_text into (title, text) pairs.

    Expects sections separated by `---` with headers like `## Source: Title`.
    """
    if not source_text.strip():
        return []

    parts = re.split(r"\n\n---\n\n", source_text)
    result: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _SOURCE_SECTION_RE.match(part)
        if m:
            title = m.group(1).strip()
            body = part[m.end() :].strip()
        else:
            title = f"source_{len(result) + 1}"
            body = part
        result.append((title, body))
    return result


def _merge_digests_to_text(digests: list[dict]) -> str:
    """Merge per-source digest dicts into a single Markdown string."""
    sections: list[str] = []
    for d in digests:
        if not d.get("digest"):
            continue
        title = d.get("title", "Untitled")
        text = d.get("digest")
        sections.append(f"## Digest: {title}\n\n{text}")
    return "\n\n---\n\n".join(sections)


def _make_digest_sources(llm: "LLMClient"):
    """Phase 0.3.1 adapter for per-source digesting.

    Internally calls LLM once per source section, then merges digests.
    """

    def digest_sources(inputs: dict) -> dict:
        query = inputs.get("query") or ""
        source_text = inputs.get("sources") or inputs.get("source_text") or ""
        budget = int(inputs.get("digest_budget") or DIGEST_SOURCES_BUDGET_PER_SOURCE)
        max_chars = int(inputs.get("max_source_chars") or DIGEST_SOURCES_MAX_SOURCE_CHARS)

        sections = _split_source_sections(source_text)
        digests: list[dict] = []
        warnings: list[str] = []

        for title, text in sections:
            original_chars = len(text)
            truncated_for_digest = original_chars > max_chars
            if truncated_for_digest:
                text = text[:max_chars]
                warnings.append(f"Source '{title}' truncated to {max_chars} chars for digest.")
            try:
                digest_text = llm.digest_sources(
                    query=query,
                    source_title=title,
                    source_text=text,
                    budget=budget,
                )
            except Exception as e:
                logging.error(f"digest_sources: LLM call failed for '{title}': {e}")
                digest_text = ""
                warnings.append(f"Source '{title}' digest failed: {e}")

            digests.append(
                {
                    "title": title,
                    "digest": digest_text,
                    "original_chars": original_chars,
                    "digested_chars": len(text),
                    "digest_chars": len(digest_text),
                    "truncated_for_digest": truncated_for_digest,
                }
            )

        digest_merged = _merge_digests_to_text(digests)
        coverage = [
            {
                "title": d["title"],
                "has_digest": bool(d["digest"]),
                "digest_chars": d["digest_chars"],
                "original_chars": d["original_chars"],
                "digested_chars": d["digested_chars"],
                "truncated_for_digest": d["truncated_for_digest"],
            }
            for d in digests
        ]

        return {
            "source_digests": digests,
            "digest_text": digest_merged,
            "source_coverage": coverage,
            "warnings": warnings,
        }

    return digest_sources


# Public name → adapter-factory map. Each factory takes an LLMClient and
# returns a callable matching the Adapter contract.
_BUILTIN_FACTORIES = {
    "vault.load_sources": _make_load_sources,
    "llm.digest_sources": _make_digest_sources,
    "llm.answer_from_sources": _make_answer_from_sources,
    "llm.synthesize": _make_synthesize,
    "llm.critique": _make_critique,
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
