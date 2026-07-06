# 🔔 @ling-architect 範例指令

讓 Ling-Ling 戴上工程師帽,幫你把一個系統或模組**畫成架構圖**——系統概觀、模組地圖、
關鍵流程、狀態機、依賴與邊界、風險。圖與文字保證一致,看不到的部分會明說,不硬猜。

> [!IMPORTANT]
> 跟 `@ling-code-review` 一樣是 **vault-only**:先用 `make pack-code` 把原始碼打包進
> vault,再發動。打包這步是你自己執行的確定性 CLI(零 LLM、不執行程式碼)。

---

## 工作流

### 步驟 1:打包(通常打包一組相關檔案)
```bash
make pack-code SRC="System_Engine/agents/registry.py System_Engine/services/command_dispatcher.py" TITLE=command-routing
```

### 步驟 2:發動測繪
在 `toLingLing/` 建檔,寫:
```markdown
@ling-architect [[command-routing]]
```
報告寫入 `fromLingLing/`。斜線寫法 `/architect [[名稱]]` 亦可。

---

> [!TIP]
> 發動前系統會用 `ast` 抽出**結構事實**(模組、類別/函式、內部/外部依賴)餵給模型,
> 讓它「照事實作圖」而不是憑空想像——這是圖正確性的關鍵。
> 產出的 Mermaid 圖走的是硬化過的修復管線,可用 `make validate-mermaid MMD=<報告檔>` 驗。
> 也接受一般 vault 筆記(非打包)當輸入,只是少了 ast 事實這層 grounding。
