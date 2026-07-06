"""Deterministic identifier correction for generated CODE reviews.

Sibling of identifier_guard.py — that one is hardcoded for US patent numbers
(`US-...`, digit-heavy); it cannot protect code identifiers like `RecallAgent`
or `_load_prompt`. Weak local models (gemma4:26b) mangle these too: casing
(`recallagent`), separator (`recall_agent` ↔ `RecallAgent`), etc.

The canonical identifiers are known exactly — the pack-code CLI harvests them
with `ast` into the packed note's `identifiers:` frontmatter. So we snap
near-misses back, with a deliberately SAFE scope (v1):

  * only inside inline-code backticks (`` `foo` ``) — never rewrites prose;
  * only single identifier tokens (`^[A-Za-z][A-Za-z0-9_]*$`);
  * only "distinctive" canonicals (containing an uppercase letter, underscore,
    or digit) — a plain lowercase name like `main`/`run` is skipped, since it
    collides with ordinary words and builtins;
  * only a NORMALIZED-EXACT match (casing/separator differs, nothing else) that
    is UNIQUE — i.e. it is unambiguously the same identifier, just reformatted.

Fuzzy misspelling correction (e.g. `RecalAgent` → `RecallAgent`) is intentionally
NOT done here: it carries false-positive risk and needs its own analysis.
"""

import re

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*$")


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()


def _is_distinctive(name: str) -> bool:
    """A name unlikely to collide with an ordinary English word: has an
    uppercase letter, an underscore, or a digit."""
    return any(c.isupper() or c == "_" or c.isdigit() for c in name)


def correct_code_identifiers(text: str, canonical_ids):
    """Snap backtick-wrapped code identifiers in `text` back to the canonical
    spelling. Returns ``(corrected_text, fixes)`` (each fix ``{"from","to"}``).

    Safe by construction: a normalized-exact match to a distinctive canonical is
    that identifier reformatted, so replacing it cannot lose information."""
    if not canonical_ids:
        return text, []

    # normalized → canonical, but only for distinctive canonicals and only when
    # the normalized form is UNIQUE (drop ambiguous collisions entirely).
    by_norm: dict[str, str | None] = {}
    for cid in canonical_ids:
        if not _is_distinctive(cid):
            continue
        n = _norm(cid)
        by_norm[n] = None if n in by_norm and by_norm[n] != cid else cid
    resolved = {n: c for n, c in by_norm.items() if c is not None}
    if not resolved:
        return text, []

    fixes: list[dict] = []

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        if not _IDENT_RE.match(token):
            return m.group(0)
        canon = resolved.get(_norm(token))
        if canon and token != canon:
            fixes.append({"from": token, "to": canon})
            return f"`{canon}`"
        return m.group(0)

    return _BACKTICK_RE.sub(_sub, text), fixes
