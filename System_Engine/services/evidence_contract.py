"""Versioned passage-scope contract shared by the judge and apply boundary."""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = 2
RELATIONS = ("full_support", "partial_support", "analogy", "contradicts", "neutral")
PASSAGE_LIMIT = 2000


def validate_scope_verdict(value: Any, passage: str) -> dict:
    """Validate shape and literal citations, never semantic entailment or lineage."""
    if not isinstance(passage, str) or not passage.strip() or len(passage) > PASSAGE_LIMIT:
        raise ValueError("送審段落必須是預算內的非空字串")
    if not isinstance(value, dict):
        raise ValueError("判定必須是 object")
    version = value.get("contract_version")
    if type(version) is not int or version != CONTRACT_VERSION:
        raise ValueError("判定契約版本不符；舊 supports 不自動升級")
    relation = value.get("relation")
    if not isinstance(relation, str) or relation not in RELATIONS:
        raise ValueError("未知的支持範圍")
    for key in ("reason", "applicability"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{key} 必須是非空字串")
    for key in ("quotes", "unsupported"):
        items = value.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item.strip() for item in items
        ):
            raise ValueError(f"{key} 必須是非空字串組成的陣列")
    if relation != "neutral" and not value["quotes"]:
        raise ValueError("非中立判定必須提供逐字引文")
    if any(quote not in passage for quote in value["quotes"]):
        raise ValueError("引文不存在於送審段落")
    if relation == "full_support" and value["unsupported"]:
        raise ValueError("完整支持不得有未支持部分")
    if relation in ("partial_support", "analogy") and not value["unsupported"]:
        raise ValueError("局部支持或類比必須列出未支持部分")
    return {
        key: value[key]
        for key in (
            "contract_version",
            "relation",
            "reason",
            "quotes",
            "unsupported",
            "applicability",
        )
    }
