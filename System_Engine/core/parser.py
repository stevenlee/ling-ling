"""Markdown / Mermaid / JSON helpers used throughout the agent pipeline.

The public surface (callers across agents/, services/, watchers/, maintenance/):

    parse_markdown_metadata(content)        -> dict
    dump_markdown_with_metadata(meta, body) -> str
    clean_llm_response(text)                -> str
    run_markdown_quality_checks(text, ...)  -> (str, list[dict])
    repair_mermaid_fences(text)             -> (str, list[dict])
    repair_mermaid_label_quotes(text)       -> (str, list[dict])
    repair_latex_carriage_returns(text)     -> (str, list[dict])
    repair_latex_escape_collisions(text)    -> (str, list[dict])
    strip_body_frontmatter(text)            -> (str, list[dict])
    extract_json_array(text)                -> list[dict]
    extract_json_object(text)               -> dict

Quality-fix records are structured `{type, line?, before?, after?}` so
note frontmatter retains a recoverable diff of what changed. Only `type`
is guaranteed; other fields are omitted when they wouldn't carry
information.

Every repair function is idempotent: running it twice yields the same output
as running it once, and the fix-list will be empty on the second pass. This
matters because the pipeline used to invoke `run_markdown_quality_checks`
twice in a row.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

import yaml


# ─── Tag manager (lazy singleton) ──────────────────────────────────────

_tag_manager_instance = None


def _get_tag_manager():
    """Return a cached TagManager instance (lazy import to avoid cycles)."""
    global _tag_manager_instance
    if _tag_manager_instance is None:
        from core.tag_manager import TagManager
        from core.config import TAG_MAP_FILE
        _tag_manager_instance = TagManager(TAG_MAP_FILE)
    return _tag_manager_instance


# ─── Frontmatter ───────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
# Body hashtag: `#word` preceded by SOL or whitespace.  CJK ranges included.
_HASHTAG_RE = re.compile(r'(?:^|\s)#([\w一-鿿]+)')


def parse_markdown_metadata(content: str) -> dict:
    """Extract YAML frontmatter + body hashtags from a markdown string."""
    if not content:
        return {"tags": []}

    tags: set[str] = set()
    metadata: dict = {}
    remaining = content

    fm = _FRONTMATTER_RE.search(content)
    if fm:
        try:
            yaml_data = yaml.safe_load(fm.group(1))
        except Exception as e:
            logging.error(f"Parser: failed to parse YAML frontmatter: {e}")
            yaml_data = None

        if isinstance(yaml_data, dict):
            for key, value in yaml_data.items():
                if key == "tags":
                    if isinstance(value, list):
                        tags.update(str(t).strip() for t in value)
                    elif isinstance(value, str):
                        tags.update(t.strip() for t in value.split(","))
                else:
                    metadata[key] = value
        remaining = content[fm.end():]

    tags.update(_HASHTAG_RE.findall(remaining))

    tm = _get_tag_manager()
    metadata["tags"] = sorted({tm.normalize(t) for t in tags if t})
    return metadata


def dump_markdown_with_metadata(metadata: dict, content: str) -> str:
    """Serialize metadata + body as a single markdown document."""
    clean: dict = {}
    for k, v in (metadata or {}).items():
        if k == "tags" and isinstance(v, (set, list, tuple)):
            clean[k] = sorted({str(t) for t in v})
        elif isinstance(v, set):
            clean[k] = sorted(v)
        else:
            clean[k] = v

    frontmatter = yaml.safe_dump(clean, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{content}"


# ─── Mermaid: shared regexes ───────────────────────────────────────────

MERMAID_START_RE = re.compile(
    r'^\s*(graph\s+(?:TD|TB|BT|RL|LR)|flowchart\s+(?:TD|TB|BT|RL|LR)|'
    r'sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline)\b',
    re.IGNORECASE,
)

MARKDOWN_BOUNDARY_RE = re.compile(r'^\s*(#{1,6}\s+|---\s*$|\*\*\*\s*$|___\s*$)')

MERMAID_CONTINUATION_RE = re.compile(
    r'^\s*('
    r'graph\b|flowchart\b|subgraph\b|end\b|style\b|classDef\b|class\b|'
    r'click\b|linkStyle\b|direction\b|sequenceDiagram\b|participant\b|'
    r'note\b|activate\b|deactivate\b|alt\b|else\b|opt\b|loop\b|par\b|'
    r'stateDiagram\b|stateDiagram-v2\b|erDiagram\b|journey\b|gantt\b|'
    r'pie\b|mindmap\b|timeline\b|section\b|title\b|%%|'
    r'[\w".()[\]{}:/ -]+\s*(?:-->|---|-.->|==>|--|:|\|)'
    r')',
    re.IGNORECASE,
)

# Mermaid node-shape opener/closer pairs we know how to quote-repair.
# Order matters: longer/more-specific openers first so we never match e.g. `[`
# inside an actual `[[` opener.
_MERMAID_SHAPES: tuple[tuple[str, str], ...] = (
    ("[[", "]]"),   # subroutine
    ("[(", ")]"),   # cylinder
    ("[/", "/]"),   # parallelogram-alt
    ("[\\", "\\]"), # parallelogram
    ("[/", "\\]"),  # trapezoid
    ("[\\", "/]"),  # trapezoid-alt
    ("((", "))"),   # circle
    ("{{", "}}"),   # hexagon
    ("([", "])"),   # stadium
    (">",  "]"),    # asymmetric
    ("[",  "]"),    # rectangle
    ("(",  ")"),    # round
    ("{",  "}"),    # rhombus
)

# Node IDs must start with a word/CJK character — never with `-`. Allowing
# leading `-` lets the walker match arrow operators like `-->` as if they were
# node ids, which combined with the `>...]` asymmetric shape silently
# corrupts `A[X] --> B[Y]` into `A["X"] -->"B[Y"]`.
_MERMAID_NODE_HEAD_RE = re.compile(r'[\w一-鿿][\w\-一-鿿]*')

LATEX_CR_COMMAND_RE = re.compile(r'\r(ightarrow|ight|angle|brace|ceil|floor|vert|Vert)\b')

# Other JSON-escape collisions affecting LaTeX commands. When LLMs emit
# LaTeX inside a JSON string, they often forget to escape the backslash;
# json.loads then interprets `\binom` as <BS>inom, `\frac` as <FF>rac,
# `\vec` as <VT>ec. We restore the backslash here.
#
# Skipped: `\n` (collides with legit newlines) and `\t` (legit tabs).
# Both `\r` and the alternates below operate on the control characters
# left behind by JSON decoding, not on literal backslashes.
_LATEX_ESCAPE_COLLISIONS: tuple[tuple[str, str, str], ...] = (
    ("\x08", "b", "repaired_latex_backspace"),       # \b → \binom, \big, ...
    ("\x0c", "f", "repaired_latex_form_feed"),       # \f → \frac, \forall, ...
    ("\x0b", "v", "repaired_latex_vertical_tab"),    # \v → \vec, \vee, ...
)


# ─── quality_fix record helpers ───────────────────────────────────────
#
# Each repair function returns a list of structured records
# `{type, line, before, after}`. Only `type` is required; the other fields
# are omitted when they wouldn't carry information (e.g. a structural fix
# with no meaningful before/after snippet). Snippets are truncated to
# `_FIX_SNIPPET_LEN` characters so a chatty pipeline doesn't bloat the
# frontmatter of generated notes.

_FIX_SNIPPET_LEN = 80


def _truncate_snippet(s: str) -> str:
    if s is None:
        return ""
    if len(s) <= _FIX_SNIPPET_LEN:
        return s
    return s[: _FIX_SNIPPET_LEN - 1] + "…"


def _make_fix(
    type_: str,
    *,
    line: int | None = None,
    before: str = "",
    after: str = "",
) -> dict:
    """Build a quality_fix record. Omits empty/None fields for compactness."""
    fix: dict = {"type": type_}
    if line is not None:
        fix["line"] = line
    before = _truncate_snippet(before)
    after = _truncate_snippet(after)
    if before:
        fix["before"] = before
    if after:
        fix["after"] = after
    return fix


def repair_latex_carriage_returns(text: str) -> tuple[str, list[dict]]:
    """Repair `\r` that should have been a literal `\\r` (LaTeX command)."""
    if not text:
        return "", []
    fixes: list[dict] = []
    parts: list[str] = []
    last_end = 0
    for match in LATEX_CR_COMMAND_RE.finditer(text):
        parts.append(text[last_end:match.start()])
        before = match.group(0)
        after = "\\r" + match.group(1)
        parts.append(after)
        line_no = text.count("\n", 0, match.start()) + 1
        fixes.append(_make_fix(
            "repaired_latex_carriage_return",
            line=line_no,
            before=before,
            after=after,
        ))
        last_end = match.end()
    if not fixes:
        return text, []
    parts.append(text[last_end:])
    return "".join(parts), fixes


def repair_latex_escape_collisions(text: str) -> tuple[str, list[dict]]:
    """Restore LaTeX commands swallowed by JSON's `\\b` / `\\f` / `\\v` escapes.

    LLM-generated JSON often forgets to escape the backslash inside
    LaTeX. After `json.loads`, `\\binom` becomes `<BS>inom`, `\\frac`
    becomes `<FF>rac`, `\\vec` becomes `<VT>ec`. This pass walks the
    text, finds runs of `<control-char><letters>`, and restores the
    backslash + LaTeX-letter prefix.

    `\\r` is handled separately by `repair_latex_carriage_returns` to
    preserve its existing `repaired_latex_carriage_return` fix-type for
    legacy queries against note metadata. `\\n` and `\\t` are NOT
    repaired because they collide with legitimate newlines and tabs.
    """
    if not text:
        return "", []

    fixes: list[dict] = []
    cleaned = text

    for control_char, latex_letter, fix_type in _LATEX_ESCAPE_COLLISIONS:
        if control_char not in cleaned:
            continue
        pattern = re.compile(re.escape(control_char) + r"([a-zA-Z]+)")
        parts: list[str] = []
        last_end = 0
        any_match = False
        for match in pattern.finditer(cleaned):
            any_match = True
            parts.append(cleaned[last_end:match.start()])
            suffix = match.group(1)
            before = control_char + suffix
            after = f"\\{latex_letter}{suffix}"
            parts.append(after)
            line_no = cleaned.count("\n", 0, match.start()) + 1
            fixes.append(_make_fix(
                fix_type,
                line=line_no,
                before=before,
                after=after,
            ))
            last_end = match.end()
        if any_match:
            parts.append(cleaned[last_end:])
            cleaned = "".join(parts)

    return cleaned, fixes


# ─── Mermaid: label quoting ────────────────────────────────────────────

def _strip_mermaid_comment(line: str) -> str:
    """Return the portion of a mermaid line before any `%%` comment."""
    idx = line.find("%%")
    return line if idx < 0 else line[:idx]


def _find_shape_end(text: str, start: int, opener: str, closer: str) -> int:
    """Return the index of the matching closer for an opener at `start`.

    Skips closers that appear inside double-quoted segments. Returns -1 if no
    matching closer is found.
    """
    i = start + len(opener)
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if text.startswith(closer, i):
            return i
        i += 1
    return -1


def _quote_labels_in_line(line: str) -> tuple[str, bool]:
    """Quote unquoted labels inside the recognized mermaid shapes on one line.

    Returns (new_line, changed). Comments (everything after `%%`) are left
    untouched.
    """
    code_part = _strip_mermaid_comment(line)
    comment_part = line[len(code_part):]
    if not code_part.strip():
        return line, False

    out: list[str] = []
    i = 0
    n = len(code_part)
    changed = False

    while i < n:
        # Skip double-quoted strings as-is.
        if code_part[i] == '"':
            out.append(code_part[i])
            i += 1
            while i < n and code_part[i] != '"':
                if code_part[i] == "\\" and i + 1 < n:
                    out.append(code_part[i:i + 2])
                    i += 2
                    continue
                out.append(code_part[i])
                i += 1
            if i < n:
                out.append(code_part[i])
                i += 1
            continue

        # Try to match `nodeId<opener>label<closer>` starting here.
        head = _MERMAID_NODE_HEAD_RE.match(code_part, i)
        if not head:
            out.append(code_part[i])
            i += 1
            continue

        head_end = head.end()
        matched = False
        for opener, closer in _MERMAID_SHAPES:
            if not code_part.startswith(opener, head_end):
                continue
            close_at = _find_shape_end(code_part, head_end, opener, closer)
            if close_at < 0:
                continue
            label_start = head_end + len(opener)
            label = code_part[label_start:close_at]
            stripped = label.strip()
            if stripped and not stripped.startswith(('"', "'")):
                escaped = stripped.replace("\\", "\\\\").replace('"', '\\"')
                out.append(code_part[i:head_end])
                out.append(opener)
                out.append(f'"{escaped}"')
                out.append(closer)
                changed = True
            else:
                out.append(code_part[i:close_at + len(closer)])
            i = close_at + len(closer)
            matched = True
            break

        if not matched:
            out.append(code_part[i:head_end])
            i = head_end

    return "".join(out) + comment_part, changed


def repair_mermaid_label_quotes(text: str) -> tuple[str, list[dict]]:
    """Quote bare labels inside mermaid node shapes within fenced blocks."""
    if not text:
        return "", []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False

    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if not in_mermaid and stripped == "```mermaid":
            in_mermaid = True
            out.append(line)
            continue
        if in_mermaid and stripped == "```":
            in_mermaid = False
            out.append(line)
            continue

        if in_mermaid:
            new_line, changed = _quote_labels_in_line(line)
            if changed:
                fixes.append(_make_fix(
                    "quoted_mermaid_labels",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
            out.append(new_line)
        else:
            out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: fence repair ────────────────────────────────────────────

_FENCE_RE = re.compile(r'^```(\w*)\s*$')


def _is_mermaid_continuation(line: str) -> bool:
    s = line.strip()
    return not s or bool(MERMAID_CONTINUATION_RE.match(s))


def _build_next_nonempty(lines: list[str]) -> list[int]:
    """For each index, return the index of the next non-empty line (or len)."""
    n = len(lines)
    nxt = [n] * (n + 1)
    last = n
    for i in range(n - 1, -1, -1):
        nxt[i] = i if lines[i].strip() else last
        last = nxt[i]
    return nxt


def repair_mermaid_fences(text: str) -> tuple[str, list[dict]]:
    """Fix common LLM mistakes around mermaid code fences."""
    if not text:
        return "", []

    lines = text.splitlines()
    nxt = _build_next_nonempty(lines)
    out: list[str] = []
    fixes: list[dict] = []
    in_fence = False
    fence_lang = ""
    i = 0
    n = len(lines)

    def peek_next_nonempty(start: int) -> str:
        idx = nxt[start] if start < len(nxt) else n
        return lines[idx] if idx < n else ""

    while i < n:
        line = lines[i]
        stripped = line.strip()
        fence_match = _FENCE_RE.match(stripped)

        if fence_match:
            # Closing a mermaid block: drop premature ``` if more mermaid follows.
            if in_fence and fence_lang == "mermaid":
                following = peek_next_nonempty(i + 1)
                if (
                    following
                    and not MARKDOWN_BOUNDARY_RE.match(following)
                    and _is_mermaid_continuation(following)
                ):
                    fixes.append(_make_fix(
                        "ignored_premature_mermaid_close",
                        line=i + 1,
                        before=line,
                    ))
                    i += 1
                    continue

            in_fence = not in_fence
            fence_lang = fence_match.group(1).lower() if in_fence else ""
            out.append(line)
            i += 1
            continue

        # Bare `mermaid` keyword followed by a real diagram → wrap it.
        if (
            not in_fence
            and stripped.lower() == "mermaid"
            and MERMAID_START_RE.match(peek_next_nonempty(i + 1))
        ):
            fixes.append(_make_fix(
                "wrapped_bare_mermaid",
                line=i + 1,
                before=line,
                after="```mermaid",
            ))
            out.append("```mermaid")
            i += 1

            while i < n:
                current = lines[i]
                cs = current.strip()

                if cs == "```":
                    i += 1
                    break

                if cs and MARKDOWN_BOUNDARY_RE.match(current) and out[-1].strip():
                    break

                following = peek_next_nonempty(i + 1)
                if not cs and following and (
                    MARKDOWN_BOUNDARY_RE.match(following)
                    or not _is_mermaid_continuation(following)
                ):
                    i += 1
                    break

                if cs and not _is_mermaid_continuation(current):
                    break

                out.append(current)
                i += 1

            while out and not out[-1].strip():
                out.pop()
            out.append("```")
            continue

        out.append(line)
        i += 1

    if in_fence and fence_lang == "mermaid":
        fixes.append(_make_fix(
            "closed_unterminated_mermaid",
            line=n,
            after="```",
        ))
        out.append("```")

    return "\n".join(out), fixes


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
        r'^---\s*\n(.*?)\n---\s*(?:\n|$)',
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
    cleaned = text_stripped[match.end():].lstrip()
    return cleaned, [_make_fix(
        "removed_body_frontmatter",
        line=1,
        before=full_block,
    )]


# ─── Markdown: bold spacing ────────────────────────────────────────────

_BOLD_BLOCK_RE = re.compile(r'([^\s*])?\*\*(.*?)\*\*([^\s*])?')

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
        g1, g2, g3 = match.groups()
        before_str = match.group(0)
        
        needs_space_before = (g1 is not None)
        needs_space_after = (g3 is not None)
        
        if not needs_space_before and not needs_space_after:
            continue
            
        res = ''
        if g1: res += g1 + ' '
        res += '**' + g2 + '**'
        if g3: res += ' ' + g3
        
        new_text.append(text[last_end:match.start()])
        new_text.append(res)
        line_no = text.count("\n", 0, match.start()) + 1
        fixes.append(_make_fix(
            "repaired_bold_spacing",
            line=line_no,
            before=before_str,
            after=res,
        ))
        last_end = match.end()
        
    if not fixes:
        return text, []
        
    new_text.append(text[last_end:])
    return "".join(new_text), fixes


def run_markdown_quality_checks(text: str, strip_frontmatter: bool = False) -> tuple[str, list[dict]]:
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
    pipeline.extend([
        repair_latex_carriage_returns,
        repair_latex_escape_collisions,
        repair_mermaid_fences,
        repair_mermaid_label_quotes,
        repair_markdown_bold_spacing,
    ])

    for step in pipeline:
        cleaned, applied = step(cleaned)
        fixes.extend(applied)

    # Line-level trailing whitespace: count affected lines for traceability.
    affected_lines = [
        i + 1 for i, line in enumerate(cleaned.split("\n"))
        if line != line.rstrip()
    ]
    if affected_lines:
        stripped = "\n".join(line.rstrip() for line in cleaned.split("\n"))
        fixes.append(_make_fix(
            "trailing_whitespace",
            line=affected_lines[0],
            before=f"{len(affected_lines)} line(s) affected",
        ))
        cleaned = stripped

    # Collapse 3+ blank lines down to 2.
    collapsed = re.sub(r'\n{3,}', '\n\n', cleaned)
    if collapsed != cleaned:
        fixes.append(_make_fix("excessive_blank_lines"))
        cleaned = collapsed

    return cleaned.strip(), fixes


_OUTER_FENCE_RE = re.compile(r'^```(\w*)\n(.*?)\n```$', re.DOTALL | re.IGNORECASE)
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


# ─── JSON extraction ───────────────────────────────────────────────────

_FENCED_JSON_RE = re.compile(r'```(?:json)?\s*\n(.*?)\n```', re.DOTALL | re.IGNORECASE)


def _candidate_payloads(text: str) -> Iterable[str]:
    """Yield candidate JSON payloads: fenced first (if present), then raw."""
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        yield fenced.group(1).strip()
    yield text.strip()


def extract_json_array(text: str) -> list:
    """Extract a JSON array of dicts from LLM output."""
    if not text:
        return []
    for candidate in _candidate_payloads(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        match = re.search(r'\[.*\]', candidate, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
    return []


def extract_json_object(text: str) -> dict:
    """Extract a JSON object from LLM output."""
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for candidate in _candidate_payloads(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        for match in re.finditer(r'\{', candidate):
            try:
                parsed, _ = decoder.raw_decode(candidate[match.start():])
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}
