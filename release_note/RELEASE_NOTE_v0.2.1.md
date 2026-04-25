# Release Notes - v0.2.1 (Navigation & Refinement)

# 🎀 Ling-Ling Mentor System v0.2.1 發行說明

Ling-Ling v0.2.1 聚焦於提升長文閱讀的連貫性與手動清洗素材的便利性。我們引入了「電子書式導航」與全新的「Consolidate 投遞流程」，讓知識精煉的過程更加優雅且精確。

---

## ✨ v0.2.1 核心進化亮點

### 📑 電子書式導航連結 (E-book Style Navigation)
*   **跨頁互聯**：解析後的每個 Part 底下現在會自動生成「上一篇 | 下一篇」連結，支援流暢的連續閱讀。
*   **維度跳轉**：新增「查看全文總結 (Synthesis)」與「查看完整原始檔 (Original)」快速連結，讓您在局部細節、整體大綱與原始出處之間無縫穿梭。

### ⚙️ Consolidate 精煉工作流 (Refined Workflow)
*   **暫存與處理分離**：
    *   **`Clippings/`**：回歸純粹的暫存緩衝區，系統不再主動監視，方便您在此進行手動清洗（刪除廣告、版權雜訊）。
    *   **`Consolidate/`**：全新的投遞入口。整理好的檔案拉進這裡，立即觸發 AI 解析流程。
*   **歸檔對稱性**：處理完畢的原始檔將自動歸類至 `raw/consolidate/`，與入口名稱完美對稱。

### 🎨 專業化狀態反饋 (UI Terminology Upgrade)
*   **層級化提示**：終端機狀態更新為更精確的動詞序列：
    *   `Preparing`: 系統預處理與分段檢查。
    *   `Distilling`: AI 正在針對 Part 進行深度蒸餾。
    *   `Synthesizing`: 正在合成最終的總結報告。
    *   `Successfully Consolidated`: 任務圓滿達成。

### 🛠️ 穩定性與可靠性強化 (Reliability Boost)
*   **搬移事件感知 (Move Detection)**：優化了監視器邏輯，除了新增檔案外，現在也支援「拖拉/搬移」檔案的動作偵測。
*   **啟動自動巡邏 (Startup Scan)**：系統每次啟動時都會主動掃描 `Consolidate/` 與 `toLingLing/`，確保重啟期間或斷電時漏掉的檔案能被自動補齊。
*   **YAML 解析加固**：修正了當 YAML frontmatter 存在空欄位（如 `title: `）或日期缺失時導致的系統崩潰。
*   **索引版面優化**：`index.md` 現在會將原始檔與處理後的頁面智慧歸類，並優化了日期的顯示邏輯。

---

## 🛠️ 修復與改動清單
*   **目錄調整**：新增 `Consolidate/` 與 `raw/consolidate/`。
*   **邏輯更新**：`ClippingWatcher` 現在僅監視 `Consolidate/`。
*   **穩定性**：修復 `NoneType` 排序錯誤與 Gutenberg 版權雜訊導致的英語偏誤（透過手動清洗建議）。

---

**總結：**
v0.2.1 讓 Ling-Ling 的知識處理不再只是單向的輸出，而是構建了一個可來回穿梭的導航網絡。透過手動清洗與自動精煉的分工，您的知識庫品質將提升到一個全新的層次。

*「精煉過的知識，才真正屬於你。」* ── 玲玲 v0.2.1，陪您更有紀律地學習。
