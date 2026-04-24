## 視覺化規範
- **Mermaid 圖表**：始終包含一個 `mermaid` 圖表（如 `graph TD` 或 `sequenceDiagram`）來解釋內容邏輯。
- **圖表標籤**：圖表內的標籤請使用繁體中文。
- **位置**：圖表應放在頁面底部或相關章節之後。
- **至關重要：引用規則**
	- **所有**標籤與名稱（包括節點「Nodes」與子圖「Subgraphs」）**必須**包含在雙引號之內。
	  - ✅ 正確: A["Concept (Detail)"]
	  - ✅ 正確: subgraph "Logic Flow Layer"
	  - ✅ 正確: root(("Main Topic"))
