> [!TIP]
> **發動方式**：將此檔案拖入 `toLingLing/` 資料夾，並在檔名附加上 Plan ID。
> **效果**：系統會自動載入該計畫並嚴格執行 (PipelineRunner)。
>
> **基本用法**：
> 1. `@ling-do my_custom_plan`
>
> 這是 Phase 5 引入的執行代理 (Executor Agent)。它會去 `Database/plans/` 尋找 `my_custom_plan.json` 或 `my_custom_plan.yml` 並執行。您可以搭配 `@ling-plan` 或 `planner-mode` 先產生腳本，確認無誤後再使用此指令執行。
