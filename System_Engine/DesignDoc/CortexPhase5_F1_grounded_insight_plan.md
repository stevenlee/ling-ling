# Cortex Phase 5 · F1 — Cortex-grounded Insight（實施計劃）

> 狀態：**F1 全部五條防禦已實作（2026-06-14），flag 仍預設 OFF。** 五防禦俱全，現在開 flag 是安全的——但建議：開 flag → 讓 grounded 洞察跑幾晚 → 跑 `echo_canary` → 無 alarm 再維持開啟（enable protocol）。F2（`@ling-recall`）、F3（`@ling-tensions`）已上線。
>
> **五防禦落地對照**：
> - ② 辯證 framing → `insight_agent._grounding_block`（測試：`test_grounding_block_is_dialectical`）。
> - ③ falsifiability 閘 → `_cortex_priors`（`test_cortex_priors_falsifiability_gate`）。
> - 注入 + provenance 標記 → `_expand_seed` + `_grounded_on_acc` → 報告 frontmatter `grounded_on`。
> - ① provenance 防火牆（兩條循環路徑都堵）：
>   - **強化路徑**：`cortex_consolidation._merge_into` 若 grounded 洞察附和自己的先驗 → 記 evidence、跳過 S/confidence 強化（`test_firewall_skips_reinforcement_on_self_agreement`）。
>   - **falsification 路徑**：`cortex_ledger._independent_insights(exclude_grounded_on=page.claim_id)` 把「被 prompt 來挑戰該主張」的洞察排除在獨立計數外，避免辯證 framing 製造出反例去殺自己的先驗（`test_independent_insights_excludes_grounded_self_dissent`）。
> - ④ 非對稱信任 → 同上：只有外部證據能 reinforce，自我附和不能（`test_normal_merge_still_reinforces`）。
> - ⑤ 同溫層金絲雀 → `maintenance/echo_canary.py::run_echo_canary`，比較 grounded vs cold 的 novelty/groundedness，novelty 系統性偏低 → alarm（`test_echo_canary.py`）。
>
> **開 flag 前的待辦（operational）**：把 `run_echo_canary` 註冊成週任務（mirror `weekly_memoir`）以自動監看；目前是 on-demand 函式。
>
> ~~原 stage 1/2 規劃如下↓~~
>
> **Stage 1 已完成（commit 在 feat/cortex-phase5-f1-grounded）**：
> - `insight_agent._should_ground(idea)`：flag + `CORTEX_GROUND_FRACTION` 決定哪些 seed 注入（hash-based 決定性，留 cold 控制組給 canary）。
> - `_cortex_priors(idea)`：falsifiability 閘（防禦③，只取 `>= CORTEX_GROUND_MIN_FALSIFIABILITY` 的主張；小語料用全部、大語料 recall_claims 排序 top-K）。
> - `_grounding_block(priors)`：辯證式 framing（防禦②，「請挑戰不要附和、最有價值是張力與反例」）。
> - `_expand_seed`：注入 grounding block，回傳 dict 帶 `grounded_on=[claim_id...]`（provenance 標記）。flag off → byte-identical。
> - flags：`CORTEX_GROUNDED_INSIGHT_ENABLED`(False)/`_MIN_FALSIFIABILITY`(0.5)/`_TOP_K`(3)/`_FRACTION`(0.7)。6 tests。
>
> **Stage 2 待做（精確 hook，讀碼後確認）**：
> 1. **Plumb `grounded_on` → frontmatter**：`generate_insight`/`generate_full_insight` 開頭 `self._grounded_on_acc=set()`；`_expand_seed` 把 `grounded_on` update 進去；`generate_insight` 把 `sorted(self._grounded_on_acc)` 寫進 `meta["grounded_on"]`（→ 報告 frontmatter → Insights/ mirror）。
> 2. **Provenance firewall（防禦①+④）於 `maintenance/cortex_consolidation.py`**：`run_consolidation` 讀 insight `meta.get("grounded_on")`；傳進 `process_claim` → `_merge_into(page, claim, evidence, grounded_on)`：**若 `page.claim_id in grounded_on` → append evidence 但跳過 S+1/confidence 上調**（grounded 洞察附和自己的先驗不算 reinforcement）。把 `grounded_on` 也存進 evidence dict，供 `cortex_ledger` 的「≥2 獨立來源」falsified 計數排除非獨立者。
> 3. **Canary（防禦⑤）**：insight 依 `meta` 有無 `grounded_on` 分 grounded/cold 兩組，比較 `groundedness/novelty/falsifiability` 分佈；grounded 組同時更低 novelty+falsifiability → 告警。複用 memoir 報告基建（`maintenance/echo_canary.py`）。
> 4. 全部就緒 + canary 驗證無同溫層特徵後，才可 `CORTEX_GROUNDED_INSIGHT_ENABLED=true`。
> 主題：**關閉記憶迴路**——讓累積的 Cortex 長期記憶**主動參與**洞察生成，但**不製造同溫層**。

## 0. 一句話

Monte Carlo 洞察生成時，注入最相關的高信心 Cortex 主張當「你已經相信什麼」，**但以辯證方式使用**（挑戰新材料、獎勵張力），並用 provenance 防火牆 + falsifiability 閘 + 非對稱信任 + 金絲雀量測防止自我印證。

## 1. 為什麼

- 記憶現況：Cortex 主張被生長/衰減/falsify/驗證，但生成端（insight / synthesis / answer_query）**完全不主動讀**。記憶只寫不讀 = 半套。
- F1 直接打「洞察太空靈/無根」——gen-mix 的 doc-anchored seeds 從**素材端**攻，F1 從**記憶端**攻，兩面夾擊。

## 2. 核心風險與防禦（這是 F1 的靈魂，不是附錄）

天真版（「這是你相信的，生成洞察」）會把系統自己的 Popperian 防禦拆掉，產生：確認偏誤、**循環引證**、異議壓制、信心膨脹。F1 的設計原則：**記憶用來挑戰，不是附和（dialectical, not confirmatory）**。

五條 load-bearing 防禦，全部列為**驗收硬條款（缺任一不准 merge）**：

1. **Provenance 防火牆**：F1 注入時，被生成的洞察必須標記它「看過哪些 Cortex claim_id」（寫進 insight frontmatter，如 `grounded_on: [claim_id, ...]`）。下游鞏固（Phase 2/4）**不得**讓一個洞察去強化/支持它 grounding 過的主張——擴張 Phase 4「≥2 獨立 insight」的**獨立**定義：看過主張 A 的洞察對 A 不算獨立證據。**斷掉循環引證閉環。**
2. **辯證式 framing**：注入先驗的 prompt 必須要求 LLM 找出新材料**在哪裡推翻/修正/延伸**先驗，明確**不准**只複述附和。輸出的價值排序：矛盾 > 修正 > 延伸 > 附和。
3. **Falsifiability 閘**：只注入 `falsifiability >= CORTEX_GROUND_MIN_FALSIFIABILITY`（預設 0.5）的主張當先驗。不可反駁的主張（模糊全稱）**永不**當錨——它們無法被推翻、只會自我強化。
4. **非對稱信任**：F1 grounded 洞察若**附和**先驗 → **不**觸發任何 reinforcement（S/confidence 不動）；若**矛盾**先驗 → 照常進 Phase 4 falsification 複審。只有**外部**證據能升信心，自我附和不能。
5. **同溫層金絲雀**：F1 上線即內建量測——對照 **grounded 洞察 vs cold（無記憶）洞察**的 `groundedness / novelty / falsifiability / 矛盾數`。同溫層特徵 = grounded 系統性更低 novelty + 更低 falsifiability。寫進每週報告（或併入 6/26 falsifiability 檢查）；命中特徵 → 自動告警 + 建議回退 flag。

## 3. 規格

### 3.1 Flag（`core/config.py`）
- `CORTEX_GROUNDED_INSIGHT_ENABLED`（預設 **false**——先 cold 對照跑一陣子才開）。
- `CORTEX_GROUND_MIN_FALSIFIABILITY`（預設 0.5）。
- `CORTEX_GROUND_TOP_K`（預設 3）——每個 seed 注入幾條先驗。
- `CORTEX_GROUND_FRACTION`（預設 0.7）——只有這比例的 seed 注入記憶，其餘刻意 cold（保留 ε 探索精神，留新方向冒出的空間）。

### 3.2 注入點
- `agents/insight_agent.py` 的 `_expand_seed`（或 cross-round synthesis）：seed 已有 `idea`，用 `recall_claims(self.rag, idea, top_k=CORTEX_GROUND_TOP_K, statuses=("active","dormant"))` 撈先驗，**再過 falsifiability 閘**。
- 注入的 prompt 區塊（辯證式）：
  ```
  你對相關主題已有的信念（附可反駁性）：
  - {claim}（可反駁性 {fals}；反例：{falsifier}）
  ...
  任務：這份新材料在哪裡【推翻/修正/延伸】上述信念？最有價值的是張力與反例，不是附和。
  若新材料只是複述既有信念，明說「無新增」而非硬湊。
  ```
- frontmatter 標記：`grounded_on: [claim_id...]`、`grounded: true`。cold seed 不標。

### 3.3 Provenance 防火牆（鞏固端）
- `maintenance/cortex_consolidation.py`：抽主張時，若來源 insight 的 frontmatter 有 `grounded_on`，這條新主張對 `grounded_on` 內的 claim_id **不可**作為 reinforcement/支持證據（matching/合併/Phase 4 independent-count 都排除）。
- 實作：把 `grounded_on` 帶進 claim 的 evidence provenance；Phase 4 的「獨立來源」計數過濾掉 provenance 重疊者。

### 3.4 非對稱信任
- grounded 洞察鞏固出的主張：若與其 `grounded_on` 主張**語義一致**（adjudicate=equivalent/entails）→ **不**做 reconsolidation 的 S+1/confidence 上調（只記連結）；若 `contradicts` → 照常雙向記矛盾 + 進 Phase 4。

### 3.5 金絲雀
- `insight_signals` 已算 groundedness/novelty/refute；falsifiability 在鞏固時算。新增一個彙整：把洞察依 `grounded` 分兩組，比較四訊號的分佈。
- 出口：併入 weekly memoir 或新 `maintenance/echo_canary.py`（複用 memoir 報告基建）。命中同溫層特徵（grounded 組 novelty & falsifiability 同時系統性偏低）→ `fromLingLing/` 告警 + log。

## 4. 測試（hermetic）
- recall 注入：mock recall_claims 回固定主張，斷言 prompt 含辯證 framing + falsifiability 閘擋掉低分主張。
- falsifiability 閘：低於門檻的主張不進 prompt。
- fraction：`CORTEX_GROUND_FRACTION` 控制注入比例（確定性切分，非隨機不可測——用 seed index % 之類）。
- **provenance 防火牆**：grounded_on=[A] 的洞察抽出的主張，Phase 4 獨立計數**不**把它算進 A 的支持/矛盾獨立來源（防循環）。
- **非對稱信任**：grounded 洞察與先驗一致 → 無 S/confidence 上調；矛盾 → 進複審。
- flag off → 行為與現狀 byte-identical（cold 路徑不變）。
- 金絲雀：給定 grounded/cold 兩組訊號，正確算出分組分佈、命中特徵時告警。

## 5. 驗收硬條款
1. 全套既有測試綠，零修改既有測試（除非是擴張 Phase 4 獨立定義的那條——若動到，明確標注並說明）。
2. §2 的 ①②③④⑤ 五條防禦**全部**有對應實作 + 測試。缺任一不准 merge。
3. flag 預設 false；off 時 byte-identical。
4. Live：真實 vault 跑一次 grounded insight，貼 transcript，並用金絲雀對照 cold 版的四訊號（證明 grounded 沒有壓低 novelty/falsifiability）。
5. README/CHANGELOG 一條；本檔狀態更新為完成。

## 6. 哲學註腳（寫進 PR 說明）
這是**個人**知識系統，本就該反映你的世界觀。F1 的目標不是消滅你的視角，而是防止系統用**自我引證**製造假確定、或壓制**你自己**讀進來的矛盾材料。成品該是**忠實而自我批判的鏡子**——連同知識裡的張力一起照出來。衰減 + 可反駁性 + 浮現異議（F3）是讓它「會自我懷疑」的三根支柱。

## 7. 與 F2/F3 的關係
- **F2（已完成）**：`recall_claims` 原語 + `@ling-recall`。F1 重用 `recall_claims`。
- **F3（F1 之後）**：張力摘要——主動浮現矛盾/瀕死/薄證據主張。是 §2 防禦⑤「浮現異議」的使用者出口，與 F1 互補。
