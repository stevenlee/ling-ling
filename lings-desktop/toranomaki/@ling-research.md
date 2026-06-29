> [!TIP]
> **發動方式**：在任何筆記（Notes 或 Pages）的內文打上 `@ling-research [想探索的主題]`，存檔後系統即會在背景自動觸發。
> **效果**：自動啟動雙軌檢索策略（學術深掘與專利廣搜），並將檢索結果精美地附加在筆記最下方。
> 
> **一般探索模式**：
> - 直接針對特定主題進行研究 (由 LLM 自動產生檢索關鍵字)：
>   `@ling-research Solid state battery`
>   `@ling-research Transformer模型架構`
> 
> **指定關鍵字模式 (Direct Keyword)**：
> - 想要精準控制搜尋關鍵字，可加上 `keywords:` 或 `kw:`：
>   `@ling-research kw: quantum computing, machine learning`
> - *註：若您指定的關鍵字少於五個，系統會自動呼叫 LLM 幫忙補充到五個；若超過五個，則全數採用您的設定，不再額外補充。*
> 
> **基於筆記的進階擴展 (Agentic RAG)**：
> - 想要為某篇既有筆記尋找延伸學術文獻？直接用雙向連結：
>   `@ling-research [[Energy_Storage]]`
> - 當然也可以結合自訂關鍵字，指引 LLM 重點搜尋方向：
>   `@ling-research [[Energy_Storage]] kw: safety issues`
> - *系統會自動讀取該筆記的全文化作上下文，並自動產出/結合您的關鍵字為您找來高度相關的延伸文獻。*
> 
> **檢索策略 (雙軌制)**：
> 1. **精兵主義 (arXiv / Wikipedia)**：由 LLM 嚴格篩選出最相關的 3~5 篇，提供深度摘要（Elite Digest）。
> 2. **大範圍掃描 (USPTO 專利)**：重視廣度與防禦性。一次檢索最高 30 篇專利，由 LLM 結構化整理為 Markdown 表格，方便您快速掃視（Skim reading）。
> 
> **一鍵下載至剪貼簿 (Clippings)**：
> - 系統為您生成的「學術摘要」區塊中，每筆資料都會附帶一個專屬的下載指令，例如：
>   `@ling-download 2110.01831v1`
> - 當您閱讀摘要覺得非常有價值時，只要保留這個指令並存檔，系統就會瞬間將該文獻建立在 `Clippings/` 目錄下，並自動與您當前的筆記建立雙向連結！
