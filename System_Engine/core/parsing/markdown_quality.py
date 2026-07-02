"""Deterministic markdown cleanup: body frontmatter, tables, bold spacing, orchestrator.

Moved verbatim out of core/parser.py (P2a of the refactor roadmap).
"""

from __future__ import annotations

import re

import yaml

from core.parsing.common import _make_fix
from core.parsing.latex_repair import (
    repair_latex_carriage_returns,
    repair_latex_escape_collisions,
    repair_unclosed_latex_display,
)
from core.parsing.mermaid_repair import (
    repair_mermaid_classdiagram,
    repair_mermaid_double_quotes,
    repair_mermaid_fences,
    repair_mermaid_label_quotes,
    repair_mermaid_latex_labels,
    repair_mermaid_mindmap_brackets,
    repair_mermaid_mindmap_labels,
    repair_mermaid_mindmap_math,
    repair_mermaid_overquoted_node,
    repair_mermaid_quadrant_points,
    repair_mermaid_quoted_endpoint_labels,
    repair_mermaid_quoted_node_ids,
    repair_mermaid_subgraph_keyword,
)


# ─── Misc cleanup ──────────────────────────────────────────────────────


def strip_body_frontmatter(text: str) -> tuple[str, list[dict]]:
    """Remove accidental YAML frontmatter from an LLM-generated body.

    Only strips the leading `---...---` block when it parses as a YAML
    mapping. Hand-authored Markdown that opens with a horizontal rule
    `---` and later contains a second `---` separator is left alone, so
    we don't accidentally swallow a real document section between two
    horizontal rules.
    """
    if not text:
        return "", []
    text_stripped = text.strip()
    match = re.match(
        r"^---\s*\n(.*?)\n---\s*(?:\n|$)",
        text_stripped,
        flags=re.DOTALL,
    )
    if not match:
        return text, []
    block = match.group(1)
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return text, []
    if not isinstance(parsed, dict):
        return text, []
    full_block = text_stripped[: match.end()]
    cleaned = text_stripped[match.end() :].lstrip()
    return cleaned, [
        _make_fix(
            "removed_body_frontmatter",
            line=1,
            before=full_block,
        )
    ]


# ─── Markdown: Table formatting ──────────────────────────────────────────

# Match table separator row: e.g. `|---|`, `|:--|--:|`, etc.
_TABLE_SEP_RE = re.compile(r"^\|?[\s\-\:\.\|]+\|?$")


def repair_markdown_tables(text: str) -> tuple[str, list[dict]]:
    """Fix common LLM markdown table errors.

    1. Align separator column counts with the header.
    2. Align data row column counts with the header (append empty columns).
    3. Hide interspersed non-table text that breaks table rendering using HTML comments.
    """
    if not text or "|" not in text:
        return text, []

    lines = text.splitlines()
    out = []
    fixes = []

    in_table = False
    expected_pipes = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # If we are not in a table
        if not in_table:
            if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
                # Lookahead to see if next line is a separator
                if i + 1 < len(lines):
                    next_stripped = lines[i + 1].strip()
                    # A separator a previous pass hid in a comment leaves the
                    # table headerless and unrenderable (and the data rows below
                    # never register as a table). Unwrap and restore a clean
                    # separator matching the header's column count.
                    sep = next_stripped
                    while sep.startswith("<!--") and sep.endswith("-->"):
                        sep = sep[4:-3].strip()
                    if sep != next_stripped and sep.startswith("|") and _TABLE_SEP_RE.match(sep):
                        cols = stripped.count("|") - 1
                        clean_sep = "|" + "|".join([" --- " for _ in range(cols)]) + "|"
                        fixes.append(
                            _make_fix(
                                "restored_hidden_table_separator",
                                line=i + 2,
                                before=lines[i + 1],
                                after=clean_sep,
                            )
                        )
                        lines[i + 1] = clean_sep
                        next_stripped = clean_sep
                    if next_stripped.startswith("|") and _TABLE_SEP_RE.match(next_stripped):
                        in_table = True
                        expected_pipes = stripped.count("|")
                        out.append(line)
                        i += 1
                        continue
            out.append(line)
            i += 1
            continue

        # If we ARE in a table
        if stripped.startswith("|") and stripped.endswith("|"):
            pipes = stripped.count("|")

            # Separator row
            if _TABLE_SEP_RE.match(stripped) and not any(c.isalnum() for c in stripped):
                if pipes != expected_pipes:
                    cols = expected_pipes - 1
                    new_line = "|" + "|".join([" --- " for _ in range(cols)]) + "|"
                    fixes.append(
                        _make_fix(
                            "repaired_table_separator_columns",
                            line=i + 1,
                            before=line,
                            after=new_line,
                        )
                    )
                    out.append(new_line)
                else:
                    out.append(line)
            # Data row
            else:
                if pipes < expected_pipes:
                    diff = expected_pipes - pipes
                    new_line = line.rstrip() + "".join(["   |" for _ in range(diff)])
                    fixes.append(
                        _make_fix(
                            "repaired_table_data_columns",
                            line=i + 1,
                            before=line,
                            after=new_line,
                        )
                    )
                    out.append(new_line)
                else:
                    out.append(line)
            i += 1
        elif not stripped:
            in_table = False
            out.append(line)
            i += 1
        else:
            indent = line[: len(line) - len(line.lstrip())]

            # Unwrap any HTML-comment layers a previous quality pass may have
            # added, so re-running this check never nests them
            # (<!-- <!-- ... --> -->). The inner text is re-decided below.
            was_comment = stripped.startswith("<!--") and stripped.endswith("-->")
            inner = stripped
            while inner.startswith("<!--") and inner.endswith("-->"):
                inner = inner[4:-3].strip()

            # A line that carries pipes and closes with '|' is a real table row;
            # if it is only missing its leading '|', restore it and reprocess via
            # the data-row path rather than hiding the user's data in a comment.
            if inner.endswith("|") and inner.count("|") >= 2:
                repaired = f"{indent}{inner if inner.startswith('|') else '| ' + inner}"
                if repaired != line:
                    fixes.append(
                        _make_fix(
                            "restored_hidden_table_row"
                            if was_comment
                            else "repaired_table_row_missing_leading_pipe",
                            line=i + 1,
                            before=line,
                            after=repaired,
                        )
                    )
                lines[i] = repaired
                continue  # re-process the now well-formed row; do not advance i

            # Genuine non-row text that was previously hidden: collapse any
            # nesting back to a single comment layer instead of re-wrapping.
            if was_comment:
                collapsed = f"{indent}<!-- {inner} -->"
                if collapsed != line:
                    fixes.append(
                        _make_fix(
                            "collapsed_nested_table_comment",
                            line=i + 1,
                            before=line,
                            after=collapsed,
                        )
                    )
                out.append(collapsed)
                i += 1
                continue

            # Interspersed text detection
            table_continues = False
            for look in range(i + 1, min(i + 6, len(lines))):
                if lines[look].strip().startswith("|") and lines[look].strip().endswith("|"):
                    table_continues = True
                    break
                if not lines[look].strip():
                    break

            if table_continues:
                fixes.append(
                    _make_fix(
                        "hidden_interspersed_table_text",
                        line=i + 1,
                        before=line,
                        after=f"<!-- {line} -->",
                    )
                )
                # To prevent breaking blockquotes or lists inside HTML comments, we just wrap it simply
                out.append(f"<!-- {line} -->")
                i += 1
            else:
                in_table = False
                out.append(line)
                i += 1

    return "\n".join(out), fixes


# ─── Markdown: bold spacing ────────────────────────────────────────────

_BOLD_BLOCK_RE = re.compile(r"(?<!\*)\*\*(.+?)\*\*(?!\*)")


def repair_markdown_bold_spacing(text: str) -> tuple[str, list[dict]]:
    """Ensure spaces around bold **text** for Obsidian compatibility.

    Obsidian (and some standard markdown parsers) requires spaces around
    `**` when mixed with CJK characters to reliably parse bold blocks.
    This turns `強調**XYZ**的` into `強調 **XYZ** 的`.
    """
    if not text or "**" not in text:
        return text, []

    fixes: list[dict] = []
    new_text = []
    last_end = 0

    for match in _BOLD_BLOCK_RE.finditer(text):
        start = match.start()
        end = match.end()
        inner = match.group(1)

        if not inner.strip():
            continue

        char_before = text[start - 1] if start > 0 else " "
        char_after = text[end] if end < len(text) else " "

        needs_space_before = char_before not in (" ", "\n", "\t", "*", "「", "『", "(", "[")
        needs_space_after = char_after not in (
            " ",
            "\n",
            "\t",
            "*",
            "」",
            "』",
            ")",
            "]",
            "，",
            "。",
            "！",
            "？",
            ",",
            ".",
            "!",
            "?",
            "：",
            ":",
            "；",
            ";",
        )

        if not needs_space_before and not needs_space_after:
            continue

        res = ""
        if needs_space_before:
            res += " "
        res += f"**{inner}**"
        if needs_space_after:
            res += " "

        new_text.append(text[last_end:start])
        new_text.append(res)

        line_no = text.count("\n", 0, start) + 1
        fixes.append(
            _make_fix(
                "repaired_bold_spacing",
                line=line_no,
                before=match.group(0),
                after=res,
            )
        )
        last_end = end

    if not fixes:
        return text, []

    new_text.append(text[last_end:])
    return "".join(new_text), fixes


def run_markdown_quality_checks(
    text: str, strip_frontmatter: bool = False
) -> tuple[str, list[dict]]:
    """Run deterministic cleanup passes. Idempotent.

    Returns `(cleaned_text, fixes)`. Each fix is a structured dict:
    `{type, line?, before?, after?}`. The `type` field is always present;
    the other fields are omitted when they wouldn't carry information.
    """
    if not text:
        return "", []

    fixes: list[dict] = []
    cleaned = text

    pipeline: list = []
    if strip_frontmatter:
        pipeline.append(strip_body_frontmatter)
    pipeline.extend(
        [
            repair_latex_carriage_returns,
            repair_latex_escape_collisions,
            repair_unclosed_latex_display,
            repair_mermaid_fences,
            repair_mermaid_subgraph_keyword,
            repair_mermaid_quoted_node_ids,
            repair_mermaid_overquoted_node,
            repair_mermaid_double_quotes,
            repair_mermaid_quoted_endpoint_labels,
            repair_mermaid_label_quotes,
            repair_mermaid_mindmap_labels,
            repair_mermaid_mindmap_math,
            repair_mermaid_mindmap_brackets,
            repair_mermaid_quadrant_points,
            repair_mermaid_classdiagram,
            repair_mermaid_latex_labels,
            repair_markdown_tables,
            repair_markdown_bold_spacing,
        ]
    )

    for step in pipeline:
        cleaned, applied = step(cleaned)
        fixes.extend(applied)

    # Line-level trailing whitespace: count affected lines for traceability.
    affected_lines = [i + 1 for i, line in enumerate(cleaned.split("\n")) if line != line.rstrip()]
    if affected_lines:
        stripped = "\n".join(line.rstrip() for line in cleaned.split("\n"))
        fixes.append(
            _make_fix(
                "trailing_whitespace",
                line=affected_lines[0],
                before=f"{len(affected_lines)} line(s) affected",
            )
        )
        cleaned = stripped

    # Collapse 3+ blank lines down to 2.
    collapsed = re.sub(r"\n{3,}", "\n\n", cleaned)
    if collapsed != cleaned:
        fixes.append(_make_fix("excessive_blank_lines"))
        cleaned = collapsed

    return cleaned.strip(), fixes


_OUTER_FENCE_RE = re.compile(r"^```(\w*)\n(.*?)\n```$", re.DOTALL | re.IGNORECASE)
_CONTAINER_LANGS = frozenset({"", "markdown", "md", "txt", "text", "markdown-math"})


def clean_llm_response(text: str) -> str:
    r"""Unwrap an outer ```markdown container, but keep mermaid/python intact."""
    if not text:
        return ""
    text = text.strip()
    match = _OUTER_FENCE_RE.match(text)
    if not match:
        return text
    lang = match.group(1).lower()
    if lang in _CONTAINER_LANGS:
        return match.group(2).strip()
    return text
