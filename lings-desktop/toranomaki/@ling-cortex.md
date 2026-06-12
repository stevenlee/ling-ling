> [!TIP]
> **發動方式**：將此檔案拖入 `toLingLing/` 資料夾。
> **效果**：對 Cortex 長期記憶層跑一次完整的三層驗證，報告寫入 `fromLingLing/`。
>
> **驗證內容**：
> 1. **管線健康（紅線）**：頁面可解析、claim_id 唯一、索引一致（chunks + facets）、無懸空 facet、bench 無退步。
> 2. **鞏固品質（黃線）**：claim 產率、groundedness 分佈（只計過閘 insights）、refute 覆蓋率、falsifiability 分佈（mean < 0.4 警示）。
> 3. **檢索效益**：facet lift、Cortex 頁被檢索命中次數。
>
> **報告還會列出**：
> - 各狀態分佈（active / fading / dormant / falsified）
> - ⚔️ 矛盾對——知識庫裡互相打架的主張
> - 🪦 已 falsified 的主張（檔案保留，記錄曾相信過什麼）
> - 🔍 人工抽查清單：每條主張附「證偽」提示——**覺得爛的直接刪頁，
>   刪除就是品質投票**（存活率是系統的回饋訊號）
>
> **建議時機**：系統跑了幾晚 dreaming window 之後、或大量 ingest 之後，
> 想看看大腦長成什麼樣子時。
