"""Wiki-note response parsing: YAML+Markdown hybrid + frontmatter cleanup (P2b).

Moved verbatim from services/llm_client.py staticmethods.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

from core.parsing.markdown_quality import (
    clean_llm_response,
    strip_body_frontmatter,
    strip_orphan_leading_fence,
)

_FENCED_MARKDOWN_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n```$", re.DOTALL | re.IGNORECASE)
_YAML_HEADER_RE = re.compile(
    r"(?:^|\n)(?:---|```yaml)\s*\n(.*?)\n(?:---|```)\s*(?:\n|$)", re.DOTALL
)
_YAML_MARKDOWN_CLEANUP_RE = re.compile(
    r"(^|[:\[,\s])[\*\_]{1,2}(.*?)[\*\_]{1,2}(?=[\]\s,:]|$)",
    re.MULTILINE,
)
_H1_TITLE_RE = re.compile(r"^#\s+(.*)", re.MULTILINE)
_PURE_PUNCTUATION_RE = re.compile(r"^\s*[．。！？!?、]+\s*$")
_DIGEST_LIST_FIELDS = {"key_points", "evidence", "terms", "open_questions", "highlights"}


@dataclass(frozen=True)
class EntityParseResult:
    """Typed parse outcome for model-authored entity pages.

    ``invalid`` is intentionally distinct from an entity whose body happens to
    contain YAML-looking prose. In strict ingestion, a discovered but broken
    YAML contract must never fall back to publishing the raw response.
    """

    status: str  # "valid" | "salvaged" | "invalid"
    value: dict
    issues: list[str] = field(default_factory=list)
    salvage_actions: list[str] = field(default_factory=list)
    had_yaml_header: bool = False

    @property
    def valid(self) -> bool:
        return self.status in {"valid", "salvaged"}


def _remove_standalone_yaml_punctuation(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    cleaned = [line for line in lines if not _PURE_PUNCTUATION_RE.fullmatch(line)]
    return "\n".join(cleaned), len(cleaned) != len(lines)


def _normalize_digest_list_indentation(text: str) -> tuple[str, bool]:
    """Normalize list items only beneath known flat ``part_digest`` fields."""
    lines = text.splitlines()
    changed = False
    part_indent: int | None = None
    field_indent: int | None = None
    in_list = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("part_digest:"):
            part_indent = indent
            field_indent = None
            in_list = False
            continue
        if part_indent is None:
            continue
        if stripped and indent <= part_indent:
            part_indent = None
            field_indent = None
            in_list = False
            continue
        key_match = re.match(r"([a-z_]+):(?:\s|$)", stripped)
        if key_match:
            field_indent = indent
            in_list = key_match.group(1) in _DIGEST_LIST_FIELDS
            continue
        if in_list and field_indent is not None and stripped.startswith("-"):
            expected = field_indent + 2
            if indent != expected:
                lines[index] = " " * expected + stripped
                changed = True
    return "\n".join(lines), changed


def _coerce_entity_schema(metadata: dict) -> tuple[dict, list[str]]:
    metadata = dict(metadata)
    actions: list[str] = []
    digest = metadata.get("part_digest")
    if not isinstance(digest, dict):
        return metadata, actions
    digest = dict(digest)
    for key in ("open_questions", "highlights"):
        value = digest.get(key)
        if isinstance(value, str):
            digest[key] = [value]
            actions.append(f"coerce_{key}_scalar_to_list")
    handoff = digest.get("handoff")
    if isinstance(handoff, list) and all(isinstance(item, str) for item in handoff):
        digest["handoff"] = " ".join(item.strip() for item in handoff if item.strip())
        actions.append("coerce_handoff_list_to_string")
    metadata["part_digest"] = digest
    return metadata, actions


def _escape_bare_backslashes_in_double_quoted_yaml(text: str) -> str:
    r"""Make model-authored YAML double-quoted scalars safe for LaTeX.

    YAML treats backslashes inside double quotes as escapes. That makes common
    LaTeX fail loudly (``\operatorname`` has an invalid ``\o`` escape) or,
    worse, corrupt silently (``\emptyset``, ``\rangle`` and ``\neq`` begin
    with valid YAML escape letters). Model-generated metadata is single-line
    data, so an odd run of backslashes in a double-quoted scalar is intended as
    literal content unless it escapes a quote or slash. Make that run even.

    Plain and single-quoted YAML scalars are untouched because backslashes are
    already literal there. Existing doubled backslashes are idempotent.
    """
    out: list[str] = []
    in_double = False
    i = 0
    while i < len(text):
        char = text[i]
        if not in_double:
            out.append(char)
            if char == '"':
                in_double = True
            i += 1
            continue

        if char == '"':
            out.append(char)
            in_double = False
            i += 1
            continue

        if char != "\\":
            out.append(char)
            i += 1
            continue

        run_end = i
        while run_end < len(text) and text[run_end] == "\\":
            run_end += 1
        run_length = run_end - i
        next_char = text[run_end] if run_end < len(text) else ""
        out.append("\\" * run_length)

        # Preserve the two ordinary YAML escapes that are plausible in prose.
        # Every other odd run is data (usually LaTeX), not YAML syntax.
        if run_length % 2 and next_char not in {'"', "/"}:
            out.append("\\")

        if next_char == '"' and run_length % 2:
            # The quote is escaped in the original lexical stream, so consume
            # it without ending the double-quoted scalar.
            out.append(next_char)
            i = run_end + 1
        else:
            i = run_end
    return "".join(out)


def strip_accidental_frontmatter(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _FENCED_MARKDOWN_RE.sub(r"\1", text).strip()
    text, _ = strip_body_frontmatter(text)
    return text.strip()


def parse_entity_response(text: str, *, require_yaml_header: bool = True) -> EntityParseResult:
    """Parse an entity response without disguising contract failure as prose."""
    if not text:
        value = {"title": "Untitled", "tags": [], "type": "entity", "content": ""}
        return EntityParseResult("invalid", value, ["empty_response"])

    text = clean_llm_response(text)
    result = {"title": "Untitled", "tags": [], "type": "entity", "content": text}

    yaml_match = _YAML_HEADER_RE.search(text)
    if yaml_match:
        yaml_str = yaml_match.group(1).strip()
        # Bold/italic markers can sneak into LLM-produced YAML.
        clean_yaml_str = _YAML_MARKDOWN_CLEANUP_RE.sub(r'\1"\2"', yaml_str)
        clean_yaml_str = _escape_bare_backslashes_in_double_quoted_yaml(clean_yaml_str)
        normalized_initial, reindented_initial = _normalize_digest_list_indentation(clean_yaml_str)
        initial_actions = ["normalize_digest_list_indentation"] if reindented_initial else []
        candidates: list[tuple[str, list[str]]] = [(normalized_initial, initial_actions)]
        no_punctuation, removed = _remove_standalone_yaml_punctuation(clean_yaml_str)
        if removed:
            normalized_punctuation, reindented_punctuation = _normalize_digest_list_indentation(
                no_punctuation
            )
            punctuation_actions = ["remove_standalone_punctuation"]
            if reindented_punctuation:
                punctuation_actions.append("normalize_digest_list_indentation")
            candidates.append((normalized_punctuation, punctuation_actions))
        metadata = None
        parse_error: Exception | None = None
        actions: list[str] = []
        for candidate, candidate_actions in candidates:
            try:
                metadata = yaml.safe_load(candidate)
                actions = candidate_actions
                break
            except Exception as exc:
                parse_error = exc

        if isinstance(metadata, dict):
            metadata, coercions = _coerce_entity_schema(metadata)
            actions.extend(coercions)
            for key in ("title", "tags", "type", "pending_concepts", "part_digest"):
                if key in metadata:
                    result[key] = str(metadata[key]) if key == "title" else metadata[key]
            # A frequent model shape double-closes a fenced YAML header:
            # `````yaml ... --- ``` ``.  The YAML matcher consumes ``---`` and
            # leaves the bare ````` as the first body line.  If outer-wrapper
            # cleanup runs first, that orphan pairs with the final Mermaid
            # close and strips both, manufacturing an unclosed Mermaid block.
            # Remove only the already-defined high-confidence orphan shape
            # before considering an outer Markdown wrapper.
            raw_body = text[yaml_match.end() :].strip()
            raw_body, orphan_fixes = strip_orphan_leading_fence(raw_body)
            if orphan_fixes:
                actions.append("strip_orphan_leading_fence")
            result["content"] = clean_llm_response(raw_body)
            return EntityParseResult(
                "salvaged" if actions else "valid",
                result,
                salvage_actions=actions,
                had_yaml_header=True,
            )

        logging.warning(f"YAML parse failed: {parse_error}\nOffending string:\n{yaml_str}")
        return EntityParseResult(
            "invalid",
            result,
            ["yaml_parse_failed"],
            had_yaml_header=True,
        )

    title_match = _H1_TITLE_RE.search(text)
    if title_match:
        result["title"] = title_match.group(1).strip()
    if require_yaml_header:
        return EntityParseResult("invalid", result, ["missing_yaml_header"])
    return EntityParseResult("valid", result)


def hybrid_parse(text: str) -> dict:
    """Compatibility parser; strict ingestion uses :func:`parse_entity_response`."""
    return parse_entity_response(text, require_yaml_header=False).value
