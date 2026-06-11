# Cortex Phase 2 — Review（2026-06-11，Gemini 3.1 Pro 審查）

> 裁定：**APPROVE**（commit 446e9f0，零 must-fix）。本檔為 Gemini
> 審查報告存檔 + reviewer 回應。三個 nitpick 中第 3 個（外部編輯
> 不 bump `updated` 導致 embedding 快取過期）已於合併前修復：
> 快取失效條件加入 claim content hash（commit 見 merge 前一筆），
> 附回歸測試。第 1、2 個維持現狀（中文主場景下 8 字元門檻合理；
> confidence floor 與降級對接本來就是 Phase 4 範圍）。
> 註：報告結尾的 Phase 3/4 名稱有誤——按主計畫，Phase 3 是
> 衰減（S/R 模型），Phase 4 是主張帳本與 falsified。

---

# Cortex Memory Phase 2 Code Review

## 審查摘要

這是一次高水準的實作。程式碼結構乾淨、職責分明，且嚴格遵守了 `CortexMemory_phase1_brief.md` 中定義的設計不變量（Invariants）與工程紀律。針對此階段的關鍵難點（例如：模型幻覺容錯、快取失效管理、YAML 時間戳陷阱），實作中都已採取了漂亮且具備韌性的防禦性設計。

**審查結果：APPROVE（通過）**，可以直接進入 Phase 3 的開發。

---

## 亮點與符合度確認

### 1. 設計不變量的嚴格堅守
* **一頁一主張（Invariant 2）**：`extract_claims` 的 Prompt 設計非常精準，強制規定「可獨立判真偽的陳述句，不得包含 this/it」，並將輸出鎖死在上限 3 條，且透過 `len(claim.strip()) >= 8` 過濾垃圾產出。每個 Claim 皆擁有自己的 `CortexPage`。
* **寫入路徑單一化（Invariant 2.1）**：`cortex_store.py` 成功封裝了所有寫入邏輯。沒有任何 LLM 能直接覆寫整個檔案。Machine state 由 Frontmatter 統一管理，本文採用 Section 替換，達到了絕對的安全。
* **僅合併 Equivalent（Invariant 5）**：`_Consolidator` 中只有 `verdict == "equivalent"` 會觸發 `_merge_into`，其他判定（如 entails, contradicts）均正確降級為 typed links。 Prompt 也特別聲明特例/通例不能算 equivalent。

### 2. 工程紀律與容錯設計 (Fail-open)
* **Adjudicate 退路機制**：LLM 解析失敗或給出定義外的值時，穩健地 Fallback 到 `unrelated`，以「保守不合併、不連結」的姿態完美詮釋 Fail-open。
* **PyYAML 偷轉型防禦**：`_as_str()` 針對 PyYAML 的 `datetime/date` 劫持做了漂亮的 ISO 轉回，確保 `render(parse(page))` 可以 100% Round-trip，這是很常見但極容易被忽略的坑。
* **Crash 韌性**：`state_file` 的寫入在每個 Insight 完成後立刻落盤 (`state["processed"]`)，即便中途當機也能在下一次喚醒時接續，不會浪費 quota。

### 3. 快取與效能優化
* **Embedding 快取失效機制**：把快取的 Validity 綁定在 `page.updated` 上，精準且低成本，徹底解決了 reconsolidation 發生時向量過期的問題。
* **Adjudication 快取修剪**：採用了「雙方都離開 Cortex 才刪除」的保守策略，確保有價值的「無關聯」裁決紀錄不會被錯誤回收。
* **Fast-path 相同主張合併**：`claim_id` 直接比對，避開了無謂的 LLM Adjudication，節省珍貴的 API quota。

---

## 微小建議 (Nitpicks / Optional)
這些點不影響 Phase 2 的通過，可視情況在未來階段微調：

1. **`llm_client.py` - `extract_claims` 容錯**
   - 目前 `len(claim.strip()) >= 8` 的過濾標準已經不錯，但如果 LLM 回傳全英文，8 個字元可能只是兩個單字（例如 "Dogs run"）。不過考慮到系統主語言為中文，8 個字元的門檻算合理。
2. **`_dent_confidence` 廣播更新問題**
   - 當一個新的 Claim 被標記為 `contradicts` 時，程式會扣減被矛盾對象 (`other`) 的 confidence。這是正確的。但如果被矛盾對象的 confidence 掉到 0.1，後續 Phase 4 可能會將它降級，這部分留待後續實作對接即可。
3. **MtimeCache 在 `vault_watcher` 與 Cortex 的連動**
   - 雖然 `cortex_store` 是唯一的寫入路徑，但如果有外部工具（例如 Obsidian）直接修改了 `Cortex/` 目錄下的 Markdown（例如加上雙向連結），這會改變檔案的 `updated` 嗎？如果沒改到 Frontmatter 的 `updated`，向量快取可能不會更新。未來如果允許手動干預 Cortex 頁面，可能需要讓 Vault Watcher 同步更新 `updated` 時間戳。

---

## 結論

**這是一次教科書等級的 Agentic Feature 交付。** 所有設計決策都圍繞著安全、冪等（Idempotent）與容錯（Fail-open）展開。762 個全綠測試證明了架構的穩健性。

請繼續保持這個節奏，我們可以開始準備進入 **Phase 3 (Insight 關聯拓撲)** 或 **Phase 4 (遺忘與收斂)** 了！
