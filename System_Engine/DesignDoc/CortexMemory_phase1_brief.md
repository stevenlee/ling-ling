# Cortex Phase 1 — 實作規格（委外交付用）

> 委託範圍：**只有 Phase 1（insight 品質訊號塔，第 1、2 層）**。
> 必讀順序：① [Engineering_Conventions.md](Engineering_Conventions.md)
> ② [CortexMemory_implementation_plan.md](CortexMemory_implementation_plan.md) §1、§8
> ③ 本文件。
> 工作分支：`cortex/phase-1`。Review 修正最多兩輪。

## 0. 明確的「不做」清單（碰了直接退回）

- ❌ `Cortex/` 目錄、鞏固、聚類、合併（Phase 2）
- ❌ S/R 衰減模型、狀態轉換、simulation.py（Phase 3）
- ❌ 主張帳本、falsified（Phase 4）
- ❌ 修改任何既有測試
- ❌ 修改 `_build_system_prompt`、busy lock、scheduler 核心

Phase 1 是**純測量**：替每份 insight 算分數、記下來。不根據分數
做任何行動。

## 1. 交付物清單

### 1.1 `services/insight_signals.py`（新模組）

```python
@dataclass
class InsightSignals:
    groundedness: float        # 0–1
    broken_links: list[str]    # 引用但不存在的頁面
    novelty: float             # 0–1（1 = 全新）
    max_similar_insight: str | None   # 最相似的歷史 insight id
    bridging: float            # 0–1（1 = 連接最遠的聚落）
    refute_verdict: str | None # "survived" | "refuted" | None(未跑)
    refute_notes: str          # 反駁者摘要（≤500 字）

def compute_signals(report_content, related_titles, rag, llm,
                    *, run_refute: bool = True) -> InsightSignals
```

**Groundedness（機械，零 LLM）**：
- 抽出內文所有 `[[wikilink]]`（含 `[[A|別名]]` 形式，取 A）。
- 對照 `rag.get_all_indexed_titles()` ∪ vault 檔案存在性
  （`PAGES_DIR`/`NOTES_DIR` 下 `<title>.md` 是否存在）。
- `groundedness = 存在的引用數 / 總引用數`（無引用時 = 1.0，
  並不算失格——但 0 條引用的 insight novelty 權重自然低）。
- 失效引用列入 `broken_links`。

**Novelty（機械，零 LLM）**：
- 用 `rag.ef`（RAGManager 的 embedding function）對 insight 的
  核心文字（報告去除 frontmatter 後前 2000 字）算 embedding。
- 與**歷史 insight embeddings** 比對取最大 cosine 相似度，
  `novelty = 1 − max_sim`。
- 歷史 embeddings 存 sidecar `Database/insight_signals.json`：
  `{insight_id: {"embedding": [...], "ts": "..."}}`，原子寫入，
  上限保留最近 500 筆（FIFO）。首筆 insight novelty = 1.0。

**Bridging（機械，零 LLM）**：
- 輸入 `related_titles`（InsightAgent 已有 target_titles）≥2 時：
  對每對來源頁算 embedding cosine，
  `bridging = 1 − min_pairwise_sim`（連接越遠的素材分越高）。
- 來源 <2 或 embedding 失敗 → `bridging = 0.0`（fail-open）。

**Refute（第 2 層，1 個 LLM call）**：
- 新增 vault 資產 `Templates/Operations/refute.md`：frontmatter
  仿照 `critique.md`（type: operation / expected_inputs / produces /
  cost_class: medium），本體指示反駁者「找出過度泛化、因果倒置、
  來源不支持之處；結尾一行 `Verdict: survived` 或
  `Verdict: refuted`」。
- `llm_client.py` 新方法 `refute_insight(candidate, sources) -> dict`
  （`{"verdict": "survived"|"refuted"|None, "notes": str}`），
  經 `_build_system_prompt(operation="refute", persona="none",
  forced_template="none")`，trace stage = `refute_insight`。
  verdict 解析仿 `_VERDICT_RE` 的寬容寫法（容忍 markdown 修飾）。
- 任何失敗 → `verdict=None`，不擋主流程。

### 1.2 InsightAgent 整合

- 在 `generate_insight()` 報告產出後、`_write_report()` 前呼叫
  `compute_signals()`，結果併入 meta dict：

```yaml
signals:
  groundedness: 0.86
  novelty: 0.41
  bridging: 0.63
  refute_verdict: survived
signals_version: 1
```

- `generate_full_insight()` 的多策略路徑同樣計分（每策略各一次）。
- 同時把 signals 寫進 trace：`record_artifact` 的 metadata
  （insight 報告本來就 record artifact 的路徑上加欄位即可）。

### 1.3 Config（`core/config.py`）

```
INSIGHT_SIGNALS_ENABLED  default true   # 總開關
INSIGHT_REFUTE_ENABLED   default true   # 第 2 層單獨開關（省成本用）
```

## 2. 驗收標準（全部必須滿足）

1. 全套既有測試綠（726 passed 起跳），**零修改既有測試**。
2. 新測試涵蓋（hermetic，仿 `test_skill_preconditions.py` 模式）：
   - wikilink 抽取（含別名形式）與 broken link 偵測
   - groundedness 計算（全存在 / 部分 / 無引用）
   - novelty：首筆 = 1.0；高相似歷史 → 低分；sidecar 上限 500
   - bridging：遠近素材對比；<2 來源 fail-open
   - refute verdict 解析（survived/refuted/亂格式→None）
   - LLM/RAG 失敗時 fail-open（signals 部分缺值，主流程不擋）
   - flag off → `compute_signals` 不被呼叫
   - frontmatter 出現 `signals` block 且 Insights/ 鏡像 byte-identical
     （沿用既有鏡像契約）
3. `INSIGHT_SIGNALS_ENABLED=false` 時行為與現狀完全一致。
4. 新增 LLM 成本：每份 insight 最多 1 call（refute），可關。
5. 文件：`.env.example` 加兩個 flag；README Refactor Notes 加一節
   （格式仿既有條目）。

## 3. Review 重點預告（會被檢查的事）

- Engineering_Conventions 全部條款，特別是：fail-open、hermetic
  測試、config 引用方式（monkeypatch 可達）、原子寫入 sidecar。
- mock 防禦：MagicMock LLM 打進來時 signals 不得爆炸。
- 鏡像契約：`Insights/` 副本必須與 `fromLingLing/` 報告
  byte-identical（既有測試 `test_insight_agent.py` 守著這條）。
- sidecar JSON 損毀時的行為（warning + 重建，不可 crash）。
