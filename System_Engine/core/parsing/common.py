"""Shared quality-fix record helpers for all repair modules.

Moved verbatim out of core/parser.py (P2a of the refactor roadmap).
"""

from __future__ import annotations


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
