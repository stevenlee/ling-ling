# Coder Persona C1–C3 實作簡報

> 執行者:Opus(依本簡報實作)。審查者:Claude(每 phase 最多兩輪 review,之後接手)。
> 願景(使用者原話):讓 coder persona「能夠做 code review、寫系統架構、流程圖、狀態圖等等」。
> 使用者已拍板的三個決策:
> 1. **讀取邊界:vault-only 維持**——agent 不得讀 vault 以外的檔案。
> 2. **範圍:C1+C2+C3 完整三期**(C4 ingestion profile 留下輪)。
> 3. **驗收對象:ling-ling 自身 repo(dogfood)**。
>
> 相關文件:`PromptSystem_architecture.md`(三路徑地圖——本簡報大量引用其結論)、
> `PromptSystem_P3-P6_implementation_plan.md`(委派節奏的前例)。

---

## 0. 核心設計:vault-only × dogfood 的解法 = pack-code 橋

「agent 只能讀 vault」與「要 review ling-ling 自己的原始碼」的矛盾,由一個
**使用者主動執行的打包工具**解開:

```
使用者執行:make pack-code SRC=System_Engine/services/identifier_guard.py
    → 產生 lings-desktop/CodeReview/identifier_guard.py.md(fenced code + metadata)
使用者在筆記寫:@ling-code-review [[identifier_guard.py]]
    → CodeReviewAgent 讀「vault 內的打包筆記」→ 報告進 fromLingLing/
```

原則:**搬程式碼進 vault 的是使用者執行的確定性 CLI,不是 agent**。agent 端的
vault-only 邊界一寸不破。packer 讀 repo 檔案、只寫進 vault、絕不執行任何程式碼。

## 0.1 通用約束(每個 phase 都適用)

1. **一期一 branch**:`coder/c1-foundation` → `coder/c2-code-review` → `coder/c3-architect`,
   嚴格依序 stack,每期完成後停下等 review,不要往下做。
2. **驗收門檻**:`make check` 全綠。pre-commit ruff format 打斷 commit 時,重新 add 再 commit。
3. **mypy 豁免清單只減不增;不新增 .env 知識**(行為開關走 Scripture,但本簡報的工作
   預設不需要新開關——想加開關前先停下來提出)。
4. **Persona 美學(硬條款)**:Ling-Ling 是日系女高中生/鄰家好學生聲音。軟 emoji
   限定(🥀🌼🌸🌱🔔💦💧📓🍵🧹🎀🌷),**即使主題是 code/安全也絕不用**
   🚨🔴❌🧠🛡️⚔️🤖💻⚡🔥。reviewer.md 已有完整範例段落,照抄其紀律。
5. **弱模型教訓(gemma4:26b)**:一 chunk 一 call,絕不合併呼叫(A2 合併實驗已證明失敗);
   結構化輸出走 `_complete_json`;**發現(finding)以識別符錨定,不以行號錨定**
   ——26B 會捏造行號,函式/類別名可被 identifier_guard 校正,行號不能。
6. **不建第四份 mermaid 政策**:圖表規則的 LLM-facing 真相源是
   `Templates/Prompts/mermaid_rules.md`(math-policy: katex-v2),template 裡只引用、
   不複述;產出靠 BaseAgent._self_correct + run_markdown_quality_checks 自癒。
7. **語言**:報告一律走 path A(answer_query),語言三明治自動保證 OUTPUT_LANGUAGE;
   lean JSON 萃取不掛語言 banner(見 PromptSystem_architecture.md §2)。

---

## C1:地基(persona 重寫 + templates + operations)

### C1.1 重寫 `lings-desktop/Scripture/Personas/coder.md`

現況是初版遺留(英文、無 Ling-Ling 聲音、只講「把 code snippet 轉 wiki 頁」)。
以 **reviewer.md 為樣式範本**(同目錄)整個重寫:

- 結構:`## 🎯 Role` / `## ✨ Core Traits` / `## 🗣️ Voice` / `## 📜 Guidelines & Best Practices`。
- Role:Ling-Ling 戴上工程師帽——讀程式碼、誠實指出問題、畫得出架構、教得會人。
  忠於**程式碼的讀者與維護者**,不是作者的面子。
- Core Traits 建議(可微調,精神不可變):
  * 溫暖但不奉承——爛就說爛,但說得出「為什麼」與「怎麼改」。
  * 具體到識別符——每個發現都錨定到函式/類別/模組名,絕不空泛。
  * 學習優先——review 完讀者要知道這段 code 教了什麼、下一步改哪。
  * 少而準——寧可三個高信心發現,不要十個湊數的(呼應系統既有 review 品味)。
  * 結構化技術寫作——保留舊 persona 的長處(架構概觀/元件/範例/注意事項的組織力)。
- Voice:沿用 reviewer.md 的軟 emoji 紀律段落(含禁用清單,一字不漏的精神)。
- Guidelines 收尾必含 reviewer.md 的「Stay in voice, defer the structure」精神:
  Template 管章節、operation 管方法論、persona 只供聲音與判斷力。
- 語言變體:先不做 .zh/.ja(實作時檢查 reviewer.md 是否有變體,跟它一致即可;
  path A 的語言三明治已保證輸出語言)。

### C1.2 新 template `lings-desktop/Templates/code-review-rpt.md`

格式範本參考同目錄 `tech-rpt.md`(YAML frontmatter 區塊 + body 章節)。章節:

1. `# Code Review 報告`(YAML: title / tags: ["code-review"] / type: "code-review")
2. `## 總評` — verdict 先行:整體品質一句話 + 最重要的 1-3 件事。
3. `## 發現` — 每條:**嚴重度(💧 需修 / 🌱 建議 / 🍵 見仁見智)**、
   位置(`檔名 → 函式/類別名`)、問題描述、code 摘錄(fenced)、建議修法。
4. `## 值得學的地方` — 這份 code 做得好的模式(學習優先,不是客套)。
5. `## 下一步` — 排序過的行動清單。

規則寫進 template:發現按嚴重度排序;每條必有識別符錨點;無發現時誠實說
「沒有值得提的問題」而不是硬湊。

### C1.3 新 template `lings-desktop/Templates/architecture-rpt.md`

1. `## 系統概觀` — 這個模組/系統做什麼、邊界在哪。
2. `## 模組地圖` — mermaid flowchart(模組間依賴;引用 mermaid_rules.md 的
   ID/引號紀律:純英文 ID、中文進 label)。
3. `## 關鍵流程` — 1-2 條主要資料流(flowchart 或 sequenceDiagram;
   注意 sequenceDiagram 訊息文字不可含 `$$` math——政策見 mermaid_rules.md)。
4. `## 狀態機` — 若有狀態性元件,stateDiagram-v2(沒有就明說「無狀態機」,不硬畫)。
5. `## 依賴與邊界` — 外部依賴、設定來源、安全/IO 邊界。
6. `## 風險與建議` — 架構層級的觀察。

### C1.4 兩個 operation(`lings-desktop/Templates/Operations/`)

CapabilityManager 會自動掃描註冊(檔名 stem = 正式 ID);frontmatter 格式照
既有 operations(type/description/expected_inputs/produces/cost_class)。

**`review_code.md`**:方法論——
- 逐項 checklist:正確性、錯誤處理、邊界條件、資源管理、可讀性、測試覆蓋、基本安全面。
- 嚴重度定義(💧=會出錯或誤導維護者;🌱=更好的寫法;🍵=風格偏好)。
- 硬規則:只引用真實存在的識別符;不確定就標「需人工確認」;不重寫整檔;
  引用的 code 摘錄必須逐字來自輸入(不改寫)。
- Non-Goals:不評高層需求對錯、不做效能猜測(沒證據時)。

**`map_architecture.md`**:方法論——
- 先列元件清單(從輸入的 import/結構事實),再畫關係,最後才寫敘述。
- 圖與文字必須一致(圖裡的每個節點,文中都要出現)。
- 承認不知道:輸入沒給的模組不畫、不猜。

### C1 驗收

- `make check` 全綠(`test_prompt_assets.py` 的引用檢查天然涵蓋新檔——operations
  目前無 profile 引用它們,不會觸發;但孤兒變體/格式檢查會跑)。
- persona/template/operation 檔案齊備,美學條款(禁用 emoji 清單)出現在 coder.md。
- PR 描述附 coder.md 全文供 review。

---

## C2:pack-code 橋 + @ling-code-review

### C2.1 打包工具 `System_Engine/tools/pack_code.py` + make target

- CLI:`python System_Engine/tools/pack_code.py <SRC>... [--dest CodeReview] [--title 名稱]`
  (SRC 是**相對 repo root** 的檔案或目錄;目錄時遞迴收 `.py`,其他副檔名先不收)。
- Makefile 加:`make pack-code SRC=path [TITLE=名稱]`。
- 輸出:`lings-desktop/CodeReview/<sanitize 後標題>.md`,內容:
  - frontmatter:`type: packed-code`、source 路徑清單、git commit(`git rev-parse --short HEAD`)、
    打包時間、檔案數/總行數。
  - body:每檔一節 `## path/to/file.py`,fenced ```python 區塊(逐字,不省略)。
- 防護:單檔 >200KB 拒收(報錯);總量 >1MB 拒收;絕不執行任何被打包的程式碼;
  檔名經 `core/vault_utils.sanitize_filename()`(既有統一入口)。
- **確定性、零 LLM**。
- 附帶產出識別符清單:打包時用 Python `ast` 抽出所有 function/class 名,寫進
  frontmatter 的 `identifiers:` 清單——C2.3 的 identifier_guard 直接吃這份,
  不必 review 時再解析。

### C2.2 `agents/code_review_agent.py`(繼承 BaseAgent)

指令形:`@ling-code-review [[打包筆記標題]]`(wikilink 解析照抄
`visualize_agent.py` L19-27 的 `_WIKILINK_RE` 模式;讀筆記照 `_load_note` 的
vault-only 模式,額外支援 `CodeReview/` 目錄)。

流程(map → reduce → report):
1. **切分**:按 `## 檔案` 節切,一檔一 chunk(超長檔再按函式邊界對半切,
   切點寧粗勿細)。
2. **Map(lean JSON,path C)**:每 chunk 一次 `_complete_json(kind="array")`,
   system prompt 內嵌 review_code 方法論的濃縮版(control-prompt 留 code,
   見 PromptSystem_architecture.md §4 邊界原則),產出 findings:
   `{file, anchor(函式/類別名), severity(high|med|low), category, claim, excerpt, suggestion}`。
   stage 命名:`code_review_extract`。
3. **Reduce(確定性優先)**:同 `(file, anchor, category)` 去重合併;數量太多時
   按 severity 截前 N(N=20,常數即可)。不用 LLM tally(除非 review 時發現必要,
   先不做)。
4. **Report(path A)**:`llm.answer_query(指令文, wiki_context=findings 的結構化摘要+關鍵摘錄,
   persona="coder", operation="review_code", forced_template="code-review-rpt")`
   ——這是整個架構的正確組合方式,也是 persona×operation×template 三軸的 dogfood。
   (實作時核對 `answer_query` 的參數簽名,見 `services/llm_client.py` ~L350-426。)
5. **identifier_guard**:用 frontmatter 的 `identifiers:` 清單呼叫既有
   `correct_identifiers(body, canon)`(用法照抄 `agents/review_agent.py` L107-112),
   把模型改壞的函式/類別名 snap 回正確拼寫。
6. `_write_report(標題, body, "code-review", meta)` → fromLingLing/。

### C2.3 命令註冊(4 觸點 + 一個命名陷阱)

1. `agents/registry.py`(~L22-49):`"code-review": CodeReviewAgent`。
2. `services/command_dispatcher.py`(~L39-80)`INTENT_ROUTES` 加三元組;L25 區 import。
3. `lings-desktop/toranomaki/@ling-code-review.md`:使用說明(照既有 30 個命令文件的體例,
   含 pack-code 工作流示範)。
4. **⚠️ 命名陷阱(必查)**:確認既有 `review` 命令的匹配不會吃掉 `code-review`
   (dispatcher/watcher 的匹配語意要現場驗證;系統已有 `@ling-research(?!-)` 負向
   lookahead 前例,見 `watchers/vault_watcher.py` L196-203。若匹配是 substring 式,
   `code-review` 要排在 `review` 之前或用同款 lookahead)。

### C2.4 檢核點(實作時必須現場驗證,結果寫進 PR)

- **`CodeReview/` 目錄與 daemon 的互動**:打包筆記會不會被 ingestion 誤吃、
  會不會進 RAG index?查 VaultWatcher 的觸發範圍與 RAG 索引範圍。若會誤觸,
  選最小干預的排除法(排除目錄或 frontmatter type 過濾),把證據與選擇寫進 PR。
- packer 對 symlink 的行為(不得跟出 repo 外)。

### C2.5 測試

- packer:tmp repo → 打包 → frontmatter/識別符/大小上限/sanitize 斷言。
- agent:FakeLLM(照 `test_cortex_recall.py` 的 stub 模式)——chunk 切分、
  findings 去重、answer_query 收到 persona="coder"/operation="review_code"/
  forced_template="code-review-rpt" 的斷言、identifier_guard 生效斷言
  (餵一個故意拼錯的函式名,斷言被 snap 回來)。
- dispatcher:`@ling-code-review` 路由到新 agent、且 `@ling-review` 不受影響(反向也是)。

### C2 驗收(dogfood)

`make pack-code SRC=System_Engine/services/identifier_guard.py` →
`@ling-code-review [[identifier_guard.py]]` → fromLingLing/ 出現報告:
繁中、軟 emoji、發現有識別符錨點、無捏造識別符。(需 daemon,由使用者手動跑;
PR 附一次真實輸出全文。)`make check` 全綠。

---

## C3:@ling-architect(架構文件 + 流程/狀態圖)

### C3.1 `agents/architect_agent.py`

指令形:`@ling-architect [[打包筆記標題]]`(輸入同 C2 的打包筆記;也接受一般
vault 技術筆記——內容不是 packed-code 時跳過 C3.2 直接走 C3.3)。

### C3.2 確定性前置掃描(packed-code 輸入時)

用 `ast` 從打包筆記的 code 區塊抽**事實**,餵給 LLM 當結構化 context:
- 模組清單、每模組的 import(區分內部/外部)、頂層 class/function 清單。
- 這一步零 LLM——讓 26B 畫圖時「抄事實」而不是「猜結構」,是圖正確性的關鍵。

### C3.3 產報告(path A)

`answer_query(..., persona="coder", operation="map_architecture",
forced_template="architecture-rpt")`,wiki_context = 前置掃描的事實表 + code 摘錄。
輸出經 BaseAgent._self_correct 自癒(mermaid repair 管線這輪已大幅硬化,直接受益),
`_write_report(..., "architecture", ...)` → fromLingLing/。

### C3.4 測試與驗收

- 測試:ast 掃描器(tmp 檔案→import/類別清單斷言)、FakeLLM 下 persona/operation/
  template 三參數斷言、命令路由。
- **mermaid 驗收(硬門檻)**:對 dogfood 產出的報告跑
  `node scripts/validate_mermaid.mjs <報告檔>` → 0 fail,結果貼 PR。
- dogfood 場景:`make pack-code SRC="System_Engine/agents/registry.py System_Engine/services/command_dispatcher.py" TITLE=command-routing`
  (CLI 本身接受多個 SRC 引數;make target 用引號傳遞)→ `@ling-architect [[command-routing]]` →
  架構報告含模組地圖 + 至少一張流程圖;圖中節點與文字敘述一致。

---

## 執行順序與 review 節奏

C1 → C2 → C3,嚴格依序(C2 消費 C1 的 persona/template/operation;C3 消費 C2 的
packer 與打包格式)。每期:開 branch → 實作 → `make check` 全綠 → commit(訊息含
期別)→ 停下等 review。最多兩輪 review,之後審查者接手。

## 完成定義(整體)

- [ ] C1:coder.md 重寫(Ling-Ling 聲音+美學硬條款)+ 兩 template + 兩 operation
- [ ] C2:pack_code 工具(+make target)+ CodeReviewAgent + 命令註冊(含命名陷阱驗證)
      + identifier_guard 接線 + CodeReview/ 目錄互動檢核 + dogfood 報告
- [ ] C3:ArchitectAgent + ast 前置掃描 + dogfood 架構報告(mermaid 0 fail)
- [ ] 全程 make check 綠;agent 端 vault-only 邊界零破口;不新建 mermaid 政策副本
- [ ] 三份 toranomaki 命令文件(code-review、architect;pack-code 用法附在前者)
