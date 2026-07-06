### Output Format (Code Review)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Code Review: (受檢對象)"
tags: ["code-review", "engineering"]
type: "code-review"
---
```

After the closing `---`, the Markdown body follows the structure below. The review *method* — few-but-precise findings, identifier anchors (never line numbers), verbatim snippets, verdict-first, credit where due — comes from the `review_code` operation; this template only fixes the sections.

## 總評
Lead with the verdict: one honest sentence on the overall health of this code, then the single most important thing to address. If nothing is wrong, say so plainly rather than manufacturing concerns.

## 發現
Findings, ordered by severity (most serious first). Each finding is a `###` subsection:

`### 💧 / 🌱 / 🍵 `函式或類別名`: 一句話問題摘要`

- **嚴重度**: 💧 需修（會出錯或誤導維護者）／🌱 建議（有更好的寫法）／🍵 見仁見智（風格偏好）
- **位置**: 檔名 → 函式/類別/模組名（用原始碼裡的確切識別符;不要用行號）
- **問題**: what's wrong and why it matters to a maintainer.
- **摘錄**: a short fenced code block, copied verbatim from the source.
- **建議**: how you'd change it (a sketch or a concrete snippet).

If a finding depends on something not shown (a caller, a config, a test), mark it **需人工確認** and say what you'd need to see.

## 值得學的地方
The patterns, structures, or decisions this code does *well* — concretely, by name. This is genuine credit, not a courtesy; skip the section only if there is honestly nothing worth highlighting.

## 下一步
An ordered action list: what to fix first, what test is missing, what refactor is worth doing. The reader should close this knowing exactly where to start.
