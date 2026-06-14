"""Learning Artifacts — Phase 6 visual router.

Picks the RIGHT learning aid for content's cognitive structure instead of
always emitting a Mermaid flowchart: classify → render → validate. Robustness
lessons baked in — classification via `_complete_json` (no answer_query
template scaffolding), Markdown tables and simple Mermaid over freehand SVG,
generated Mermaid run through the markdown quality checker, and `none` as a
first-class output (don't force a diagram onto prose that has no structure).
"""

from __future__ import annotations

import logging
import re

from core.parser import run_markdown_quality_checks

# type → (human description for the classifier, renderer kind).
ARTIFACT_TYPES = {
    "comparison_table": "內容在比較 ≥2 個對象的多個維度",
    "flowchart": "流程、因果序列或步驟",
    "mindmap": "一個主題的階層分解",
    "timeline": "時序、階段或歷史演進",
    "quadrant": "物件落在兩個軸 / 取捨空間",
    "concept_map": "概念之間的網狀關係（非序列）",
    "argument_map": "論證性內容（主張 + 根據 + 反駁）",
    "none": "沒有強視覺結構的散文",
}

# Mermaid header per diagram kind.
_MERMAID_KIND = {
    "flowchart": "flowchart TD",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "quadrant": "quadrantChart",
    "concept_map": "graph LR",
}

_MERMAID_RULES = (
    "Mermaid 規則：每個 node label 用雙引號包住（如 `A[\"概念（細節）\"]`）；"
    "subgraph 名稱也用雙引號；不要在 label 裡用未跳脫的特殊字元。只輸出一個 ```mermaid 區塊,不要任何說明文字。"
)

# Per-kind syntax guidance (the generic rules above aren't enough for mindmap).
_MERMAID_HINTS = {
    "mindmap": "mindmap 語法：第一行 `mindmap`,再用**縮排**表示階層,根節點寫 `root((\"主題\"))`,"
               "子節點每行一個、用縮排表示層級（如 `    分支A`、`      子項`）。不要用 `-->` 箭頭。",
    "timeline": "timeline 語法：第一行 `timeline`,接 `title 標題`,然後每段寫 `時期 : 事件 : 事件`。",
    "quadrant": "quadrantChart 語法：第一行 `quadrantChart`,設 `x-axis`、`y-axis`、四個 `quadrant-1..4`,"
                "再以 `\"點名\": [x, y]`（0~1）放點。",
    "concept_map": "用 `graph LR`,節點間用帶標籤的邊表達關係,如 `A[\"概念\"] -->|\"關係\"| B[\"概念\"]`。",
    "flowchart": "用 `flowchart TD`,箭頭 `-->` 表流程/因果,需要分組時用 `subgraph \"群組\" ... end`。",
}

_CLASSIFY_SYSTEM = (
    "你是學習產物分類器。讀使用者提供的內容,判斷它最適合哪一種學習輔助產物,幫助讀者更快理解或記住。\n"
    "選項（只能回其中一個 type）：\n"
    + "\n".join(f"- {k}：{v}" for k, v in ARTIFACT_TYPES.items())
    + "\n\n回 JSON：{\"type\": \"<上列之一>\", \"confidence\": <0-1>, \"reason\": \"<一句話為什麼>\"}\n"
    "重要：內容若沒有清楚的結構,回 type=\"none\"——寧可不產圖,也不要硬湊一張誤導的圖。"
)

_MERMAID_BLOCK_RE = re.compile(r"```mermaid.*?```", re.DOTALL)


def classify_structure(llm, content: str) -> dict:
    """Return {type, confidence, reason}. Fail-open to 'none'."""
    parsed = llm._complete_json(
        kind="object",
        system_prompt=_CLASSIFY_SYSTEM,
        user_msg=content[:6000],
        temperature=0.0,
        trace_context={"stage": "artifact_classify", "metadata": {}},
    ) if hasattr(llm, "_complete_json") else {}
    t = parsed.get("type") if isinstance(parsed, dict) else None
    if t not in ARTIFACT_TYPES:
        return {"type": "none", "confidence": 0.0, "reason": "classification failed or unstructured"}
    return {
        "type": t,
        "confidence": float(parsed.get("confidence") or 0.0),
        "reason": str(parsed.get("reason") or "").strip(),
    }


def _render_table(llm, content: str) -> str:
    sys = ("把內容整理成**一個 Markdown 比較表格**,欄是比較維度、列是被比較的對象。"
           "只輸出表格本身,不要前後說明。維度要能凸顯差異。")
    return llm.complete(sys, content[:6000], temperature=0.2, stage="artifact_table").strip()


def _render_mermaid(llm, content: str, kind: str) -> str:
    header = _MERMAID_KIND.get(kind, "flowchart TD")
    hint = _MERMAID_HINTS.get(kind, "")
    sys = (f"把內容畫成一個 Mermaid **{kind}**（以 `{header}` 開頭）。{hint} {_MERMAID_RULES}")
    raw = llm.complete(sys, content[:6000], temperature=0.2, stage=f"artifact_{kind}")
    # Repair common issues (fences, label quotes, arrows) via the existing
    # quality checker, then validate the diagram is actually the requested kind.
    cleaned, _ = run_markdown_quality_checks(raw or "")
    m = _MERMAID_BLOCK_RE.search(cleaned)
    if not m or not _validate_mermaid(m.group(0), kind):
        return ""
    return m.group(0)


def _validate_mermaid(block: str, kind: str) -> bool:
    """Per-kind sanity: the diagram declares the requested type and has content.
    Catches 'asked for mindmap, got flowchart' and empty/garbage blocks. Not a
    full Mermaid parser — header keyword + non-trivial body."""
    inner = block.strip()
    inner = inner[inner.find("\n") + 1:] if "\n" in inner else ""   # drop ```mermaid fence line
    inner = inner.rsplit("```", 1)[0].strip()                       # drop closing fence
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    if len(lines) < 2:                                              # header + ≥1 content line
        return False
    expected = _MERMAID_KIND[kind].split()[0].lower()              # flowchart/mindmap/timeline/quadrantchart/graph
    return lines[0].strip().lower().startswith(expected)


def build_artifact(llm, content: str, *, forced_type: str | None = None) -> dict:
    """Classify (or honor forced_type) → render → validate.

    Returns {type, reason, artifact}. artifact is "" when type is 'none' or a
    renderer failed validation (caller shows a graceful note rather than a
    broken diagram).
    """
    if not content or not content.strip():
        return {"type": "none", "reason": "empty content", "artifact": ""}

    if forced_type and forced_type in ARTIFACT_TYPES:
        chosen = {"type": forced_type, "confidence": 1.0, "reason": "使用者指定"}
    else:
        chosen = classify_structure(llm, content)

    t = chosen["type"]
    artifact = ""
    try:
        if t == "comparison_table":
            artifact = _render_table(llm, content)
        elif t in _MERMAID_KIND:
            artifact = _render_mermaid(llm, content, t)
        elif t == "argument_map":
            from core.config import ARGUMENT_MAP_MERMAID
            from services.argument_map import build_argument_map, render_argument_map
            artifact = render_argument_map(
                build_argument_map(llm, content), with_mermaid=ARGUMENT_MAP_MERMAID
            )
        # t == "none" → no artifact
    except Exception as e:
        logging.warning(f"learning_artifacts: render failed for {t}: {e}")
        artifact = ""

    return {"type": t, "reason": chosen.get("reason", ""), "artifact": artifact}


def maybe_artifact_section(llm, content: str) -> str:
    """A '## 🖼️ 學習輔助' section for `content`, or '' when disabled / 'none' /
    render failed. Gated by VISUAL_ROUTER_ENABLED — this is the AUTO-attach to
    synthesis/insight output (the on-demand @ling-visualize is never gated).
    Returns '' (and makes zero LLM calls) when the flag is off, so callers stay
    byte-identical by default. Fail-open."""
    from core.config import VISUAL_ROUTER_ENABLED
    if not VISUAL_ROUTER_ENABLED:
        return ""
    try:
        result = build_artifact(llm, content)
    except Exception as e:
        logging.warning(f"learning_artifacts: auto-attach failed: {e}")
        return ""
    if not result.get("artifact"):
        return ""
    return f"## 🖼️ 學習輔助（{result['type']}）\n\n{result['artifact']}\n\n"
