"""Markdown / Mermaid / JSON helpers used throughout the agent pipeline.

The public surface (callers across agents/, services/, watchers/, maintenance/):

    parse_markdown_metadata(content)        -> dict
    dump_markdown_with_metadata(meta, body) -> str
    clean_llm_response(text)                -> str
    run_markdown_quality_checks(text, ...)  -> (str, list[dict])
    repair_mermaid_fences(text)             -> (str, list[dict])
    repair_mermaid_quoted_endpoint_labels(text) -> (str, list[dict])
    repair_mermaid_label_quotes(text)       -> (str, list[dict])
    repair_mermaid_quadrant_points(text)    -> (str, list[dict])
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
# A quoted node *declaration* whose id text can't be a bare id, e.g.
# `"First Edition (1908)"["第一版"]`. The quoted string is the node id (a label
# follows in the shape); it must map to the SAME synthesized id the node's edges
# and `style`/`class` lines use, or the declaration and the edges render as two
# different nodes. Anchored at line start; lookahead requires a shape opener.
_MERMAID_QUOTED_DECL_RE = re.compile(r'^(\s*)"([^"\n]+)"(?=[\[\(\{>])')
# `style "X" ...` / `class "X" ...` / `click "X" ...` — the quoted token is a
# node id reference, so it must resolve to the same synthesized id as the node.
_MERMAID_QUOTED_STYLE_TARGET_RE = re.compile(
    r'^(\s*)(style|class|click)\s+"([^"\n]+)"'
)
# quadrantChart data point: `<name>: [x, y]`. Mermaid requires the point name in
# double quotes; the LLM routinely drops them (esp. for CJK/space names), which
# fails the whole chart. Lookahead skips lines whose name is already quoted or a
# `%%` comment. Trailing styling (e.g. ` radius: 5`) after the coords is allowed.
_MERMAID_QUADRANT_POINT_RE = re.compile(
    r'^(\s*)([^"%\s].*?)\s*:\s*(\[\s*[\d.]+\s*,\s*[\d.]+\s*\].*)$'
)
# Pre-scan: collect ids already used in a fence so a synthesized id can't
# collide with an author's node. Leading id, or id immediately before a shape.
_MERMAID_EXISTING_ID_RE = re.compile(
    r'(?:^\s*|' + _MERMAID_CONN_ARROW_PAT + r'\s*)([\w\-一-鿿]+)(?=\s|[\[\(\{>]|$)'
)
# Synthesized ids are forced to pure ASCII (the generation prompt mandates
# English-only ids). A label with no usable ASCII — e.g. an all-CJK endpoint —
# slugs to empty and falls back to a synthetic `node`/`node_1` id; the CJK text
# survives in the bracketed label. `\w` would keep CJK/accented chars, so we
# restrict to the ASCII id alphabet explicitly.
_MERMAID_ID_SLUG_RE = re.compile(r'[^A-Za-z0-9_]')
# classDiagram declaration: `class Id`, optional `["label"]`, optional `{` body
# opener. Used to dedup repeated declarations and to know which ids already
# exist before hoisting inline labels.
_CLASSDIAGRAM_DECL_RE = re.compile(
    r'^(\s*)class\s+([A-Za-z_]\w*)\s*(\["[^"\n]*"\]|\[[^\]\n]*\])?\s*(\{)?\s*$'
)
# An inline `Id["label"]` token. Legal only right after `class`; on a
# relationship line (`A *-- B["label"]`) it's flowchart syntax that classDiagram
# rejects, so the label must be hoisted to a real `class Id["label"]` decl.
_CLASSDIAGRAM_INLINE_LABEL_RE = re.compile(r'(?<![\w"])([A-Za-z_]\w*)\["([^"\n]*)"\]')
# A class declaration that OPENS a member body: `class Id["label"] {` (possibly
# closing inline, `class Id { <> }`). Group 4 captures everything after the `{`.
_CLASSDIAGRAM_BODY_OPEN_RE = re.compile(
    r'^(\s*)class\s+([A-Za-z_]\w*)\s*(\["[^"\n]*"\]|\[[^\]\n]*\])?\s*\{(.*)$'
)

# A LaTeX `\r…` command (\rightarrow, \rangle, …) emitted in under-escaped JSON
# decodes to a carriage-return CONTROL char + the suffix, not a literal `\r`.
# Match the CR char (\x0d) so the corruption is actually repaired; the suffix
# list keeps real CRLF line endings (CR not followed by these) untouched.
LATEX_CR_COMMAND_RE = re.compile("\r" + r"(ightarrow|ight|angle|brace|ceil|floor|vert|Vert)\b")

# Finds `${\displaystyle ... $` blocks for unclosed brace repair
UNCLOSED_LATEX_DISPLAY_RE = re.compile(r'\$\{\\displaystyle(.*?)(?<!\\)\$', re.DOTALL)

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
    "ldots": "…", "cdots": "…", "dotsc": "…", "dotsb": "…", "dots": "…",
    "vdots": "⋮", "ddots": "⋱", "langle": "⟨", "rangle": "⟩",
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
# `_{sub}` / `^{sup}` → keep the marker; wrap multi-char scripts in parens so
# grouping survives (`y^{m-1}` → `y^(m-1)`, not the ambiguous `y^m-1`). A
# single-char script drops its braces (`y^{2}` → `y^2`).
_MERMAID_LATEX_SCRIPT_RE = re.compile(r'([_^])\{([^{}]*)\}')


def _mermaid_script_repl(m: "re.Match") -> str:
    marker, content = m.group(1), m.group(2)
    # Parenthesize only when grouping is ambiguous — an operator or space inside
    # (`y^{m-1}` → `y^(m-1)`). Plain word/number scripts read fine bare
    # (`T_{New}` → `T_New`, `x^{2}` → `x^2`).
    if re.search(r'[-+*/ ,]', content):
        return f"{marker}({content})"
    return f"{marker}{content}"
# Any backslash command we don't have a glyph for: drop it entirely.
_MERMAID_LATEX_UNKNOWN_CMD_RE = re.compile(r'\\+[a-zA-Z]+')
# A stray backslash that is NOT escaping a double-quote (`\"` is a legitimate
# mermaid label escape and must survive).
_MERMAID_LATEX_STRAY_SLASH_RE = re.compile(r'\\(?!")')
# A math span inside a label: `$$...$$` (mermaid's KaTeX delimiter) or a single
# `$...$`. Mermaid only renders the `$$...$$` form, so single-`$` math is
# normalized up to `$$...$$`; existing `$$...$$` is matched first and kept.
_MERMAID_MATH_SPAN_RE = re.compile(r'\$\$.+?\$\$|\$[^$\n]+?\$')

# LaTeX commands whose leading `\<letter>` was eaten by a JSON escape collision
# (`\t`/`\n`/`\f`/`\r`/`\b`/`\v`) AND whose control char was later flattened to a
# space — leaving a bare command tail (`\frac`→`rac`, `\theta`→`heta`). The
# control char is gone, so this can't be recovered deterministically in PLAIN
# text. But INSIDE a `$...$` math span the ambiguity vanishes: a bare known tail
# is always a corrupted command. Each recovery is anchored (not preceded/followed
# by a letter or backslash; argument-taking ones require a following `{`) and
# only multi-char, unambiguous tails are included — single-letter tails like
# `o` (`\to`) / `u` (`\nu`) are too easily real variables and are left alone.
_LATEX_MATH_COMMAND_RECOVERIES: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat), repl) for pat, repl in (
        (r'(?<![\\A-Za-z])rac(?=\{)', r'\\frac'),
        (r'(?<![\\A-Za-z])inom(?=\{)', r'\\binom'),
        (r'(?<![\\A-Za-z])ec(?=\{)', r'\\vec'),
        (r'(?<![\\A-Za-z])ightarrow(?![A-Za-z])', r'\\rightarrow'),
        (r'(?<![\\A-Za-z])riangle(?![A-Za-z])', r'\\triangle'),
        (r'(?<![\\A-Za-z])orall(?![A-Za-z])', r'\\forall'),
        (r'(?<![\\A-Za-z])heta(?![A-Za-z])', r'\\theta'),
        (r'(?<![\\A-Za-z])imes(?![A-Za-z])', r'\\times'),
        (r'(?<![\\A-Za-z])abla(?![A-Za-z])', r'\\nabla'),
        (r'(?<![\\A-Za-z])eta(?![A-Za-z])', r'\\beta'),
        (r'(?<![\\A-Za-z])ho(?![A-Za-z])', r'\\rho'),
        (r'(?<![\\A-Za-z])au(?![A-Za-z])', r'\\tau'),
        (r'(?<![\\A-Za-z])eq(?![A-Za-z])', r'\\neq'),
    )
)


def _restore_math_commands(s: str) -> str:
    """Restore backslash-eaten LaTeX commands inside a math span (see map)."""
    for pat, repl in _LATEX_MATH_COMMAND_RECOVERIES:
        s = pat.sub(repl, s)
    return s


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


def repair_unclosed_latex_display(text: str) -> tuple[str, list[dict]]:
    """Repair missing closing brace in `${\\displaystyle ... $` math blocks.
    
    LLMs sometimes output `${\\displaystyle X$` instead of `${\\displaystyle X}$`.
    This pass counts braces between `${` and the next `$` and inserts a closing 
    brace if one is missing.
    """
    if not text or "${\\displaystyle" not in text:
        return text, []
        
    fixes: list[dict] = []
    parts: list[str] = []
    last_end = 0
    
    for match in UNCLOSED_LATEX_DISPLAY_RE.finditer(text):
        content = match.group(1)
        full_math = "${\\displaystyle" + content
        if full_math.count('{') > full_math.count('}'):
            diff = full_math.count('{') - full_math.count('}')
            before = match.group(0)
            after = full_math + ("}" * diff) + "$"
            
            parts.append(text[last_end:match.start()])
            parts.append(after)
            
            line_no = text.count("\n", 0, match.start()) + 1
            fixes.append(_make_fix(
                "repaired_unclosed_latex_display",
                line=line_no,
                before=before,
                after=after
            ))
            last_end = match.end()
        else:
            parts.append(text[last_end:match.end()])
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
# ASCII malformations of the keyword: `sub graph "t"` (split by space) or
# `subsubgraph "t"` / `subsubsubgraph "t"` (the prefix doubled). The inner
# group requires at least one space or extra `sub`, so a valid `subgraph`
# (just `sub`+`graph`, nothing between) never matches.
_SUBGRAPH_ASCII_MALFORMED_RE = re.compile(
    r'^(\s*)sub(?:\s+|sub)+graph\b(.*)$',
    re.IGNORECASE,
)


def repair_mermaid_subgraph_keyword(text: str) -> tuple[str, list[dict]]:
    """Fix truncated ``subgraph`` keywords inside mermaid fences.

    LLMs sometimes replace the ``graph`` part of ``subgraph`` with CJK or
    other text when generating bilingual diagrams:
    ``sub定的 "title"`` → ``subgraph "title"``.

    Also normalizes ASCII manglings of the keyword — ``sub graph "title"``
    (split by a space) and ``subsubgraph "title"`` (prefix doubled) — both
    back to ``subgraph "title"``.
    """
    if not text or "sub" not in text.lower():
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
            else:
                a = _SUBGRAPH_ASCII_MALFORMED_RE.match(line)
                new_line = f"{a.group(1)}subgraph{a.group(2)}" if a else line
            if new_line != line:
                fixes.append(_make_fix(
                    "repaired_mermaid_subgraph_keyword",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
            out.append(new_line)
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


# ─── Mermaid: over-quoted node repair ─────────────────────────────────

# `"Id["label"]"` — the LLM wrapped a whole `id["label"]` node in an extra pair
# of outer quotes (often only on a connection endpoint: `--> "Id["label"]"`).
# `repair_mermaid_quoted_node_ids` misses it (there's no `"` between id and the
# shape opener), so the outer quotes survive and break the line. Strip them back
# to a bare `id["label"]`. Handles the `[]`, `()` and `{}` shapes.
_MERMAID_OVERQUOTED_NODE_RE = re.compile(
    r'"([A-Za-z_]\w*)([\[\(\{])"([^"\n]*)"([\]\)\}])"'
)
_MERMAID_SHAPE_CLOSERS = {"[": "]", "(": ")", "{": "}"}


def repair_mermaid_overquoted_node(text: str) -> tuple[str, list[dict]]:
    """Strip an extra outer quote pair wrapping a whole ``id["label"]`` node.

    Only rewrites when the shape brackets match (``[`` with ``]`` etc.), so a
    coincidental quote run is left alone. Idempotent — once the outer quotes are
    gone the pattern no longer matches.
    """
    if not text or '"' not in text:
        return text, []

    def _repl(m: re.Match) -> str:
        opener, closer = m.group(2), m.group(4)
        if _MERMAID_SHAPE_CLOSERS[opener] != closer:
            return m.group(0)
        return f'{m.group(1)}{opener}"{m.group(3)}"{closer}'

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

        if in_mermaid and '"' in line:
            new_line = _MERMAID_OVERQUOTED_NODE_RE.sub(_repl, line)
            if new_line != line:
                fixes.append(_make_fix(
                    "stripped_mermaid_overquoted_node",
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
    share one node). The id is an ASCII-only slug of the label so it reads
    sensibly and naturally dedups; an all-CJK label slugs to empty and falls
    back to ``node`` (CJK survives in the label). On a slug collision with a
    *different* label or an author's existing id, a counter suffix is appended.
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
    r"""Canonicalize quoted node ids that carry a *label* across a whole fence.

    A quoted string with spaces/punctuation can't stand in for a node id, yet
    the LLM uses it as one in three places that must all agree:

    * edge endpoints — ``"Plan work" --> "Ship it"``
    * node declarations — ``"First Edition (1908)"["第一版"]``
    * style/class/click targets — ``style "First Edition (1908)" fill:#f9f``

    Each is promoted to a synthesized, deduped id, keyed by the quoted text so
    *the same string always resolves to the same id* fence-wide — otherwise the
    declaration, its edges and its style line drift into separate nodes (which
    is exactly how duplicate/dangling nodes and dead `style` lines arise). When
    a declaration already gives the id a label, later endpoints reuse the bare
    id rather than re-emitting a competing label.

    Single-token endpoints (``"A1"``) are a legal bare id and are left for
    ``repair_mermaid_label_quotes`` to merely unquote, keeping the id stable for
    bare-id cross-references. Edge labels (``A -- "edge" --> B``) are untouched:
    the anchors require the quoted text adjacent to the arrow at the line's
    start/end. Idempotent — once rewritten, none of the anchors match again.
    """
    if not text:
        return text, []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False
    label_to_id: dict[str, str] = {}
    used_ids: set[str] = set()
    labeled_ids: set[str] = set()

    def rewrite(line: str, idx: int) -> str:
        def repl_decl(m: re.Match) -> str:
            label = m.group(2)
            if _MERMAID_BARE_ID_RE.match(label):
                return m.group(0)
            nid = _synthesize_node_id(label, label_to_id, used_ids)
            labeled_ids.add(nid)
            return f'{m.group(1)}{nid}'

        def repl_style(m: re.Match) -> str:
            target = m.group(3)
            if _MERMAID_BARE_ID_RE.match(target):
                return m.group(0)
            nid = _synthesize_node_id(target, label_to_id, used_ids)
            return f'{m.group(1)}{m.group(2)} {nid}'

        def repl_start(m: re.Match) -> str:
            label = m.group(2)
            if _MERMAID_BARE_ID_RE.match(label):
                return m.group(0)
            nid = _synthesize_node_id(label, label_to_id, used_ids)
            if nid in labeled_ids:
                return f'{m.group(1)}{nid} {m.group(3)}'
            return f'{m.group(1)}{nid}["{label}"] {m.group(3)}'

        def repl_end(m: re.Match) -> str:
            label = m.group(2)
            if _MERMAID_BARE_ID_RE.match(label):
                return m.group(0)
            nid = _synthesize_node_id(label, label_to_id, used_ids)
            if nid in labeled_ids:
                return f'{m.group(1)} {nid}{m.group(3)}'
            return f'{m.group(1)} {nid}["{label}"]{m.group(3)}'

        new = _MERMAID_QUOTED_DECL_RE.sub(repl_decl, line)
        new = _MERMAID_QUOTED_STYLE_TARGET_RE.sub(repl_style, new)
        new = _MERMAID_CONN_START_QUOTED_LABEL_RE.sub(repl_start, new)
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
            labeled_ids = set()
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


# ─── Mermaid: mindmap bracket neutralization ──────────────────────────

_MINDMAP_HALF_TO_FULL = {
    "(": "（", ")": "）", "[": "［", "]": "］", "{": "｛", "}": "｝",
}
# Mindmap node shapes, longest/double markers first so `((` isn't shadowed by
# `(`. A shape wraps the WHOLE label; brackets there are syntax, not text.
_MINDMAP_SHAPE_PAIRS = (
    ("((", "))"), ("))", "(("), ("{{", "}}"),
    ("[", "]"), ("(", ")"), (")", "("), ("{", "}"),
)


def _neutralize_brackets(s: str) -> str:
    return "".join(_MINDMAP_HALF_TO_FULL.get(c, c) for c in s)


def _fix_mindmap_brackets(content: str) -> str:
    """Convert half-width brackets in a mindmap node's *text* to full-width.

    Mermaid mindmap reads ``()``/``[]``/``{}`` as node-shape delimiters, so a
    label like ``證明 sqrt(2) 為無理數`` is misparsed (the ``(2)`` looks like a
    rounded shape) and breaks the diagram. A leading shape wrapper that spans
    the whole label (``root((主題))``, ``(說明)``) is legitimate and preserved —
    only its *interior* brackets are neutralized.
    """
    if not any(c in content for c in "()[]{}"):
        return content
    head, rest = re.match(r'^([\w\-]*)(.*)$', content, re.UNICODE).groups()
    for open_, close_ in _MINDMAP_SHAPE_PAIRS:
        if (rest.startswith(open_) and rest.endswith(close_)
                and len(rest) >= len(open_) + len(close_)):
            inner = rest[len(open_):len(rest) - len(close_)]
            return f"{head}{open_}{_neutralize_brackets(inner)}{close_}"
    return f"{head}{_neutralize_brackets(rest)}"


def repair_mermaid_mindmap_brackets(text: str) -> tuple[str, list[dict]]:
    """Neutralize stray half-width brackets in ``mindmap`` node text.

    Scoped to ``mindmap`` fences only (other kinds use brackets as real shape
    syntax). Idempotent — once a label's brackets are full-width, the quick
    bracket check finds nothing left to convert.
    """
    if not text or "mindmap" not in text:
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

        if in_mermaid and is_mindmap and line.strip() and any(c in line for c in "()[]{}"):
            indent = line[:len(line) - len(line.lstrip())]
            new_content = _fix_mindmap_brackets(line.strip())
            new_line = f"{indent}{new_content}"
            if new_line != line:
                fixes.append(_make_fix(
                    "neutralized_mindmap_brackets",
                    line=idx + 1,
                    before=line,
                    after=new_line,
                ))
            out.append(new_line)
        else:
            out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: quadrantChart point quoting ─────────────────────────────


def repair_mermaid_quadrant_points(text: str) -> tuple[str, list[dict]]:
    r"""Wrap ``quadrantChart`` data-point names in double quotes.

    A point is written ``"<name>": [x, y]``; Mermaid requires the quotes and the
    LLM routinely omits them (``Campaign A: [0.3, 0.6]``), which fails the whole
    chart — especially for names with spaces or CJK. We quote the bare name on
    every point line inside a ``quadrantChart`` fence, leaving the axis / title /
    ``quadrant-N`` definition lines (no ``: [x, y]`` shape) untouched.

    Scoped to ``quadrantChart`` blocks only. Idempotent (already-quoted names are
    skipped by the regex lookahead).
    """
    if not text or "[" not in text:
        return text, []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    in_mermaid = False
    is_quadrant = False

    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if not in_mermaid and stripped == "```mermaid":
            in_mermaid = True
            is_quadrant = _peek_mermaid_kind(lines, idx).startswith("quadrantchart")
            out.append(line)
            continue
        if in_mermaid and stripped == "```":
            in_mermaid = False
            is_quadrant = False
            out.append(line)
            continue

        if in_mermaid and is_quadrant:
            m = _MERMAID_QUADRANT_POINT_RE.match(line)
            if m:
                new_line = f'{m.group(1)}"{m.group(2)}": {m.group(3)}'
                if new_line != line:
                    fixes.append(_make_fix(
                        "quoted_quadrant_point",
                        line=idx + 1,
                        before=line,
                        after=new_line,
                    ))
                    out.append(new_line)
                    continue
        out.append(line)

    return "\n".join(out), fixes


# ─── Mermaid: classDiagram structural repair ──────────────────────────


def _repair_classdiagram_body(body: list[str], base_line: int) -> tuple[list[str], list[dict]]:
    """Fix two structural faults inside one ``classDiagram`` fence body.

    1. **Hoist inline labels.** ``A *-- B["label"]`` is invalid: classDiagram
       relationship endpoints are bare class ids. The label is hoisted to a real
       ``class B["label"]`` declaration (added once, after the header) and the
       relationship is rewritten to the bare id.
    2. **Dedup declarations.** A class declared twice with the *same* line (the
       LLM repeats ``class X["..."]``) renders as a redundant/duplicate entry;
       the later exact-duplicate decls are dropped.
    3. **Strip empty/malformed bodies.** A ``{ ... }`` body that holds no real
       member or stereotype — ``{}``, ``{ <> }``, ``{ <<>> }`` — is dropped to a
       bare ``class X["label"]``. A body with a genuine stereotype
       (``<<instance>>``) or attribute (``+name string``) is kept intact.

    Idempotent: once hoisted, deduped and stripped, no pattern matches again.
    """
    fixes: list[dict] = []

    # Pre-scan: ids already given a `class` declaration (any form), and the
    # indentation to reuse for hoisted declarations.
    existing_ids: set[str] = set()
    indent = "    "
    header_idx = 0
    for i, ln in enumerate(body):
        if ln.strip():
            header_idx = i
            break
    for ln in body:
        m = _CLASSDIAGRAM_DECL_RE.match(ln) or _CLASSDIAGRAM_BODY_OPEN_RE.match(ln)
        if m:
            existing_ids.add(m.group(2))
            indent = m.group(1) or indent

    out: list[str] = []
    hoisted: list[tuple[str, str]] = []
    hoisted_ids: set[str] = set()
    seen_decls: set[str] = set()

    idx = 0
    while idx < len(body):
        line = body[idx]

        # A class declaration that opens a `{ ... }` member body — capture the
        # whole block (it may span lines or close inline) and decide keep/strip.
        bopen = _CLASSDIAGRAM_BODY_OPEN_RE.match(line)
        if bopen:
            start = idx
            depth = line.count("{") - line.count("}")
            block = [line]
            while depth > 0 and idx + 1 < len(body):
                idx += 1
                block.append(body[idx])
                depth += body[idx].count("{") - body[idx].count("}")
            if depth > 0:
                # Unclosed body — don't risk swallowing following lines.
                out.append(line)
                idx = start + 1
                continue
            full = "\n".join(block)
            inner = full[full.index("{") + 1:full.rindex("}")]
            if re.sub(r'[{}<>\s]', "", inner) == "":
                # No real member/stereotype — degrade to a bare declaration.
                label = bopen.group(3) or ""
                bare = f'{bopen.group(1)}class {bopen.group(2)}{label}'
                fixes.append(_make_fix(
                    "stripped_empty_classdiagram_body",
                    line=base_line + start,
                    before=full,
                    after=bare,
                ))
                out.append(bare)
            else:
                out.extend(block)
            idx += 1
            continue

        decl = _CLASSDIAGRAM_DECL_RE.match(line)
        if decl:
            key = line.strip()
            if key in seen_decls:
                fixes.append(_make_fix(
                    "deduped_classdiagram_decl",
                    line=base_line + idx,
                    before=line,
                ))
                idx += 1
                continue  # drop the exact-duplicate declaration
            seen_decls.add(key)
            out.append(line)
            idx += 1
            continue

        # Non-declaration line (relationship / member shorthand): hoist any
        # inline `Id["label"]` to a class declaration, leaving the bare id.
        def _hoist(m: re.Match) -> str:
            cid, label = m.group(1), m.group(2)
            if cid not in existing_ids and cid not in hoisted_ids:
                hoisted.append((cid, label))
                hoisted_ids.add(cid)
            return cid

        new_line = _CLASSDIAGRAM_INLINE_LABEL_RE.sub(_hoist, line)
        if new_line != line:
            fixes.append(_make_fix(
                "hoisted_classdiagram_inline_label",
                line=base_line + idx,
                before=line,
                after=new_line,
            ))
        out.append(new_line)
        idx += 1

    if hoisted:
        decls = [f'{indent}class {cid}["{label}"]' for cid, label in hoisted]
        out[header_idx + 1:header_idx + 1] = decls

    return out, fixes


def repair_mermaid_classdiagram(text: str) -> tuple[str, list[dict]]:
    """Scope ``_repair_classdiagram_body`` to ``classDiagram`` fences only.

    Other diagram kinds are passed through untouched — the inline-label and
    duplicate-declaration faults are specific to classDiagram (ontology) output.
    """
    if not text or "class" not in text:
        return text, []

    lines = text.splitlines()
    out: list[str] = []
    fixes: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip().lower() == "```mermaid":
            j = i + 1
            while j < n and lines[j].strip() != "```":
                j += 1
            if _peek_mermaid_kind(lines, i).startswith("classdiagram"):
                new_body, body_fixes = _repair_classdiagram_body(lines[i + 1:j], i + 2)
                out.append(line)
                out.extend(new_body)
                if j < n:
                    out.append(lines[j])
                fixes.extend(body_fixes)
                i = j + 1
                continue
        out.append(line)
        i += 1

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
    s = _MERMAID_LATEX_SCRIPT_RE.sub(_mermaid_script_repl, s)
    s = s.replace("{", "").replace("}", "")
    s = _MERMAID_LATEX_UNKNOWN_CMD_RE.sub("", s)
    s = _MERMAID_LATEX_STRAY_SLASH_RE.sub("", s)
    # Collapse the whitespace the removed commands leave behind.
    s = re.sub(r"[ \t]{2,}", " ", s)
    return re.sub(r"\s+([,.;:?!])", r"\1", s).strip()


def _normalize_math_span(m: "re.Match") -> str:
    seg = m.group(0)
    body = seg[2:-2] if seg.startswith("$$") else seg[1:-1]
    return f"$${_restore_math_commands(body)}$$"


def _normalize_math_in_mermaid_line(line: str) -> tuple[str, str | None]:
    r"""Make LaTeX inside each double-quoted label render in mermaid's KaTeX.

    Per quoted label:
      * Contains ``$`` math → keep ``$$...$$`` and promote single ``$...$`` to
        ``$$...$$`` (the only form mermaid renders). The commands inside are
        left intact for KaTeX.
      * Contains a bare ``\command`` but NO ``$`` (KaTeX never sees it) → degrade
        to unicode/plain text so the label doesn't show literal backslashes.

    Arrow/structure syntax outside quotes is never touched. Returns the new line
    and the fix-type that applies (or ``None`` if unchanged).
    """
    out: list[str] = []
    i, n = 0, len(line)
    action: str | None = None
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
        if "$" in inner:
            new_inner = _MERMAID_MATH_SPAN_RE.sub(_normalize_math_span, inner)
            if new_inner != inner:
                inner = new_inner
                action = "normalized_mermaid_math"
        elif re.search(r"\\[a-zA-Z]", inner):
            degraded = _mermaid_latex_to_plaintext(inner)
            if degraded != inner:
                inner = degraded
                action = action or "stripped_mermaid_latex"
        out.append(f'"{inner}"')
        i = j + 1 if j < n else j
    return "".join(out), action


def repair_mermaid_latex_labels(text: str) -> tuple[str, list[dict]]:
    r"""Normalize math inside mermaid node labels so it renders via KaTeX.

    The target renderers support mermaid's KaTeX math (``$$...$$`` inside a
    label), so we PRESERVE it and promote single ``$...$`` up to ``$$...$$``
    rather than stripping math out. A bare ``\command`` with no ``$`` delimiters
    (which KaTeX would never pick up) is still degraded to plain text.

    Runs after label-quoting so every label is already wrapped in ``"..."``.
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
            new_line, action = _normalize_math_in_mermaid_line(line)
            if action:
                fixes.append(_make_fix(
                    action,
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
                        fixes.append(_make_fix(
                            "restored_hidden_table_separator",
                            line=i+2,
                            before=lines[i+1],
                            after=clean_sep,
                        ))
                        lines[i+1] = clean_sep
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
            indent = line[:len(line) - len(line.lstrip())]

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
                    fixes.append(_make_fix(
                        "restored_hidden_table_row" if was_comment
                        else "repaired_table_row_missing_leading_pipe",
                        line=i+1,
                        before=line,
                        after=repaired,
                    ))
                lines[i] = repaired
                continue  # re-process the now well-formed row; do not advance i

            # Genuine non-row text that was previously hidden: collapse any
            # nesting back to a single comment layer instead of re-wrapping.
            if was_comment:
                collapsed = f"{indent}<!-- {inner} -->"
                if collapsed != line:
                    fixes.append(_make_fix(
                        "collapsed_nested_table_comment",
                        line=i+1,
                        before=line,
                        after=collapsed,
                    ))
                out.append(collapsed)
                i += 1
                continue

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
        repair_unclosed_latex_display,
        repair_mermaid_fences,
        repair_mermaid_subgraph_keyword,
        repair_mermaid_quoted_node_ids,
        repair_mermaid_overquoted_node,
        repair_mermaid_double_quotes,
        repair_mermaid_quoted_endpoint_labels,
        repair_mermaid_label_quotes,
        repair_mermaid_mindmap_labels,
        repair_mermaid_mindmap_brackets,
        repair_mermaid_quadrant_points,
        repair_mermaid_classdiagram,
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
