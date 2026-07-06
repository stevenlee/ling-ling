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
    "concept_map": "概念之間鬆散、未型別化的網狀關係（只講得出「相關」，講不出是哪一種關係）",
    "ontology": "領域知識的本體論：關係可被型別化——類別階層 (is-a)、組成 (part-of)、物件屬性與個體 (instance-of) 等具名語意關係",
    "argument_map": "論證性內容（主張 + 根據 + 反駁）",
    "sequence_diagram": "實體之間的對話、交涉、訊息傳遞或劇情發展順序",
    "state_diagram": "事物或心理狀態的轉變與觸發條件",
    "user_journey": "主角或使用者在不同階段的心境起伏與體驗分數",
    "gantt_chart": "具有持續時間的歷史事件或專案排程重疊關係",
    "pie_chart": "整體中的比例、成分分配或資源佔比",
    "sankey_diagram": "資金流向、資源分配、能量轉換等「流量」關係",
    "xy_chart": "數值在時間序列或類別上的分佈與趨勢",
    "c4_diagram": "軟體系統的 C4 架構 (Context / Container)",
    "class_diagram": "軟體／程式碼的物件導向類別、屬性與繼承（針對程式設計，非知識領域）",
    "er_diagram": "資料庫的實體關聯表（如：一對多、主外鍵）",
    "none": "沒有強視覺結構的散文",
}

# Mermaid header per diagram kind.
_MERMAID_KIND = {
    "flowchart": "flowchart TD 或 flowchart LR",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "quadrant": "quadrantChart",
    "concept_map": "graph TD 或 graph LR",
    "sequence_diagram": "sequenceDiagram",
    "state_diagram": "stateDiagram-v2",
    "user_journey": "journey",
    "gantt_chart": "gantt",
    "pie_chart": "pie title",
    "sankey_diagram": "sankey-beta",
    "xy_chart": "xychart-beta",
    "c4_diagram": "C4Context",
    "class_diagram": "classDiagram",
    "er_diagram": "erDiagram",
    "ontology": "classDiagram",
}

# Common to every kind. Kind-specific quoting rules are added separately —
# crucially, the double-quote rule is FLOWCHART-only: mindmap is indentation-
# based and a quoted node (`"概念"`) is a parse error that kills the diagram.
# NOTE: the math rules below follow `math-policy: katex-v2` — the same policy is
# stated LLM-facing in lings-desktop/Templates/Prompts/mermaid_rules.md and
# enforced by core/parsing/mermaid_repair.py; change all three together
# (tests/test_prompt_assets.py guards the vault file's marker).
_MERMAID_RULES_COMMON = (
    "只輸出一個 ```mermaid 區塊,不要任何說明文字。"
    'label 裡若要放數學式,務必用 `$$…$$` 雙錢號包起來（mermaid 的 KaTeX 語法,例 `N["增長率 $$\\alpha$$"]`）；'
    "不要用單一 `$`,也不要把非數學的文字（如普通變數名、省略號）塞進 `$$`,那些直接寫純文字即可。"
    "【重要】全面使用純英文 ID：所有節點或變數名稱必須使用純英文(例如 Goal, Baseline)，絕對不可使用中文，以防編碼解析失敗。"
)
_MERMAID_RULES_QUOTED = (
    '每個 node label 用雙引號包住（如 `Baseline["基準模型"]`）；subgraph 名稱也用雙引號；不要在 label 裡用未跳脫的特殊字元；'
    "集中管理連線邏輯：先在上方宣告所有節點（含 subgraph 的內部節點），最後在最下方統一撰寫所有節點間的連線關係 (`-->`)，將定義與連線分開。"
    "【ID 一致性】節點 ID 只用純英文短代號（如 `ED1`），ID 本身絕不加引號（中文只放在引號 label 裡）；"
    "同一個節點在「宣告、連線、style」三處必須用『完全相同』的 ID，否則 Mermaid 會把它們當成不同節點而產生重複/懸空節點，且 style 會綁定失敗。"
)
_MERMAID_RULES_MINDMAP = (
    "絕對不要用雙引號包節點文字（mindmap 用了引號會整張圖解析失敗）；"
    "節點文字直接寫純文字,並避免括號 () [] {} 等特殊字元(需要時改用全形或省略)；"
    "mindmap 不支援數學渲染,不要使用 `$$…$$` 或 LaTeX,數學式改寫成純文字（如 1/2、x^2）。"
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
    '再以 `"點名": [x, y]`（0~1）放點。',
    "concept_map": '節點間用帶標籤的邊表達關係,如 `A["概念"] -->|"關係"| B["概念"]`。請根據內容結構決定方向：並行分支多或文字長時強烈建議使用 LR 以收斂左右寬度。',
    "flowchart": '箭頭 `-->` 表流程/因果,需要分組時用 `subgraph "群組" ... end`。請根據內容結構決定方向：並行分支多或文字長時強烈建議使用 LR 以收斂左右寬度。',
    "sequence_diagram": "sequenceDiagram 語法：第一行 `sequenceDiagram`,參與者用 `participant A`，訊息用 `A->>B: 訊息`，可搭配 `Note over A,B: 說明`。",
    "state_diagram": 'stateDiagram-v2 語法：第一行 `stateDiagram-v2` (若需要左右排版可加 `direction LR`)。起點與終點用 `[*]`。轉移如 `A --> B : 條件`。【重要優化】1. 引入狀態 ID：為所有節點定義純英文 ID (如 `state "中文" as CHMM_Model`)，確保跨層級連接穩定。2. 符號視覺優化：將文字敘述的 gamma, xi, theta, delta 等改為實際符號 γ, ξ, θ, Δ，將 10^-4 寫作 10⁻⁴。3. 結構封裝：明確定義複合狀態內的起始與結束關聯 `[*]`，避免游離節點。',
    "user_journey": "journey 語法：第一行 `journey`,標題 `title ...`,區段 `section ...`,步驟寫法為 `任務名稱: 分數: 角色`,例如 `逛商品: 5: User`（分數1~5）。",
    "gantt_chart": "gantt 語法：第一行 `gantt`,接 `title ...` 與 `dateFormat YYYY-MM-DD`，區段用 `section 名稱`，任務為 `任務名 :狀態, ID, 開始日, 結束日`。",
    "pie_chart": 'pie 語法：第一行 `pie title 標題`,每行寫 `"類別" : 數值`（數值必須是純數字）。',
    "sankey_diagram": "sankey-beta 語法：第一行 `sankey-beta`,每行寫 `來源節點, 目標節點, 數值`，如 `A, B, 10`。不要用雙引號包住節點名稱。",
    "xy_chart": 'xychart-beta 語法：第一行 `xychart-beta`，接著 `title "標題"`，定義 `x-axis [...]` 和 `y-axis "標籤" 區間`，最後加 `bar [...]` 或 `line [...]`。',
    "c4_diagram": 'C4Context 語法：使用 `Person(alias, "Label")`、`System(alias, "Label")` 與 `Rel(from, to, "Label")` 等巨集指令。',
    "class_diagram": "classDiagram 語法：第一行 `classDiagram`，使用 `class 類別名 { ... }` 或是 `類別A <|-- 類別B` 來表示繼承。",
    "er_diagram": "erDiagram 語法：第一行 `erDiagram`，實體關聯用 `實體1 ||--o{ 實體2 : 關係標籤` 等語法。",
    "ontology": (
        "ontology（本體論）語法：用 classDiagram 的符號表達領域本體，第一行寫 `classDiagram`。"
        '節點 ID 一律純英文，概念名稱放在標籤裡，如 `class Animal["動物"]`。'
        '【宣告規則】每個類別只在最上方用 `class 類別名["標籤"]` 宣告一次（不可重複宣告同一類別）；'
        '關係行只准放純類別 ID，絕對不要在關係行裡寫 `["標籤"]`（如 `A *-- B["乙"]` 是錯的，B 的標籤要寫在它自己的 `class B["乙"]` 宣告）。'
        "請務必依語意選用「不同」的箭頭區分四種關係，切勿全部用同一種線："
        "① is-a／子類別 (subClassOf)：用空心三角繼承箭頭 `Superclass <|-- Subclass`（例：`Animal <|-- Dog`），這是唯一用來表達分類階層的符號；"
        "② part-of／組成：用組合菱形 `Whole *-- Part : part-of`；"
        '③ 物件屬性／一般語意關係 (object property)：用帶標籤的實線關聯 `ClassA --> ClassB : 關係名稱`，必要時加基數如 `ClassA "1" --> "*" ClassB : owns`；'
        '④ 個體／實例 (instance-of)：個體必須先在最上方跟其他類別一樣宣告 `class Fido["標籤"]`，'
        "然後才能用**獨立一行**的 stereotype 標記 `<<instance>> Fido`（stereotype 在前、類別名稱在後，"
        "各自一行；不要用 `Fido { <<instance>> }` 這種大括號內嵌寫法）。"
        "順序不可顛倒：對「尚未宣告」的名稱寫 `<<instance>>` 會讓整張圖無法渲染。"
        "stereotype 一律用英文，mermaid 的 `<<>>` 不支援中文，"
        "寫成 `<<個體>>` 會導致語法錯誤。最後用虛線依賴 `Fido ..> Dog : instance-of` 連到其類別。"
        "大括號 `{}` 只在放資料屬性時才加；stereotype 用上面的獨立行寫法，不要留空的或殘缺的 `{}`。"
        "資料屬性 (data property) 寫成獨立一行、直接以類別名開頭，如 `Animal : +name string`；"
        "這行**開頭不要加 `class`**（`class` 只用於最上方的宣告行，成員／屬性行絕不加）。"
        "目標是讓階層、組成、屬性、個體在圖上一眼可辨，而非畫成扁平的概念網。"
    ),
}

_ONTOLOGY_PRIORITY = (
    "【關係圖優先序】concept_map、class_diagram、ontology 同屬「關係圖」這一種認知切面,至多擇一,"
    "請依內容本質挑最貼切的一種:關係鬆散、講不出明確類型時用 concept_map;"
    "內容明確是程式碼/軟體的類別設計時用 class_diagram;"
    "關係能被型別化(is-a/part-of/instance-of)、且這種階層化的本體結構正是內容重點時用 ontology。"
    "**僅當 concept_map 與 ontology 兩者適合度難分軒輊時,才傾向選 ontology。**"
)


def _build_classify_system(limit: int, exclude_types: set[str] | None = None) -> str:
    allowed_types = {
        k: v for k, v in ARTIFACT_TYPES.items() if not exclude_types or k not in exclude_types
    }
    try:
        from core.config import settings

        ontology_bias = (
            bool(getattr(settings, "ONTOLOGY_BIAS", False)) and "ontology" in allowed_types
        )
    except Exception:
        ontology_bias = False
    priority_clause = _ONTOLOGY_PRIORITY if ontology_bias else ""
    return (
        "你是學習產物分類器。讀使用者提供的內容,判斷哪些學習輔助產物最能幫助讀者理解或記住,"
        f"並依適合度排序(最適合在前),最多回{limit}種。\n"
        "選項（type 只能用下列之一）：\n"
        + "\n".join(f"- {k}：{v}" for k, v in allowed_types.items())
        + '\n\n回 JSON：{"ranked": [{"type": "<上列之一>", "confidence": <0-1>, '
        '"reason": "<一句話為什麼>"}, ...]}（依適合度排序,1~' + str(limit) + " 項）\n"
        '重要：內容若沒有清楚的結構,ranked 只放一項 type="none"——寧可不產圖,也不要硬湊誤導的圖。'
        "多種產物要呈現不同的認知切面(例如流程 vs 階層),不要選本質相同的。" + priority_clause
    )


_MERMAID_BLOCK_RE = re.compile(r"```mermaid.*?```", re.DOTALL)

# The model sometimes leaks its own reasoning into the diagram body
# (`ModelFree^... (Wait, I'll just write the final code block)`). Such garbage
# can't be repaired — the diagram is rejected so the artifact is dropped and
# regenerated on the next pass. These phrases are extremely high-signal LLM
# meta-text, near-impossible in a real diagram label, so false-drops are minimal.
_MERMAID_METATEXT_RE = re.compile(
    r"(?i)"
    r"wait,\s*i|"  # "Wait, I'll ..."
    r"i'?ll\s+just|"  # "I'll just write ..."
    r"final\s+code\s+block|"
    r"as\s+an\s+ai\b|"
    r"i\s+apologi[sz]e|"
    r"i\s+can(?:no|')?t\b|"  # I cannot / I can't
    r"let\s+me\s+(?:just|write|know)|"
    r"here'?s\s+the\s+(?:final|code|diagram)|"
    r"as\s+requested"
)


def mermaid_has_metatext(block: str) -> bool:
    """True if the mermaid block contains leaked LLM meta-text/reasoning."""
    return bool(_MERMAID_METATEXT_RE.search(block))


_NONE_RESULT = {
    "type": "none",
    "confidence": 0.0,
    "reason": "classification failed or unstructured",
}


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


def classify_structures(
    llm, content: str, *, limit: int = 2, exclude_types: set[str] | None = None
) -> list[dict]:
    """Return a ranked list (1..limit) of {type, confidence, reason}.

    Accepts both the ranked shape `{"ranked": [...]}` and the legacy single
    `{"type": ...}` (kept so callers/tests passing a single dict still work).
    Drops `none` once any real structure is present, dedups by type, and
    fail-opens to `[none]`."""
    parsed = (
        llm._complete_json(
            kind="object",
            system_prompt=_build_classify_system(limit, exclude_types),
            user_msg=content[:6000],
            temperature=0.0,
            trace_context={"stage": "artifact_classify", "metadata": {}},
        )
        if hasattr(llm, "_complete_json")
        else {}
    )

    if isinstance(parsed, dict) and isinstance(parsed.get("ranked"), list):
        raw_choices = parsed["ranked"]
    elif isinstance(parsed, dict) and "type" in parsed:  # legacy single-pick
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
    sys = (
        "把內容整理成**一個 Markdown 比較表格**,欄是比較維度、列是被比較的對象。"
        "只輸出表格本身,不要前後說明。維度要能凸顯差異。" + _LANG_MATCH_RULE
    )
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
    sys = f"把內容畫成一個 Mermaid **{kind}**（以 `{header}` 開頭）。{hint} {rules} {_LANG_MATCH_RULE}"
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
    # Leaked LLM reasoning (`(Wait, I'll just write the final code block)`) is
    # unrepairable garbage — reject so the artifact is dropped and regenerated.
    if mermaid_has_metatext(block):
        return False
    inner = block.strip()
    inner = inner[inner.find("\n") + 1 :] if "\n" in inner else ""  # drop ```mermaid fence line
    inner = inner.rsplit("```", 1)[0].strip()  # drop closing fence
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    if len(lines) < 2:  # header + ≥1 content line
        return False
    expected = (
        _MERMAID_KIND[kind].split()[0].lower()
    )  # flowchart/mindmap/timeline/quadrantchart/graph
    if not lines[0].strip().lower().startswith(expected):
        return False
    # An ontology with no relationship edges is just a disconnected node dump
    # (degenerate — the whole point is the typed relations). Require ≥1 edge.
    if kind == "ontology" and not any(
        tok in inner
        for tok in ("<|--", "*--", "o--", "-->", "..>", "..|>", "<..", "--|>", "--*", "--o")
    ):
        return False
    return True


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


def build_artifacts(
    llm, content: str, *, limit: int = 2, exclude_types: set[str] | None = None
) -> list[dict]:
    """Classify into a ranked top-`limit` → render each → keep the ones that
    produced a real artifact. Returns a list of {type, reason, artifact} (may
    be empty). Used by the auto-attach path to surface complementary views."""
    if not content or not content.strip():
        return []
    results: list[dict] = []
    for chosen in classify_structures(llm, content, limit=limit, exclude_types=exclude_types):
        t = chosen["type"]
        if t == "none":
            continue
        artifact = _render_for_type(llm, content, t)
        if artifact:
            results.append({"type": t, "reason": chosen.get("reason", ""), "artifact": artifact})
    return results


def maybe_artifact_section(
    llm, content: str, *, limit: int = 2, exclude_types: set[str] | None = None
) -> str:
    """One or more '## 🖼️ 學習輔助（type）' sections for `content` (the top-`limit`
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
        results = build_artifacts(llm, content, limit=limit, exclude_types=exclude_types)
    except Exception as e:
        logging.warning(f"learning_artifacts: auto-attach failed: {e}")
        return ""
    return "".join(
        f"## 🖼️ 學習輔助（{r['type']}）\n\n{_nest_artifact_headings(r['artifact'])}\n\n"
        for r in results
    )


def _nest_artifact_headings(artifact: str) -> str:
    """Demote an artifact's own leading ``## `` heading to ``### `` so it nests
    under the ``## 🖼️ 學習輔助（type）`` wrapper instead of forming a second,
    sibling H2 (observed live: argument_map's ``## 🧩 論證結構（Toulmin）`` sat
    flush against the wrapper). Only acts when the body actually leads with an
    H2 — mermaid/table artifacts have no heading and pass through untouched.
    Fenced code is respected so a ``##`` inside a block is never shifted."""
    if not artifact.lstrip().startswith("## "):
        return artifact
    lines = artifact.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            lines[i] = "#" + line  # `## X` → `### X`
    return "\n".join(lines)
