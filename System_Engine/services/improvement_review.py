"""Deterministic review-memory and prompt-contract checks for M3 proposals."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

_JSON_KEY_RE = re.compile(r"""["']([^"'\n]+)["']\s*:""")
_FALSIFIABILITY_KEYS = {"score", "falsifier", "falsifier_zh"}


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_proposal(value: object) -> bool:
    """Reject broken persistent records before they can block a target."""
    if not isinstance(value, dict):
        return False
    required = ("id", "created", "axis", "target_path", "original_sha")
    if not all(isinstance(value.get(k), str) and value[k].strip() for k in required):
        return False
    if not all(isinstance(value.get(k), str) for k in ("original_content", "revised_content")):
        return False
    if not value["revised_content"].strip():
        return False
    try:
        datetime.fromisoformat(value["created"])
    except ValueError:
        return False
    fixes = value.get("addressed_fixes", [])
    if not isinstance(fixes, list) or not all(isinstance(f, str) for f in fixes):
        return False
    return value["original_sha"] == content_sha(value["original_content"])


def same_target(proposal: dict, target: Path, vault: Path) -> bool:
    try:
        return (vault / proposal["target_path"].replace("\\", "/")).resolve() == target.resolve()
    except (KeyError, OSError, ValueError, RuntimeError):
        return False


def diagnosis_key(rationale: str, fixes: list) -> str:
    """Ignore wording whitespace and fix order, not differences in meaning."""

    def normalize(value):
        return " ".join(str(value).split())

    payload = [normalize(rationale), sorted(normalize(f) for f in fixes)]
    return content_sha(json.dumps(payload, ensure_ascii=False))


def edit_key(edits: object) -> str | None:
    """Ignore editorial `why`; preserve edit order and significant whitespace."""
    if not isinstance(edits, list) or not edits:
        return None
    pairs = []
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        find, replace = edit.get("find"), edit.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            return None
        pairs.append([find.replace("\r\n", "\n"), replace.replace("\r\n", "\n")])
    return content_sha(json.dumps(pairs, ensure_ascii=False))


def prompt_contract_error(target_path: str, original: str, revised: str) -> str:
    """Cortex score prompts cannot invent fields their consumer never reads."""
    if Path(target_path.replace("\\", "/")).name != "cortex_falsifiability.md":
        return ""
    before = set(_JSON_KEY_RE.findall(original))
    after = set(_JSON_KEY_RE.findall(revised))
    if after - _FALSIFIABILITY_KEYS or (before and after != _FALSIFIABILITY_KEYS):
        return "Cortex JSON 契約只能含 score／falsifier／falsifier_zh；欄位變更需工程實作。"
    return ""
