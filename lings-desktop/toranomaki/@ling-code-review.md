# 🔔 @ling-code-review 範例指令

讓 Ling-Ling 戴上工程師帽,幫你 review 一段程式碼——誠實指出會出錯或誤導維護者的地方、
建議更好的寫法,並點出寫得好的部分。每個發現都錨定到**函式／類別名**(不用會漂移的行號)。

> [!IMPORTANT]
> **agent 只讀 vault**,不會去讀你電腦上的原始碼。要先用打包工具把程式碼搬進 vault:
> 這一步是**你自己執行的確定性 CLI**(零 LLM、不執行任何被打包的程式碼)。

---

## 工作流

### 步驟 1:打包程式碼進 vault
```bash
make pack-code SRC=System_Engine/services/identifier_guard.py
```
會在 `lings-desktop/CodeReview/identifier_guard.md` 產生一份打包筆記
(fenced code + 用 `ast` 抽出的識別符清單;檔名取自來源檔 stem)。多檔或整個目錄也可以:
```bash
make pack-code SRC="System_Engine/agents/registry.py System_Engine/services/command_dispatcher.py" TITLE=command-routing
```
(目錄會遞迴收 `*.py`;單檔上限 200KB、總量上限 1MB。)

### 步驟 2:發動 review
在 `toLingLing/` 建一個檔案,寫:
```markdown
@ling-code-review [[identifier_guard]]
```
報告寫入 `fromLingLing/`——總評、依嚴重度排序的發現(💧 需修 / 🌱 建議 / 🍵 見仁見智)、
值得學的地方、下一步。斜線寫法 `/code-review [[名稱]]` 亦可。

---

> [!TIP]
> `CodeReview/` 是 vault 裡的獨立資料夾,**不會被 ingestion 或 RAG 索引**,
> 所以打包的程式碼不會污染你的知識庫。review 完可自行刪除打包筆記。
