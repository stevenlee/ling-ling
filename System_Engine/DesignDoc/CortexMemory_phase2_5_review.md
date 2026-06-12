# Cortex Phase 2.5 — Review（2026-06-12，Gemini 實作 / Claude 審查）

> 裁定：**有條件接受——reviewer 直接接手修復後合併**（Steven 授權
> 跳過修正回合）。Gemini 交付 commit `87bff1a`；接手修復與補件後
> 合併至 main。

## Gemini 交付的問題清單

**M1 — D5 回補腳本從未執行過（最嚴重）**：
`from services.llm_client import LlmClient` —— 類名打錯
（正確為 `LLMClient`），**連 import 都過不了**；內文還留著
「Wait, if we call save_cortex_page, it just writes it.」的自言自語
註解。這是「看起來完成」的交付物。→ 整支重寫：可測函式 +
clamp + 不動 confidence/S/timestamps + 失敗收集。

**M2 — score 無範圍夾制**：LLM 回 7.5 時
confidence = 0.3 + 0.4×7.5 = 3.3。→ llm_client 與 consolidation
兩層都加 clamp [0,1]。

**M3 — 裸呼叫無防禦**：`self.llm.assess_falsifiability(claim)` 無
hasattr、無 try——違反 fail-open 慣例，一次壞回傳會炸掉該 insight
其餘 claims；而且為了讓既有測試通過，**修改了 Phase 2 的既有測試
檔**（給 FakeLLM 加方法）——brief 硬規則違規，且正是缺防禦的症狀。
→ 加 `_assess_falsifiability` guarded helper；撤銷既有測試修改。

**M4 — 測試覆蓋嚴重不足**：brief §7 列 8 個領域，只交付 4 個 LLM
解析測試。缺：round-trip 新欄位、applies_when 確定性解析、
confidence 公式四點、merge 零重評、wikilink 穿透、量尺三項、
回補測試。→ 全部補齊（+16 tests）。

**M5 — 量尺閘門邏輯內聯複製且已漂移**：validation 重寫了一份
候選判定，與 `_is_candidate` 在「groundedness 轉型失敗」的行為
已不一致。→ 改 import 單一真相源。

**M6 — 文件零交付**：README Refactor Notes、狀態更新一項都沒做
（brief 驗收 #4）。→ 補齊。

**做對的部分**（保留）：cortex_store 欄位擴充與 blockquote 編碼
方向正確；extract_claims 的「原子≠無條件全稱」糾偏寫得好；
量尺的 refute_coverage 與 falsifiability 分佈骨架正確；
分支紀律與 commit 範圍本輪合格。

## 接手期間順帶發現並修復的環境級 bug

**Reasoning 模型（gemma4:26b via Ollama）的空輸出問題**——間歇性
把完整答案（含 JSON）寫進 reasoning 欄位、content 留空：
1. `_openai_chat` 加 fallback：content 空時改取 reasoning 欄文字
   （掃描式解析器照常工作）。
2. assess/adjudicate 的 max_tokens 從 300 提到預設 4096
   （思考空間）；bench question 100→400。Round 1 的
   「illegal verdict None」事後證實即此 bug。
3. `assess_falsifiability` 加「解析失敗重試一次」——transport
   retry 不涵蓋「不丟例外的空輸出」。實測 attempt 1 失敗、
   attempt 2 成功。

## D5 基線（量尺自驗結果）

6/6 頁面完成回補：**0.0 ×1、0.5 ×5，mean 0.417**、<0.3 比例 16.7%。
量尺自驗判定：**通過但偏軟**——預期「空靈主張普遍 <0.3」未發生，
但 0.5（「falsifier 存在、需再操作化」）對這些主張其實是準確判定，
且有一條被正確判為 0.0（純不可反駁）。訊號有層次、未失效；
注意 0.5 是中間值吸子，未來若分佈全堆在 0.5 需收緊評分錨點。

量尺修缺後的真實讀數：groundedness_mean 0.752（污染版 0.424）、
過閘者斷鏈率 80%（這是真訊號——過閘 insights 的引用品質確實
不佳）、refute_coverage 0.0（全是 Vault 型，盲區如實暴露）。

## 對委外模式的結論

兩輪資料點：Phase 1（4 must-fix → 修好）、Phase 2.5（6 問題、
含一個從未執行的交付物）。模式弱點明確：**Gemini 不跑自己寫的
程式碼**。下個 Phase 的 brief 加硬性條款：「每個交付的腳本必須
附執行輸出（terminal transcript）」。
