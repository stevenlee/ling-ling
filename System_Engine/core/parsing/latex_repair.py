"""LaTeX repair: CR corruption, JSON-escape collisions, unclosed display math.

Moved verbatim out of core/parser.py (P2a of the refactor roadmap).
"""

from __future__ import annotations

import re

from core.parsing.common import _make_fix


# Match the CR char (\x0d) so the corruption is actually repaired; the suffix
# list keeps real CRLF line endings (CR not followed by these) untouched.
LATEX_CR_COMMAND_RE = re.compile("\r" + r"(ightarrow|ight|angle|brace|ceil|floor|vert|Vert)\b")

# Finds `${\displaystyle ... $` blocks for unclosed brace repair
UNCLOSED_LATEX_DISPLAY_RE = re.compile(r"\$\{\\displaystyle(.*?)(?<!\\)\$", re.DOTALL)

# Other JSON-escape collisions affecting LaTeX commands. When LLMs emit
# LaTeX inside a JSON string, they often forget to escape the backslash;
# json.loads then interprets `\binom` as <BS>inom, `\frac` as <FF>rac,
# `\vec` as <VT>ec. We restore the backslash here.
#
# Skipped: `\n` (collides with legit newlines) and `\t` (legit tabs).
# Both `\r` and the alternates below operate on the control characters
# left behind by JSON decoding, not on literal backslashes.
_LATEX_ESCAPE_COLLISIONS: tuple[tuple[str, str, str], ...] = (
    ("\x08", "b", "repaired_latex_backspace"),  # \b → \binom, \big, ...
    ("\x0c", "f", "repaired_latex_form_feed"),  # \f → \frac, \forall, ...
    ("\x0b", "v", "repaired_latex_vertical_tab"),  # \v → \vec, \vee, ...
    # ESC is not a JSON escape, but `$<ESC>ll_p$` (= `\ell_p`) was observed
    # across dozens of vault pages — some decode layer eats `\e` the same way.
    # ANSI sequences are safe: CSI starts with `[`, which the letter-run
    # pattern below never matches.
    ("\x1b", "e", "repaired_latex_escape_char"),  # \e → \ell, \epsilon, ...
)


def repair_latex_carriage_returns(text: str) -> tuple[str, list[dict]]:
    """Repair `\r` that should have been a literal `\\r` (LaTeX command)."""
    if not text:
        return "", []
    fixes: list[dict] = []
    parts: list[str] = []
    last_end = 0
    for match in LATEX_CR_COMMAND_RE.finditer(text):
        parts.append(text[last_end : match.start()])
        before = match.group(0)
        after = "\\r" + match.group(1)
        parts.append(after)
        line_no = text.count("\n", 0, match.start()) + 1
        fixes.append(
            _make_fix(
                "repaired_latex_carriage_return",
                line=line_no,
                before=before,
                after=after,
            )
        )
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
        if full_math.count("{") > full_math.count("}"):
            diff = full_math.count("{") - full_math.count("}")
            before = match.group(0)
            after = full_math + ("}" * diff) + "$"

            parts.append(text[last_end : match.start()])
            parts.append(after)

            line_no = text.count("\n", 0, match.start()) + 1
            fixes.append(
                _make_fix(
                    "repaired_unclosed_latex_display", line=line_no, before=before, after=after
                )
            )
            last_end = match.end()
        else:
            parts.append(text[last_end : match.end()])
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
            parts.append(cleaned[last_end : match.start()])
            suffix = match.group(1)
            before = control_char + suffix
            after = f"\\{latex_letter}{suffix}"
            parts.append(after)
            line_no = cleaned.count("\n", 0, match.start()) + 1
            fixes.append(
                _make_fix(
                    fix_type,
                    line=line_no,
                    before=before,
                    after=after,
                )
            )
            last_end = match.end()
        if any_match:
            parts.append(cleaned[last_end:])
            cleaned = "".join(parts)

    return cleaned, fixes
