"""Lenient JSON extraction from LLM output (P1 of the refactor roadmap).

Moved verbatim from core/parser.py (which re-exports for compatibility) and
joined by the array-salvage logic that lived in llm_client._parse_json_array,
so every "get JSON out of a model reply" path shares one toolbox:

- extract_json_array / extract_json_object: fenced-first candidate scan,
  strict parse, then repair of illegal and LaTeX-shaped backslash escapes.
- salvage_json_array: additionally survives tail truncation and single
  malformed entries by parsing top-level {...} objects independently.
- is_empty_json_literal: tells a GENUINE empty answer ([] / {}) from a parse
  miss, so re-roll logic never mistakes a real zero for a failure.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _candidate_payloads(text: str) -> Iterable[str]:
    """Yield candidate JSON payloads: fenced first (if present), then raw."""
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        yield fenced.group(1).strip()
    yield text.strip()


# Backslash-escape repair for LLM JSON. Two failure families share one cause —
# LLMs routinely emit LaTeX/math (`$\Delta \chi^2$`, `\frac`) and Windows paths
# inside JSON string values without escaping the backslash:
#
# 1. `\D` / `\c` / `\p` are ILLEGAL escapes → json.loads rejects the WHOLE
#    object. argument_map on academic content was silently producing nothing.
# 2. `\forall` / `\neq` / `\tan` / `\binom` / `\rho` begin with a VALID escape
#    (`\f \n \t \b \r`) → json.loads "succeeds" and silently decodes them into
#    control characters (`\x0c orall`, newline + `eq`, tab + `an`, …), which
#    then poison embeddings (bge-m3 NaN) and split lines downstream
#    (facet_backfill's appendix parse truncated key points at the `\x0c`).
#
# The scanner below repairs both in one pass: legal structural escapes are
# kept; a control escape (`\b \f \n \r \t`) is kept only when NOT followed by
# a lowercase letter — followed by one, it is LaTeX-command-shaped and gets its
# backslash doubled. Tradeoff, repair path only (strict parse already failed):
# a genuine `\n` immediately followed by a lowercase letter becomes a literal
# `\n` in the text — cosmetic, vs. silent control-char corruption.
_JSON_ESCAPE_REPAIR_RE = re.compile(
    r"\\\\"  # already-escaped backslash — keep
    r"|\\u[0-9a-fA-F]{4}"  # unicode escape — keep
    r'|\\["/]'  # quote / solidus — keep
    r"|\\[bfnrt](?![a-z])"  # control escape NOT LaTeX-shaped — keep
    r"|\\"  # illegal escape or LaTeX-shaped control escape — double
)


def _repair_backslash_escapes(candidate: str) -> str:
    return _JSON_ESCAPE_REPAIR_RE.sub(
        lambda m: "\\\\" if m.group(0) == "\\" else m.group(0), candidate
    )


def _loads_lenient(candidate: str):
    """json.loads, retried once with suspect backslash-escapes doubled.

    Strict parse first (so valid JSON is never altered); only on failure do we
    repair illegal and LaTeX-shaped escapes (see `_JSON_ESCAPE_REPAIR_RE`) and
    reparse. Returns the parsed value or raises the original error."""
    try:
        return json.loads(candidate)
    except Exception:
        repaired = _repair_backslash_escapes(candidate)
        return json.loads(repaired)  # may raise — caller handles


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
        match = re.search(r"\[.*\]", candidate, re.DOTALL)
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
        # suspect backslash-escapes repaired (same LaTeX/path failure mode).
        for variant in (candidate, _repair_backslash_escapes(candidate)):
            for match in re.finditer(r"\{", variant):
                try:
                    parsed, _ = decoder.raw_decode(variant[match.start() :])
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return {}


def salvage_json_array(text: str) -> list:
    """Best-effort extraction of a JSON array from an LLM response.

    Tolerant of common local-model failure modes: ```json fences, a tail
    truncated at the output-token limit (no closing ]), or a single
    malformed object. The fast path parses the whole array; on failure it
    SALVAGES individual top-level objects so one cut-off/bad entry doesn't
    discard the entire list (the old behaviour — a single glitch anywhere
    returned [], which then looked like "the LLM produced nothing").
    """
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    # Salvage: parse each flat {...} object independently, keeping successes.
    objs = []
    for om in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            obj = json.loads(om.group(0))
            if isinstance(obj, dict):
                objs.append(obj)
        except Exception:
            continue
    return objs


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


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
