# Release Notes - v0.3.0 (Planner Execution & Source-Grounded Insight)

# Ling-Ling Mentor System v0.3.0 發行說明

Ling-Ling v0.3.0 把 Insight 從固定流程推進到可規劃、可檢查、可執行的工作流。`@ling-insight planner-mode` 現在能先產生可驗證的 recommended plan，並在使用者明確加入 `/execute` 或 `/execution` 後，通過 readiness gate 執行 allowlisted adapters。

## 核心進化

- **Planner Mode for Insight**：`@ling-insight planner-mode` 預設只預覽；`/execute` 和 `/execution` 才會進入 guarded execution。
- **Source Loading Adapter**：新增 `vault.load_sources`，能把 vault wikilinks/titles 解析成真正的 markdown source text。
- **Source-Grounded Final Answer**：新增 `llm.answer_from_sources`，用載入的 source text 產出最終回答，而不是把任務誤送進 critique。
- **Readiness Gate**：Planner plan 執行前會檢查 capability、adapter、context keys、上游輸出形狀與常見 misuse，verdict 以 `ready` / `needs_review` / `blocked` 呈現。
- **Execution Source Appendix**：執行報告會列出載入來源、檔案路徑、source kind、原始長度、載入長度與是否截斷。
- **LLM Context Fix**：修正 `answer_query(custom_instruction=...)` 未把 `wiki_context` 餵給 LLM 的問題；這是 0.3 source-grounded execution 的關鍵修復。

## 操作方式

```md
@ling-insight planner-mode [[文章A]] [[文章B]] 請比較兩者並提出行動指引
```

只產生 planner preview。

```md
@ling-insight planner-mode /execute [[文章A]] [[文章B]] 請比較兩者並提出行動指引
```

通過 readiness gate 後執行 plan。`/execution` 是同義寫法。

## 已知限制

- 長文本 source loading 仍採 `max_chars_per_source` 前段截斷，預設由 `LOAD_SOURCES_MAX_CHARS_PER_SOURCE` 控制，預設值 `20000`。
- 多本長書的深度比較仍需要下一階段的 `digest_sources` / map-reduce 流程，避免前段偏誤與 context 壓力。
- 若某本書在 vault 中只有 Synthesis，loader 會載入摘要來源；這會在 Source Appendix 中顯示為 `source_kind: synthesis`。

## 下一步

v0.3.1 將聚焦 `digest_sources`、多書 map-reduce、以及更完整的 source-aware planner canonical flow。
