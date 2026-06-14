"""Argument map — Phase 6 axis (3): the logical skeleton, for critical thinking.

Lays out content as a Toulmin structure: claim → grounds → WARRANT → qualifier
→ rebuttal. The value-add over the system's existing claim/evidence/falsifier
extraction is the **warrant** — the unstated premise that links the grounds to
the claim. Surfacing it is exactly "find the hidden logic": it shows the reader
the assumption an argument rests on but never says out loud, and flags the
weakest such link.

Structured-Markdown output (robust, readable). Extraction via `_complete_json`
(no answer_query scaffolding). Fail-open.
"""

from __future__ import annotations

import logging
import re

_SYSTEM = (
    "你是論證分析器,服務批判性思考。抽出內容的論證骨架（Toulmin 模型）。\n"
    "最重要的任務:找出**未明說的隱含前提（warrant）**——連結「根據」到「主張」、"
    "但作者沒講出來、讀者得自己補上的假設。這是隱藏的邏輯。\n\n"
    "回 JSON：\n"
    "{\n"
    '  "claim": "<中心主張,一句>",\n'
    '  "grounds": ["<明說的根據/證據>"],\n'
    '  "warrants": ["<未明說的隱含前提——連結根據到主張的假設>"],\n'
    '  "qualifier": "<成立的條件/範圍,沒有則空字串>",\n'
    '  "rebuttals": ["<反駁、反例、或會推翻它的情況>"],\n'
    '  "weakest_link": "<哪一個 warrant 最未明說且最可爭議,一句>"\n'
    "}\n\n"
    "若內容沒有清楚的論證（不是在主張什麼）,回 claim 空字串。不要逐字抄,用精煉的話。"
)


def build_argument_map(llm, content: str) -> dict:
    """Extract the Toulmin skeleton. Fail-open: empty dict on failure."""
    if not content or not content.strip() or not hasattr(llm, "_complete_json"):
        return {}
    try:
        parsed = llm._complete_json(
            kind="object",
            system_prompt=_SYSTEM,
            user_msg=content[:6000],
            temperature=0.1,
            trace_context={"stage": "argument_map", "metadata": {}},
        )
    except Exception as e:
        logging.warning(f"argument_map: extraction failed: {e}")
        return {}
    if not isinstance(parsed, dict) or not str(parsed.get("claim") or "").strip():
        return {}

    def _list(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    return {
        "claim": str(parsed.get("claim")).strip(),
        "grounds": _list(parsed.get("grounds")),
        "warrants": _list(parsed.get("warrants")),
        "qualifier": str(parsed.get("qualifier") or "").strip(),
        "rebuttals": _list(parsed.get("rebuttals")),
        "weakest_link": str(parsed.get("weakest_link") or "").strip(),
    }


def _mm(s: str, cap: int = 80) -> str:
    """Sanitize a label for a Mermaid double-quoted node."""
    return re.sub(r"\s+", " ", str(s)).strip().replace('"', "'")[:cap]


def _argument_mermaid(data: dict) -> str:
    """Deterministic Mermaid graph from the Toulmin fields — no LLM, so it can't
    drift or hallucinate. Grounds → claim (solid); warrants/rebuttals → claim
    (dashed, labelled). Optional companion to the Markdown layout."""
    lines = ["```mermaid", "graph TD", f'  C["主張：{_mm(data["claim"])}"]']
    for i, g in enumerate(data.get("grounds", [])):
        lines.append(f'  G{i}["根據：{_mm(g)}"] --> C')
    for i, w in enumerate(data.get("warrants", [])):
        lines.append(f'  W{i}["隱含前提：{_mm(w)}"] -. 未明說 .-> C')
    for i, r in enumerate(data.get("rebuttals", [])):
        lines.append(f'  R{i}["反駁：{_mm(r)}"] -. 挑戰 .-> C')
    lines.append("```")
    return "\n".join(lines)


def render_argument_map(data: dict, with_mermaid: bool = False) -> str:
    """Structured-Markdown Toulmin layout. Returns "" if there's no claim.
    When `with_mermaid`, appends a deterministic Mermaid graph of the same
    structure (gated by Scripture's `argument_map_mermaid` at the call site)."""
    if not data or not data.get("claim"):
        return ""
    L = ["## 🧩 論證結構（Toulmin）", "", f"**主張**：{data['claim']}", ""]
    if data.get("grounds"):
        L.append("**根據（明說的證據）**：")
        L += [f"- {g}" for g in data["grounds"]]
        L.append("")
    if data.get("warrants"):
        L.append("**隱含前提（未明說——這是論證真正依賴、卻沒講出來的假設）**：")
        L += [f"- {w}" for w in data["warrants"]]
        L.append("")
    if data.get("qualifier"):
        L += [f"**適用條件**：{data['qualifier']}", ""]
    if data.get("rebuttals"):
        L.append("**反駁／反例（能動搖它的情況）**：")
        L += [f"- {r}" for r in data["rebuttals"]]
        L.append("")
    if data.get("weakest_link"):
        L += [f"> 💦 **最弱的一環**：{data['weakest_link']}"]
    if with_mermaid:
        L += ["", _argument_mermaid(data)]
    return "\n".join(L).rstrip()
