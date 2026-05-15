
> [!TIP]
> **發動方式**：將此檔案拖入 `toLingLing/` 資料夾。
> **效果**：系統會根據當前知識庫內容，產出深度語意洞察報告。
>
> **全方位掃描 (大絕招)**：
> - 執行所有已知的分析策略：`/full`
>
> **指令簡寫 (指定單一分析方法)**：
> - 尋找知識孤島：`/islands`
> - 方法論解析：`/meta-methods`
> - Monte Carlo 隨機探索：`/montecario`
> - 最近閱讀總結：`/recency`
> - 標籤叢集分析：`/tag` 或 `/tags` (例如 `/tag #gdp`)
>
> **Monte Carlo 目標模式 (Targeted Monte Carlo)**：
> - 指定 2 篇文章，探索它們之間的深層連結：
>   `/montecario [[文章A]] [[文章B]]`
> - 指定 1 篇文章，探索它與知識庫其他內容的連結：
>   `/montecario [[文章A]]`
> - 不指定目標，完全隨機探索：
>   `/montecario`
>
> **Monte Carlo 運作原理**：
> 1. 🎲 **Spark**：生成 6 組隨機配對，以高創意度評估交叉連結
> 2. 🏆 **Filter**：根據新穎度評分，保留前 3 名種子
> 3. 🔬 **Expand**：透過語意搜索找到支持證據，深度展開
> 4. ✨ **Synthesize**：以低溫度進行最終綜合報告
>
> **語法自由 (指定報表格式)**：
> - 支援空格或冒號：`/template tech-rpt` 或 `/template:tech-rpt`
>
> **視覺化自定義**：
> - 您可以隨時編輯 `Scripture/Guidelines/Visualization.md` 來調整 Mermaid 繪圖風格。
