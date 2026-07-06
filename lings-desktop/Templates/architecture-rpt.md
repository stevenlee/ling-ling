### Output Format (Architecture Map)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Architecture: (系統或模組名)"
tags: ["architecture", "engineering"]
type: "architecture"
---
```

After the closing `---`, the Markdown body follows the structure below. The method — list components before drawing relations, keep diagram and prose consistent, never invent modules that weren't shown — comes from the `map_architecture` operation; this template only fixes the sections.

All Mermaid diagrams must follow the rules in `Templates/Prompts/mermaid_rules.md` (pure-ASCII node IDs with CJK only inside quoted labels; per-kind math policy; `sequenceDiagram` message text carries no `$$` math). Do not restate those rules — just obey them.

## 系統概觀
What this system/module does and where its boundaries are — a couple of plain paragraphs a newcomer could read first.

## 模組地圖
A Mermaid `flowchart` of the modules and their dependencies. Every node here must also appear by name in the prose sections; do not draw a box you can't describe.

## 關鍵流程
One or two of the main data/control flows, as a `flowchart` or `sequenceDiagram`. Choose the primary paths, not every branch.

## 狀態機
If a component is genuinely state-driven, a `stateDiagram-v2` of its states and transitions. If nothing here is stateful, write "本系統無明顯狀態機" and move on — do not invent one.

## 依賴與邊界
External dependencies, configuration sources, and the IO/security boundaries (what the system reads, writes, or trusts).

## 風險與建議
Architecture-level observations: coupling worth loosening, boundaries worth hardening, or places the map revealed a surprise. Ground each in something visible in the code; say "can't tell from here" where the input didn't show enough.
