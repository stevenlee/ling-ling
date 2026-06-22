"""Deterministic identifier correction for generated review/blog output.

Weak local models (e.g. gemma4:26b) mangle fragile alphanumeric identifiers
even when the correct value sits verbatim in their input — observed cases for
patent ``US-12634321-B1`` were ``US-126TR34321-B1`` (inserted letters) and
``US-126 34321-B1`` (inserted space). Prompt rules do not reliably fix this.

This pass needs no model: the canonical identifier is always known from the
source document's title / filename, so we scan the output for near-miss variants
and snap them back to the canonical form. Same spirit as the other deterministic
post-passes (table repair, == == highlighting).
"""
import re
from difflib import SequenceMatcher

# US patent grant/application numbers, e.g. US-12634321-B1, US 12634321 B1, USRE49000E1.
_PATENT_RE = re.compile(r"\bUS[\s\-–]*[0-9][0-9A-Za-z\s\-–]*[0-9A-Za-z]\b")
# A no-whitespace candidate used for fuzzy matching — safe from grabbing
# neighbouring words, but still catches inserted-letter corruption.
_PAT_CAND = re.compile(r"US[\-–]?[0-9A-Za-z][0-9A-Za-z\-–]{4,18}")

_DEFAULT_THRESHOLD = 0.82


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def _digit_count(s: str) -> int:
    return sum(c.isdigit() for c in s)


def extract_identifiers(*texts: str) -> list[str]:
    """Pull canonical identifiers (currently US patent numbers) from known-good
    text such as a document title or filename. Order-preserving, de-duplicated."""
    ids: list[str] = []
    for t in texts:
        for m in _PATENT_RE.finditer(t or ""):
            v = re.sub(r"[\s–]+", "", m.group(0)).strip()
            if v and v not in ids:
                ids.append(v)
    return ids


def _tolerant_pattern(canonical: str) -> re.Pattern:
    """Match the canonical with arbitrary separators (space/hyphen/dash) between
    its alphanumeric characters — i.e. formatting corruption only."""
    chars = [c for c in canonical if c.isalnum()]
    return re.compile(r"[\s\-–]*".join(re.escape(c) for c in chars))


def correct_identifiers(text: str, canonical_ids, threshold: float = _DEFAULT_THRESHOLD):
    """Snap mangled identifiers in ``text`` back to known-good ``canonical_ids``.

    Two passes, both safe (a canonical is ground truth, so replacing a near-match
    with it cannot lose information):
      A. separator-tolerant — fixes space/hyphen corruption precisely;
      B. fuzzy (no-whitespace candidates) — fixes inserted-letter/digit corruption
         when the normalized forms are >= ``threshold`` similar.

    Returns ``(corrected_text, fixes)`` where each fix is ``{"from","to"}``.
    """
    if not canonical_ids:
        return text, []

    fixes: list[dict] = []
    norm_canon = [(_norm(c), c) for c in canonical_ids]

    # Pass A: separator-tolerant, per canonical.
    for canon in canonical_ids:
        def _sub_a(m, _c=canon):
            if m.group(0) != _c:
                fixes.append({"from": m.group(0), "to": _c})
            return _c
        text = _tolerant_pattern(canon).sub(_sub_a, text)

    # Pass B: fuzzy over no-whitespace candidates.
    def _sub_b(m):
        cand = m.group(0)
        nc = _norm(cand)
        if _digit_count(nc) < 6:
            return cand
        ratio, canon = max(((SequenceMatcher(None, nc, k).ratio(), c) for k, c in norm_canon),
                           key=lambda x: x[0])
        if canon and cand != canon and ratio >= threshold:
            fixes.append({"from": cand, "to": canon})
            return canon
        return cand
    text = _PAT_CAND.sub(_sub_b, text)

    return text, fixes
