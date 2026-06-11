# Cortex Phase 2 — 實作規格（委外交付用）

> 委託範圍：**只有 Phase 2（夜間鞏固：insight → 主張 → Cortex 頁）**。
> 必讀順序：① [Engineering_Conventions.md](Engineering_Conventions.md)
> ② [CortexMemory_implementation_plan.md](CortexMemory_implementation_plan.md)
> 核心不變量 + §2 + §2.1 + §4 + §6 ③ 本文件。
> 工作分支：`cortex/phase-2`（不准 commit 到 main）。Review 修正最多兩輪。

## 0. Round 1 的教訓，本輪硬性條款

1. **Commit 範圍自查**：commit 前跑 `git status`，**只准 add 本規格
   交付清單內的檔案**。上輪把使用者個人檔案掃進交付，本輪再犯直接退。
2. **指涉物不准便宜實作**：規格說「insight 報告」就是讀檔案內容、
   「來源頁」就是頁面內文——本文件對每個指涉物都附了解析程式碼，照用。

## 1. 明確的「不做」清單（碰了直接退回）

- ❌ R 衰減數學、active/fading/dormant 狀態機、遲滯、revival 校準（Phase 3）
- ❌ falsified 擊殺、un-merge 追蹤、矛盾累積管線（Phase 4）
  - Phase 2 對 `contradicts` 裁決只做：雙方 frontmatter 記
    `contradictions` 連結 + confidence −0.2（floor 0.1）。**不擊殺**。
- ❌ 修改既有測試、`_build_system_prompt`、busy lock、backfill pump
- ❌ ε 探索 / 興趣加權抽樣（生成端，另案）

## 2. 交付物

### 2.1 Config（`core/config.py`）

```python
CORTEX_DIR = WIKI_VAULT_DIR / "Cortex"          # 核心不變量 1：頂層目錄
CORTEX_STATE_FILE = DATABASE_DIR / "cortex_state.json"
CORTEX_ADJUDICATION_CACHE = DATABASE_DIR / "cortex_adjudications.json"
CORTEX_CONSOLIDATION_ENABLED   default true
CORTEX_MAX_INSIGHTS_PER_NIGHT  default 10      # claim 抽取配額
CORTEX_MAX_ADJUDICATIONS_PER_NIGHT default 20  # 蘊涵裁決配額
CORTEX_NEIGHBOR_TOP_K          default 3
CORTEX_NEIGHBOR_SIM_THRESHOLD  default 0.80
CORTEX_MAX_VARIANTS            default 5       # Nuances 區容量上限
```

`CORTEX_DIR` 加進 `_MANAGED_DIRECTORIES`。

### 2.2 `services/cortex_store.py` — Cortex 頁的確定性讀寫層

**LLM 永遠不重寫整頁**（不變量，計畫 §2.1）。本模組是唯一的頁面
讀寫入口，全部確定性程式碼：

```python
@dataclass
class CortexPage:
    claim_id: str            # "cortex-" + sha256(初版 claim 文字)[:16]
    path: Path
    claim: str               # Core Claim 一句話（≤200 字）
    status: str              # Phase 2 固定 "active"
    confidence: float        # 初始 0.5
    S: int                   # 初始 1；reconsolidation +1（Phase 3 會換 spacing 規則）
    last_reinforced_at: str  # ISO
    created: str
    updated: str
    evidence: list[dict]     # [{insight, sources: [...], date, summary}]
    contradictions: list[str]   # 對方 claim_id
    related: list[str]          # 對方 claim_id（entails/complementary）
    variants: list[str]         # Nuances 區內容
    counterpoints: list[str]
    schema_version: int      # 1

def parse_cortex_page(path) -> CortexPage          # 解析失敗 → None + warning
def render_cortex_page(page) -> str                # frontmatter + 固定四節
def save_cortex_page(page) -> None                 # 原子寫入
def load_all_pages(cortex_dir) -> list[CortexPage]
```

頁面格式（固定四節，標頭做成模組常數）：

```markdown
---
claim_id: cortex-ab12cd34ef56gh78
status: active
confidence: 0.5
S: 1
last_reinforced_at: "2026-06-11T03:12:00"
created: "2026-06-11T03:12:00"
updated: "2026-06-11T03:12:00"
evidence:
  - insight: "[20260611-031200][Vault][full-insight].md"
    sources: ["Doc A", "Doc B"]
    date: "2026-06-11"
    summary: "一行摘要"
contradictions: []
related: []
schema_version: 1
---

# <claim 前 60 字>

## Core Claim
<一句話主張>

## Evidence
- [[insight 檔名]]（2026-06-11）：一行摘要 — 來源：[[Doc A]]、[[Doc B]]

## Nuances & Variants
- <變體表述>

## Counterpoints
- （尚無）
```

檔名：claim 文字 sanitize 後前 60 字；碰撞時尾附 claim_id 後 6 碼。
**驗收硬門檻：`parse(render(page))` 與原 page 逐欄位相等**
（round-trip 測試，所有欄位含中文與特殊字元）。

### 2.3 `llm_client.py` 兩個新方法

```python
def extract_claims(self, insight_text: str) -> list[dict]:
    """[{\"claim\": \"一句話原子主張\", \"summary\": \"一行摘要\"}]，
    最多 3 條。stage=\"extract_claims\"。回傳 JSON 解析失敗 → []。
    溫度 0.2。claim 必須是可獨立判真偽的陳述句，不是主題標籤。"""

def adjudicate_claims(self, claim_a: str, claim_b: str) -> dict:
    """{\"verdict\": \"equivalent|entails|entailed_by|complementary|contradicts|unrelated\",
    \"rationale\": \"≤200字\"}。stage=\"adjudicate_claims\"。溫度 0。
    解析失敗 / 非法 verdict → {\"verdict\": \"unrelated\", ...}（保守：
    不合併、不連結，留待下次）。equivalent 的判準寫進 prompt：
    『A 與 B 互相蘊涵（雙向）才是 equivalent；A 是 B 的特例 → entails』"""
```

### 2.4 `maintenance/cortex_consolidation.py` — 夜間鞏固 pass

```python
def run_consolidation(llm, rag, *, ...路徑與配額參數可注入...) -> ConsolidationResult
```

流程（每晚一次，dreaming window）：

1. **收集候選 insight**：掃 `Insights/*.md`，frontmatter 滿足
   `signals` 存在、`refute_verdict != "refuted"`、
   `groundedness 為 None 或 ≥ 0.5`，且檔名不在 state ledger 的
   `processed` 清單。讀法（指涉物範例，照用）：

```python
from core.parser import parse_markdown_metadata
meta = parse_markdown_metadata(path.read_text(encoding="utf-8"))
signals = meta.get("signals") or {}
```

2. **抽取主張**：每份 insight 一次 `extract_claims` call（配額
   `CORTEX_MAX_INSIGHTS_PER_NIGHT`）。不論成敗，insight 檔名記入
   `processed`（含 `{"date", "claims": n}`）。
3. **找鄰居**：新 claim 用 `rag.ef([claim])[0]` 算 embedding，與
   （a）所有既有 Cortex 頁的 claim、（b）本晚其他新 claim 比對
   cosine；取 ≥ `SIM_THRESHOLD` 的前 `TOP_K` 個。Cortex 頁的 claim
   embeddings 快取在 state file（`{claim_id: {embedding, ts}}`），
   頁面 updated 變更時重算。
4. **蘊涵裁決**（配額 `MAX_ADJUDICATIONS_PER_NIGHT`，用完本晚剩餘
   claim 直接走「無鄰居」路徑）：
   - **快取**：key = `sha256(min(ha,hb) + max(ha,hb))`，其中
     ha/hb = sha256(claim 文字)。命中不再裁決。**無 TTL**（設計
     定案，見主計畫 §9-1）。快取存 `CORTEX_ADJUDICATION_CACHE`，
     原子寫入；夜間順手清掉引用已不存在 claim 的條目。
5. **依裁決行動**：
   - `equivalent` → **reconsolidation**：舊頁 evidence append、
     `S += 1`、`last_reinforced_at` 更新、confidence +0.1（cap
     0.9）、新表述進 Nuances（cap `MAX_VARIANTS`，滿了丟最舊）、
     updated 更新。**不新建頁**。全部 cortex_store 節級操作，
     無 LLM call。
   - `entails` / `entailed_by` / `complementary` → 新建頁 + 雙方
     `related` 互記 claim_id。
   - `contradicts` → 新建頁 + 雙方 `contradictions` 互記 +
     雙方 confidence −0.2（floor 0.1）。
   - `unrelated` / 無鄰居 → 直接新建頁。
6. **入索引**：新建或變更的 Cortex 頁呼叫
   `rag.add_document(path, claim_id, rendered_markdown)` +
   `rag.add_facets(path, claim_id, [claim 文字])`。
7. **報告**：摘要一行進 `maintenance.log.md`（仿 routing report 的
   `_append_maintenance_log`）；有新建/合併才寫 `fromLingLing/`
   報告（新建 N、合併 M、矛盾 K、配額用量）。

State ledger（`CORTEX_STATE_FILE`，原子寫入）：
`{"processed": {insight檔名: {...}}, "claim_embeddings": {...}}`。

### 2.5 接線

- `maintenance_scheduler.py`：新 MaintenanceTask
  `cortex_consolidation_daily`，`daily=True`、`idle_required=True`、
  `window_start_hour=settings.DREAMING_FROM`、
  `window_end_hour=settings.DREAMING_TO`（與 insight_daily 同窗口）、
  intent `maintenance.cortex_consolidation`。
- **刪除安全**：`rag_manager.prune_orphan_chunks` 的預設 roots 加入
  `CORTEX_DIR`；`vault_watcher._should_index` 與 `_is_indexed_dir`
  加入 `CORTEX_DIR`；`main.py` observer 增加 schedule。
  （Cortex 頁被使用者刪除 → chunks/facets 自動清理。）

## 3. 驗收標準

1. 全套既有測試綠（737 起跳），零修改既有測試。
2. 新測試（hermetic）至少涵蓋：
   - cortex_store round-trip（含中文 claim、特殊字元、空 lists）
   - 候選篩選（refuted 排除、低 groundedness 排除、processed 排除、
     groundedness=None 通過）
   - equivalent → 合併不新建（evidence 增長、S+1、變體 cap、
     confidence cap 0.9）
   - contradicts → 雙方互記 + confidence 下調 floor 0.1
   - unrelated/無鄰居 → 新建頁 + facets + add_document 被呼叫
   - 裁決快取命中不再呼叫 LLM；同對 claim 順序對調 key 相同
   - 兩個配額（insights/adjudications）到頂即停
   - extract_claims / adjudicate_claims 的 JSON 與非法輸出防禦
     （mock `_complete_text`，含 MagicMock）
   - state/cache 檔損毀 → warning + 重建不 crash
3. flag off（`CORTEX_CONSOLIDATION_ENABLED=false`）→ 任務 skipped，
   零副作用。
4. 文件：`.env.example`、README Refactor Notes 一節。
5. **只 commit 交付清單內的檔案**。

## 4. Review 重點預告

- 不變量 2（一頁一主張）與 5（只有 equivalent 合併）的落實。
- cortex_store 是否真的是唯一寫入路徑（grep 不准有第二處
  open/write Cortex 頁）。
- 裁決 prompt 是否把「雙向蘊涵」判準寫清楚。
- 配額與快取的邊界行為；ledger 原子性。
- Conventions 全條款（尤其 §4 推導優於追蹤、§9 hermetic）。
