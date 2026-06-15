"""Markdown / Mermaid / JSON helpers used throughout the agent pipeline.

The public surface (callers across agents/, services/, watchers/, maintenance/):

    parse_markdown_metadata(content)        -> dict
    dump_markdown_with_metadata(meta, body) -> str
    clean_llm_response(text)                -> str
    run_markdown_quality_checks(text, ...)  -> (str, list[dict])
    repair_mermaid_fences(text)             -> (str, list[dict])
    repair_mermaid_quoted_endpoint_labels(text) -> (str, list[dict])
    repair_mermaid_label_quotes(text)       -> (str, list[dict])
    repair_mermaid_latex_labels(text)       -> (str, list[dict])
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

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', re.DOTALL)
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
_QUOTED_NODE_DEF_RE = re.compile(r'^(\s*)\"([\w\-一-鿿]+)\s*([\[\({>][^"]*[\]\)}])\"(\s*)$')
_MERMAID_CONN_ARROW_PAT = r'(?:-->|---|-.->|==>|--[xo]|-\.-|==)'
_MERMAID_CONN_START_QUOTED_ID_RE = re.compile(
    r'^(\s*)\"([\w\-一-鿿]+)\"\s*(' + _MERMAID_CONN_ARROW_PAT + r')'
)
_MERMAID_CONN_END_QUOTED_ID_RE = re.compile(
    r'(' + _MERMAID_CONN_ARROW_PAT + r')\s*\"([\w\-一-鿿]+)\"(\s*(?:%%.*)?)$'
)
# Same two anchors, but the quoted endpoint may hold ANY text (spaces,
# punctuation) — i.e. a label that cannot be a bare node id. ``[^"\n]`` keeps
# each match to a single endpoint and never spans the arrow to the other side.
_MERMAID_CONN_START_QUOTED_LABEL_RE = re.compile(
    r'^(\s*)\"([^"\n]+)\"\s*(' + _MERMAID_CONN_ARROW_PAT + r')'
)
_MERMAID_CONN_END_QUOTED_LABEL_RE = re.compile(
    r'(' + _MERMAID_CONN_ARROW_PAT + r')\s*\"([^"\n]+)\"(\s*(?:%%.*)?)$'
)
# A quoted endpoint whose text is a legal bare id (no spaces/punctuation) is
# left to the strip pass — `"A1" --> "B1"` → `A1 --> B1`, which renders
# identically and keeps the id stable for bare-id cross-references elsewhere.
_MERMAID_BARE_ID_RE = re.compile(r'^[\w\-一-鿿]+$')
# Pre-scan: collect ids already used in a fence so a synthesized id can't
# collide with an author's node. Leading id, or id immediately before a shape.
_MERMAID_EXISTING_ID_RE = re.compile(
    r'(?:^\s*|' + _MERMAID_CONN_ARROW_PAT + r'\s*)([\w\-一-鿿]+)(?=\s|[\[\(\{>]|$)'
)
_MERMAID_ID_SLUG_RE = re.compile(r'[^\w一-鿿]')

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

# Mermaid (Obsidian's parser) cannot render LaTeX/KaTeX math inside node
# labels: `$$...$$` delimiters and backslash commands like `\mathcal` throw a
# parse error that takes the whole diagram down. The bracket-balance heuristic
# never catches it because `{System}`/`{M}_0` stay balanced. So we degrade the
# math to readable plain text *inside mermaid fences only* — inline `$...$`
# math in normal prose is legitimate Obsidian markdown and is left untouched.
#
# `\command{X}` wrappers that only style their argument collapse to `X`.
_MERMAID_LATEX_WRAPPERS = (
    "mathcal", "mathbb", "mathbf", "mathrm", "mathsf", "mathit", "mathfrak",
    "boldsymbol", "operatorname", "textbf", "textit", "textrm", "text", "bm",
)
# `\command` symbols map to a Unicode glyph. Longest names first so the
# word-boundary match never lets `\in` shadow `\infty`.
_MERMAID_LATEX_SYMBOLS: dict[str, str] = {
    "Rightarrow": "⇒", "rightarrow": "→", "Leftarrow": "⇐", "leftarrow": "←",
    "leftrightarrow": "↔", "mapsto": "↦", "implies": "⇒", "iff": "⇔",
    "subseteq": "⊆", "supseteq": "⊇", "subset": "⊂", "supset": "⊃",
    "notin": "∉", "infty": "∞", "forall": "∀", "exists": "∃", "nabla": "∇",
    "partial": "∂", "approx": "≈", "equiv": "≡", "cong": "≅", "neq": "≠",
    "leq": "≤", "geq": "≥", "times": "×", "cdot": "·", "div": "÷", "pm": "±",
    "oplus": "⊕", "otimes": "⊗", "cup": "∪", "cap": "∩", "land": "∧",
    "lor": "∨", "neg": "¬", "sqrt": "√", "sum": "Σ", "prod": "Π", "int": "∫",
    "to": "→", "in": "∈",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "phi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "Pi": "Π",
}
_MERMAID_LATEX_WRAPPER_RE = re.compile(
    r'\\+\s*(?:' + "|".join(_MERMAID_LATEX_WRAPPERS) + r')\s*\{([^{}]*)\}'
)
_MERMAID_LATEX_SYMBOL_RES: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(r'\\+' + re.escape(name) + r'\b'), glyph)
    # Longest command names first so `\infty` isn't partially eaten by `\in`.
    for name, glyph in sorted(
        _MERMAID_LATEX_SYMBOLS.items(), key=lambda kv: len(kv[0]), reverse=True
    )
)
# `_{sub}` / `^{sup}` → drop the braces, keep `_sub` / `^sup`.
_MERMAID_LATEX_SCRIPT_RE = re.compile(r'([_^])\{([^{}]*)\}')
# Any backslash command we don't have a glyph for: drop it entirely.
_MERMAID_LATEX_UNKNOWN_CMD_RE = re.compile(r'\\+[a-zA-Z]+')
# A stray backslash that is NOT escaping a double-quote (`\"` is a legitimate
# mermaid label escape and must survive).
_MERMAID_LATEX_STRAY_SLASH_RE = re.compile(r'\\(?!")')


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
            if stripped:
                if stripped.startswith("'") and stripped.endswith("'") and len(stripped) >= 2:
                    inner = stripped[1:-1]
                    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
                    out.append(code_part[i:head_end])
                    out.append(opener)
                    out.append(f'"{escaped}"')
                    out.append(closer)
                    changed = True
                elif not stripped.startswith(('"', "'")):
                    escaped = stripped.replace("\\", "\\\\").replace('"', '\\"')
                    out.append(code_part[i:head_end])
                    out.append(opener)
                    out.append(f'"{escaped}"')
                    out.append(closer)
                    changed = True
                else:
                    out.append(code_part[i:close_at + len(closer)])
            else:
                # Empty label (e.g. `A[]`): preserve the shape verbatim.
                # Without this the span was silently dropped (audit R7-E).
                out.append(code_part[i:close_at + len(closer)])
            i = close_at + len(closer)
            matched = True
            break

        if not matched:
            out.append(code_part[i:head_end])
            i = head_end

    return "".join(out) + comment_part, changed


# ─── Mermaid: subgraph keyword repair ─────────────────────────────────

# Matches lines like `sub定的 "title"` or `sub動 "title"` — the LLM truncated
# `subgraph` and glued CJK (or other non-ASCII) text after `sub`.
_SUBGRAPH_BROKEN_RE = re.compile(
    r'^(\s*)sub([^\x00-\x7F]+)\s+(.*)',
    re.IGNORECASE,
)


def repair_mermaid_subgraph_keyword(text: str) -> tuple[str, list[dict]]:
    """Fix truncated ``subgraph`` keywords inside mermaid fences.

    LLMs sometimes replace the ``graph`` part of ``subgraph`` with CJK or
    other text when generating bilingual diagrams:
    ``sub定的 "title"`` → ``subgraph "title"``.
    """
    if not text or "sub" not in text:
        return text, []

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
            m = _SUBGRAPH_BROKEN_RE.match(line)
            if m:
                new_line = f"{m.group(1)}subgraph {m.group(3)}"
                fixes.append(_make_fix(
                    "repaired_mermaid_subgraph_keyword",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
                out.append(new_line)
            else:
                out.append(line)
        else:
            out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: quoted node-ID repair ───────────────────────────────────

# `"NodeId"[` — the LLM wrapped a valid ASCII node identifier in quotes.
# Mermaid requires bare identifiers: `NodeId["label"]`, not `"NodeId"["label"]`.
_QUOTED_NODE_ID_SHAPE_RE = re.compile(
    r'"([A-Za-z_]\w*(?:-\w+)*)"'    # "varName" or "my-node"
    r'(?=[\[\(\{>])'                 # lookahead: immediately followed by shape opener
)


def repair_mermaid_quoted_node_ids(text: str) -> tuple[str, list[dict]]:
    """Strip spurious quotes from node identifiers before shape openers.

    LLMs sometimes emit ``"A"["Label (X)"]`` instead of ``A["Label (X)"]``.
    The char-by-char label-quoting walker sees ``"A"`` as a completed quoted
    string and skips it, so the error passes through untouched.
    """
    if not text:
        return text, []

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
            new_line = _QUOTED_NODE_ID_SHAPE_RE.sub(r'\1', line)
            if new_line != line:
                fixes.append(_make_fix(
                    "stripped_mermaid_quoted_node_id",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
            out.append(new_line)
        else:
            out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: double-quote repair ─────────────────────────────────────

# `[""label""]` or `(""label"")` — LLM emitted two layers of quotes inside
# a shape, which breaks the mermaid parser completely.
_DOUBLE_QUOTE_SHAPE_RE = re.compile(
    r'([\[\(\{>])""'    # shape opener + ""
    r'(.*?)'            # label content (non-greedy)
    r'""([\]\)\}])'     # "" + shape closer
)


def repair_mermaid_double_quotes(text: str) -> tuple[str, list[dict]]:
    """Collapse ``[""label""]`` to ``["label"]`` inside mermaid fences.

    LLMs occasionally double the quotes inside node shapes, producing
    invalid syntax that the bracket-balance heuristic doesn't catch
    (the quotes cancel out).
    """
    if not text or '""' not in text:
        return text, []

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

        if in_mermaid and '""' in line:
            new_line = _DOUBLE_QUOTE_SHAPE_RE.sub(r'\1"\2"\3', line)
            if new_line != line:
                fixes.append(_make_fix(
                    "repaired_mermaid_double_quotes",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
            out.append(new_line)
        else:
            out.append(line)

    return "\n".join(out), fixes


def _synthesize_node_id(label: str, label_to_id: dict[str, str], used_ids: set[str]) -> str:
    """Map a connection-label to a stable, unique node id within one fence.

    Same label → same id (so ``"Mid" --> "End"`` and ``"Start" --> "Mid"``
    share one node). The id is a slug of the label so it reads sensibly and
    naturally dedups; on a slug collision with a *different* label or an
    author's existing id, a counter suffix is appended.
    """
    if label in label_to_id:
        return label_to_id[label]
    slug = _MERMAID_ID_SLUG_RE.sub("", label)[:24] or "node"
    candidate = slug
    i = 1
    while candidate in used_ids:
        candidate = f"{slug}_{i}"
        i += 1
    used_ids.add(candidate)
    label_to_id[label] = candidate
    return candidate


def repair_mermaid_quoted_endpoint_labels(text: str) -> tuple[str, list[dict]]:
    r"""Bracket bare quoted connection endpoints that carry a *label*.

    An endpoint like ``"Plan work" --> "Ship it"`` is invalid mermaid: a
    quoted string with spaces/punctuation cannot stand in for a node. The
    LLM means it as display text, so we promote it to ``id["Plan work"]``
    with a synthesized, deduped id. Single-token endpoints (``"A1"``) are a
    legal bare id and are left for ``repair_mermaid_label_quotes`` to merely
    unquote — that keeps the id stable for any bare-id cross-references.

    Edge labels (``A -- "edge" --> B``) are untouched: the anchors require
    the quoted text to be adjacent to the arrow at the line's start/end.
    Idempotent — once rewritten to ``id["label"]`` neither anchor matches.
    """
    if not text:
        return text, []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False
    label_to_id: dict[str, str] = {}
    used_ids: set[str] = set()

    def rewrite(line: str, idx: int) -> str:
        def repl_start(m: re.Match) -> str:
            label = m.group(2)
            if _MERMAID_BARE_ID_RE.match(label):
                return m.group(0)
            nid = _synthesize_node_id(label, label_to_id, used_ids)
            return f'{m.group(1)}{nid}["{label}"] {m.group(3)}'

        def repl_end(m: re.Match) -> str:
            label = m.group(2)
            if _MERMAID_BARE_ID_RE.match(label):
                return m.group(0)
            nid = _synthesize_node_id(label, label_to_id, used_ids)
            return f'{m.group(1)} {nid}["{label}"]{m.group(3)}'

        new = _MERMAID_CONN_START_QUOTED_LABEL_RE.sub(repl_start, line)
        new = _MERMAID_CONN_END_QUOTED_LABEL_RE.sub(repl_end, new)
        if new != line:
            fixes.append(_make_fix(
                "bracketed_mermaid_quoted_endpoint",
                line=idx + 1,
                before=line,
                after=new,
            ))
        return new

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip().lower()
        if not in_mermaid and stripped == "```mermaid":
            in_mermaid = True
            label_to_id = {}
            # Seed used_ids with every id already present in this fence so a
            # synthesized id never collides with an author's node.
            used_ids = set()
            for look in lines[idx + 1:]:
                if look.strip() == "```":
                    break
                used_ids.update(_MERMAID_EXISTING_ID_RE.findall(_strip_mermaid_comment(look)))
            out.append(line)
        elif in_mermaid and stripped == "```":
            in_mermaid = False
            out.append(line)
        elif in_mermaid:
            out.append(rewrite(line, idx))
        else:
            out.append(line)
        idx += 1

    return "\n".join(out), fixes


def _peek_mermaid_kind(lines: list[str], fence_idx: int) -> str:
    """Leading keyword of the first non-empty content line in the fence opened
    at ``lines[fence_idx]`` (the ```` ```mermaid ```` line), lowercased.

    Used so flowchart-oriented repairs can recognise — and bail out of — blocks
    whose syntax they'd corrupt (notably ``mindmap``, which is indentation-based
    and rejects flowchart-style quoted labels). Returns '' if the fence is empty.
    """
    for look in lines[fence_idx + 1:]:
        s = look.strip()
        if not s:
            continue
        if s == "```":
            return ""
        return s.split()[0].lower()
    return ""


def repair_mermaid_label_quotes(text: str) -> tuple[str, list[dict]]:
    """Quote bare labels inside mermaid node shapes within fenced blocks.

    Skips ``mindmap`` blocks: their nodes are indentation-based, and adding
    flowchart-style quotes (``"分支"`` / ``id["分支"]``) is a parse error that
    takes the whole diagram down. Mindmap quote cleanup is handled by
    ``repair_mermaid_mindmap_labels``.
    """
    if not text:
        return "", []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False
    skip_block = False

    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if not in_mermaid and stripped == "```mermaid":
            in_mermaid = True
            skip_block = _peek_mermaid_kind(lines, idx).startswith("mindmap")
            out.append(line)
            continue
        if in_mermaid and stripped == "```":
            in_mermaid = False
            skip_block = False
            out.append(line)
            continue

        if in_mermaid and not skip_block:
            m = _QUOTED_NODE_DEF_RE.match(line)
            pre_processed = line
            if m:
                pre_processed = f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}"
            new_line, changed = _quote_labels_in_line(pre_processed)

            # Strip quotes around node IDs on connection lines
            conn_line = _MERMAID_CONN_START_QUOTED_ID_RE.sub(r'\1\2 \3', new_line)
            conn_line = _MERMAID_CONN_END_QUOTED_ID_RE.sub(r'\1 \2\3', conn_line)
            if conn_line != new_line:
                new_line = conn_line
                changed = True

            if pre_processed != line or changed:
                fixes.append(_make_fix(
                    "quoted_mermaid_labels",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
                out.append(new_line)
            else:
                out.append(line)
        else:
            out.append(line)

    return "\n".join(out), fixes


def repair_mermaid_mindmap_labels(text: str) -> tuple[str, list[dict]]:
    r"""Strip double quotes from ``mindmap`` node text inside mermaid fences.

    Mermaid ``mindmap`` is indentation-based and does NOT use flowchart-style
    quoted labels. A node written ``"分支"`` or ``id["分支"]`` (whether emitted by
    the model or introduced by an upstream quote pass) is a parse error that
    takes the whole diagram down. Mindmap text renders fine unquoted (CJK
    included), so we drop ``"`` from every mindmap content line — the root shape
    ``root(("主題"))`` collapses to the valid ``root((主題))``.

    Scoped to ``mindmap`` blocks only; other diagram kinds are untouched.
    Idempotent (a second pass finds no quotes left to strip).
    """
    if not text or '"' not in text:
        return text, []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False
    is_mindmap = False

    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if not in_mermaid and stripped == "```mermaid":
            in_mermaid = True
            is_mindmap = _peek_mermaid_kind(lines, idx).startswith("mindmap")
            out.append(line)
            continue
        if in_mermaid and stripped == "```":
            in_mermaid = False
            is_mindmap = False
            out.append(line)
            continue

        if in_mermaid and is_mindmap and '"' in line:
            new_line = line.replace('"', "")
            fixes.append(_make_fix(
                "stripped_mindmap_quotes",
                line=idx + 1,
                before=line,
                after=new_line,
            ))
            out.append(new_line)
        else:
            out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: LaTeX-in-label degradation ──────────────────────────────


def _mermaid_latex_to_plaintext(s: str) -> str:
    """Degrade KaTeX/LaTeX math in a mermaid label to readable plain text.

    `$$\\mathcal{T}_{New} \\cong \\mathcal{M}_0?$$` → `T_New ≅ M_0?`. Backslash
    counts are irrelevant (the quote-repair pass doubles them) since every
    backslash command is consumed; `\\"` is preserved as a label-quote escape.
    """
    s = s.replace("$$", "").replace("$", "")
    # Collapse styling wrappers (`\mathcal{T}` → `T`); loop for shallow nesting.
    prev = None
    while prev != s:
        prev = s
        s = _MERMAID_LATEX_WRAPPER_RE.sub(r"\1", s)
    for pattern, glyph in _MERMAID_LATEX_SYMBOL_RES:
        s = pattern.sub(glyph, s)
    s = _MERMAID_LATEX_SCRIPT_RE.sub(r"\1\2", s)
    s = s.replace("{", "").replace("}", "")
    s = _MERMAID_LATEX_UNKNOWN_CMD_RE.sub("", s)
    s = _MERMAID_LATEX_STRAY_SLASH_RE.sub("", s)
    # Collapse the whitespace the removed commands leave behind.
    s = re.sub(r"[ \t]{2,}", " ", s)
    return re.sub(r"\s+([,.;:?!])", r"\1", s).strip()


def _strip_latex_in_mermaid_line(line: str) -> tuple[str, bool]:
    """Degrade LaTeX inside each double-quoted label on one mermaid line.

    Only quoted segments containing a `$` or a `\\command` are touched, so
    ordinary labels (and the line's arrow/structure syntax) are left intact.
    """
    out: list[str] = []
    i, n = 0, len(line)
    changed = False
    while i < n:
        ch = line[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n and line[j] != '"':
            if line[j] == "\\" and j + 1 < n:
                j += 2
                continue
            j += 1
        inner = line[i + 1:j]
        if "$" in inner or re.search(r"\\[a-zA-Z]", inner):
            degraded = _mermaid_latex_to_plaintext(inner)
            if degraded != inner:
                changed = True
                inner = degraded
        out.append(f'"{inner}"')
        i = j + 1 if j < n else j
    return "".join(out), changed


def repair_mermaid_latex_labels(text: str) -> tuple[str, list[dict]]:
    """Degrade LaTeX math inside mermaid node labels to plain text.

    Runs after label-quoting so every label is already wrapped in `"..."`.
    Obsidian's mermaid renderer can't parse `$$...$$`/`\\command` inside a
    label and fails the whole diagram; this keeps the diagram renderable.
    """
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
            new_line, changed = _strip_latex_in_mermaid_line(line)
            if changed:
                fixes.append(_make_fix(
                    "stripped_mermaid_latex",
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


# ─── Markdown: Table formatting ──────────────────────────────────────────

# Match table separator row: e.g. `|---|`, `|:--|--:|`, etc.
_TABLE_SEP_RE = re.compile(r'^\|?[\s\-\:\.\|]+\|?$')

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
                    next_stripped = lines[i+1].strip()
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
                    fixes.append(_make_fix(
                        "repaired_table_separator_columns",
                        line=i+1,
                        before=line,
                        after=new_line,
                    ))
                    out.append(new_line)
                else:
                    out.append(line)
            # Data row
            else:
                if pipes < expected_pipes:
                    diff = expected_pipes - pipes
                    new_line = line.rstrip() + "".join(["   |" for _ in range(diff)])
                    fixes.append(_make_fix(
                        "repaired_table_data_columns",
                        line=i+1,
                        before=line,
                        after=new_line,
                    ))
                    out.append(new_line)
                else:
                    out.append(line)
            i += 1
        elif not stripped:
            in_table = False
            out.append(line)
            i += 1
        else:
            # Interspersed text detection
            table_continues = False
            for look in range(i+1, min(i+6, len(lines))):
                if lines[look].strip().startswith("|") and lines[look].strip().endswith("|"):
                    table_continues = True
                    break
                if not lines[look].strip():
                    break
                    
            if table_continues:
                fixes.append(_make_fix(
                    "hidden_interspersed_table_text",
                    line=i+1,
                    before=line,
                    after=f"<!-- {line} -->",
                ))
                # To prevent breaking blockquotes or lists inside HTML comments, we just wrap it simply
                out.append(f"<!-- {line} -->")
                i += 1
            else:
                in_table = False
                out.append(line)
                i += 1

    return "\n".join(out), fixes


# ─── Markdown: bold spacing ────────────────────────────────────────────

_BOLD_BLOCK_RE = re.compile(r'(?<!\*)\*\*(.+?)\*\*(?!\*)')

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
            
        char_before = text[start-1] if start > 0 else " "
        char_after = text[end] if end < len(text) else " "
        
        needs_space_before = char_before not in (" ", "\n", "\t", "*", "「", "『", "(", "[")
        needs_space_after = char_after not in (" ", "\n", "\t", "*", "」", "』", ")", "]", "，", "。", "！", "？", ",", ".", "!", "?", "：", ":", "；", ";")
        
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
        fixes.append(_make_fix(
            "repaired_bold_spacing",
            line=line_no,
            before=match.group(0),
            after=res,
        ))
        last_end = end
        
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
        repair_mermaid_subgraph_keyword,
        repair_mermaid_quoted_node_ids,
        repair_mermaid_double_quotes,
        repair_mermaid_quoted_endpoint_labels,
        repair_mermaid_label_quotes,
        repair_mermaid_mindmap_labels,
        repair_mermaid_latex_labels,
        repair_markdown_tables,
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


# A backslash that does NOT begin a valid JSON escape (`\" \\ \/ \b \f \n \r \t`
# or `\uXXXX`). LLMs routinely emit LaTeX/math (`$\Delta \chi^2$`, `\mathcal`,
# `\frac`) — and Windows paths — inside JSON string values, where `\D` / `\c`
# are illegal escapes that make json.loads reject the WHOLE object. argument_map
# on academic content was silently producing nothing for exactly this reason.
_ILLEGAL_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


def _loads_lenient(candidate: str):
    """json.loads, retried once with illegal backslash-escapes doubled.

    Strict parse first (so valid JSON is never altered); only on failure do we
    repair `\\X`→`\\\\X` for any X that isn't a legal escape and reparse. Returns
    the parsed value or raises the original error."""
    try:
        return json.loads(candidate)
    except Exception:
        repaired = _ILLEGAL_JSON_ESCAPE_RE.sub(r'\\\\', candidate)
        return json.loads(repaired)   # may raise — caller handles


def extract_json_array(text: str) -> list:
    """Extract a JSON array of dicts from LLM output."""
    if not text:
        return []
    for candidate in _candidate_payloads(text):
        try:
            parsed = _loads_lenient(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        match = re.search(r'\[.*\]', candidate, re.DOTALL)
        if match:
            try:
                parsed = _loads_lenient(match.group(0))
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
            parsed = _loads_lenient(candidate)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        # Embedded object: raw_decode from each `{`, strict first then with
        # illegal backslash-escapes repaired (same LaTeX/path failure mode).
        for variant in (candidate, _ILLEGAL_JSON_ESCAPE_RE.sub(r'\\\\', candidate)):
            for match in re.finditer(r'\{', variant):
                try:
                    parsed, _ = decoder.raw_decode(variant[match.start():])
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return {}


_CODE_FENCE_RE = re.compile(r'^```(?:json)?\s*|\s*```$', re.IGNORECASE)


def is_empty_json_literal(text: str, kind: str = "array") -> bool:
    """True only when the WHOLE reply is a genuine empty JSON literal.

    Distinguishes a real empty answer (the model emitted `[]` / `{}`, perhaps
    fenced or padded) from a parse miss whose text merely *contains* `[]`/`{}`
    as a substring (e.g. `{"items": []}`). Callers use this to decide whether
    a re-roll is warranted: a substring check wrongly suppresses the retry and
    masks parse failures (audit B1).
    """
    if not text:
        return False
    stripped = _CODE_FENCE_RE.sub("", text.strip()).strip()
    stripped = re.sub(r"\s+", "", stripped)
    return stripped == ("[]" if kind == "array" else "{}")
