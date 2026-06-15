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

# Common to every kind. Kind-specific quoting rules are added separately —
# crucially, the double-quote rule is FLOWCHART-only: mindmap is indentation-
# based and a quoted node (`"概念"`) is a parse error that kills the diagram.
_MERMAID_RULES_COMMON = (
    "只輸出一個 ```mermaid 區塊,不要任何說明文字。label 裡不要放 LaTeX/數學($...$、\\mathcal 等)。"
)
_MERMAID_RULES_QUOTED = (
    "每個 node label 用雙引號包住（如 `A[\"概念（細節）\"]`）；subgraph 名稱也用雙引號；"
    "不要在 label 裡用未跳脫的特殊字元。"
)
_MERMAID_RULES_MINDMAP = (
    "絕對不要用雙引號包節點文字（mindmap 用了引號會整張圖解析失敗）；"
    "節點文字直接寫純文字,並避免括號 () [] {} 等特殊字元(需要時改用全形或省略)。"
)
# Kinds that use flowchart-style quoted labels.
_QUOTED_KINDS = frozenset({"flowchart", "concept_map"})

# Pin the artifact's language to the CONTENT's language. The visual router goes
# through llm.complete() (system prompt verbatim, no `## Output Language`), so
# without this a Chinese note's diagram/table could drift to English. We match
# the content rather than forcing OUTPUT_LANGUAGE — a genuinely English note
# should get an English diagram.
_LANG_MATCH_RULE = (
    "節點／儲存格文字一律使用與內容相同的語言（內容是中文就用中文,不要改用英文）；"
    "人名、技術術語、程式碼識別符、英文書名等專有名詞可保留原文。"
)

# Per-kind syntax guidance (the generic rules above aren't enough for mindmap).
_MERMAID_HINTS = {
    "mindmap": "mindmap 語法：第一行 `mindmap`,再用**縮排**表示階層,根節點寫 `root((主題))`（不加引號）,"
               "子節點每行一個、用縮排表示層級（如 `    分支A`、`      子項`）。不要用 `-->` 箭頭。",
    "timeline": "timeline 語法：第一行 `timeline`,接 `title 標題`,然後每段寫 `時期 : 事件 : 事件`。",
    "quadrant": "quadrantChart 語法：第一行 `quadrantChart`,設 `x-axis`、`y-axis`、四個 `quadrant-1..4`,"
                "再以 `\"點名\": [x, y]`（0~1）放點。",
    "concept_map": "用 `graph LR`,節點間用帶標籤的邊表達關係,如 `A[\"概念\"] -->|\"關係\"| B[\"概念\"]`。",
    "flowchart": "用 `flowchart TD`,箭頭 `-->` 表流程/因果,需要分組時用 `subgraph \"群組\" ... end`。",
}

_CLASSIFY_SYSTEM = (
    "你是學習產物分類器。讀使用者提供的內容,判斷哪些學習輔助產物最能幫助讀者理解或記住,"
    "並依適合度排序(最適合在前),最多回兩種。\n"
    "選項（type 只能用下列之一）：\n"
    + "\n".join(f"- {k}：{v}" for k, v in ARTIFACT_TYPES.items())
    + "\n\n回 JSON：{\"ranked\": [{\"type\": \"<上列之一>\", \"confidence\": <0-1>, "
    "\"reason\": \"<一句話為什麼>\"}, ...]}（依適合度排序,1~2 項）\n"
    "重要：內容若沒有清楚的結構,ranked 只放一項 type=\"none\"——寧可不產圖,也不要硬湊誤導的圖。"
    "兩種產物要呈現不同的認知切面(例如流程 vs 階層),不要選兩個本質相同的。"
)

_MERMAID_BLOCK_RE = re.compile(r"```mermaid.*?```", re.DOTALL)


_NONE_RESULT = {"type": "none", "confidence": 0.0, "reason": "classification failed or unstructured"}


def _coerce_choice(raw) -> dict | None:
    """Validate one {type, confidence, reason} dict → normalized, or None."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t not in ARTIFACT_TYPES:
        return None
    return {
        "type": t,
        "confidence": float(raw.get("confidence") or 0.0),
        "reason": str(raw.get("reason") or "").strip(),
    }


def classify_structures(llm, content: str, *, limit: int = 2) -> list[dict]:
    """Return a ranked list (1..limit) of {type, confidence, reason}.

    Accepts both the ranked shape `{"ranked": [...]}` and the legacy single
    `{"type": ...}` (kept so callers/tests passing a single dict still work).
    Drops `none` once any real structure is present, dedups by type, and
    fail-opens to `[none]`."""
    parsed = llm._complete_json(
        kind="object",
        system_prompt=_CLASSIFY_SYSTEM,
        user_msg=content[:6000],
        temperature=0.0,
        trace_context={"stage": "artifact_classify", "metadata": {}},
    ) if hasattr(llm, "_complete_json") else {}

    if isinstance(parsed, dict) and isinstance(parsed.get("ranked"), list):
        raw_choices = parsed["ranked"]
    elif isinstance(parsed, dict) and "type" in parsed:   # legacy single-pick
        raw_choices = [parsed]
    else:
        raw_choices = []

    ranked: list[dict] = []
    seen: set[str] = set()
    for raw in raw_choices:
        choice = _coerce_choice(raw)
        if not choice or choice["type"] in seen:
            continue
        seen.add(choice["type"])
        ranked.append(choice)

    # Drop 'none' as soon as a real structure exists; cap to limit.
    real = [c for c in ranked if c["type"] != "none"]
    if real:
        return real[:limit]
    return [dict(_NONE_RESULT)]


def classify_structure(llm, content: str) -> dict:
    """Top single pick — {type, confidence, reason}. Fail-open to 'none'."""
    return classify_structures(llm, content, limit=1)[0]


def _render_table(llm, content: str) -> str:
    sys = ("把內容整理成**一個 Markdown 比較表格**,欄是比較維度、列是被比較的對象。"
           "只輸出表格本身,不要前後說明。維度要能凸顯差異。" + _LANG_MATCH_RULE)
    return llm.complete(sys, content[:6000], temperature=0.2, stage="artifact_table").strip()


def _render_mermaid(llm, content: str, kind: str) -> str:
    header = _MERMAID_KIND.get(kind, "flowchart TD")
    hint = _MERMAID_HINTS.get(kind, "")
    if kind == "mindmap":
        rules = f"{_MERMAID_RULES_MINDMAP} {_MERMAID_RULES_COMMON}"
    elif kind in _QUOTED_KINDS:
        rules = f"{_MERMAID_RULES_QUOTED} {_MERMAID_RULES_COMMON}"
    else:
        rules = _MERMAID_RULES_COMMON
    sys = (f"把內容畫成一個 Mermaid **{kind}**（以 `{header}` 開頭）。{hint} {rules} {_LANG_MATCH_RULE}")
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


def _render_for_type(llm, content: str, t: str) -> str:
    """Render one artifact of type `t`. "" on failure / 'none'. Never raises."""
    try:
        if t == "comparison_table":
            return _render_table(llm, content)
        if t in _MERMAID_KIND:
            return _render_mermaid(llm, content, t)
        if t == "argument_map":
            from core.config import settings
            from services.argument_map import build_argument_map, render_argument_map
            return render_argument_map(
                build_argument_map(llm, content), with_mermaid=settings.ARGUMENT_MAP_MERMAID
            )
    except Exception as e:
        logging.warning(f"learning_artifacts: render failed for {t}: {e}")
    return ""


def build_artifact(llm, content: str, *, forced_type: str | None = None) -> dict:
    """Classify (single top pick or honor forced_type) → render → validate.

    Returns {type, reason, artifact}. artifact is "" when type is 'none' or a
    renderer failed validation (caller shows a graceful note rather than a
    broken diagram). Single-artifact path — used by the on-demand
    @ling-visualize command. Auto-attach uses `build_artifacts` for the top-2.
    """
    if not content or not content.strip():
        return {"type": "none", "reason": "empty content", "artifact": ""}

    if forced_type and forced_type in ARTIFACT_TYPES:
        chosen = {"type": forced_type, "confidence": 1.0, "reason": "使用者指定"}
    else:
        chosen = classify_structure(llm, content)

    t = chosen["type"]
    artifact = _render_for_type(llm, content, t)
    return {"type": t, "reason": chosen.get("reason", ""), "artifact": artifact}


def build_artifacts(llm, content: str, *, limit: int = 2) -> list[dict]:
    """Classify into a ranked top-`limit` → render each → keep the ones that
    produced a real artifact. Returns a list of {type, reason, artifact} (may
    be empty). Used by the auto-attach path to surface complementary views."""
    if not content or not content.strip():
        return []
    results: list[dict] = []
    for chosen in classify_structures(llm, content, limit=limit):
        t = chosen["type"]
        if t == "none":
            continue
        artifact = _render_for_type(llm, content, t)
        if artifact:
            results.append({"type": t, "reason": chosen.get("reason", ""), "artifact": artifact})
    return results


def maybe_artifact_section(llm, content: str) -> str:
    """One or more '## 🖼️ 學習輔助（type）' sections for `content` (the top-2
    complementary views), or '' when disabled / unstructured / all renders fail.
    Gated by Scripture's `visual_router` — this is the AUTO-attach to
    synthesis/part/insight output (the on-demand @ling-visualize is never gated).
    Read live from `settings` so flipping it in Scripture.md takes effect without
    a daemon restart. Returns '' (and makes zero LLM calls) when off, so callers
    stay byte-identical by default. Fail-open."""
    from core.config import settings
    if not settings.VISUAL_ROUTER_ENABLED:
        return ""
    try:
        results = build_artifacts(llm, content)
    except Exception as e:
        logging.warning(f"learning_artifacts: auto-attach failed: {e}")
        return ""
    return "".join(
        f"## 🖼️ 學習輔助（{r['type']}）\n\n{r['artifact']}\n\n" for r in results
    )
