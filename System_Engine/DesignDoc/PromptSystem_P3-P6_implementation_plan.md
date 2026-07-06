# Prompt 系統改善 P3–P6 實作簡報

> 執行者:Opus(依本簡報實作)。審查者:Claude(最多兩輪 review,之後接手)。
> 前情:P1(mermaid 規則雙源統一)+ P2(prompt 資產 lint)已完成於 commit `9556b26`
> (branch `fix/prompt-system-p1-p2`)。本簡報只涵蓋 P3–P6。

---

## 0. 背景:三條 Prompt 路徑(必讀)

| 路徑 | 組裝方式 | 進入點 | 使用者 |
|---|---|---|---|
| **A. PromptComposer** | 語言 banner(前)→ Persona → Operation → Template → Visualization → 共通規則+語言重申(後) | `services/llm/prompt_composer.py` `build_system_prompt()`(82-177 行) | `answer_query()`、entity page、合成 |
| **B. BaseAgent 檔案載入** | 從 `lings-desktop/Templates/Prompts/*.md` 載檔;**缺檔靜默回傳 `""` 或降級 hardcoded fallback** | `agents/base_agent.py` `_load_prompt()`(83-92 行) | @ling 命令代理(recall、counter、merge、linter…) |
| **C. Lean mode** | 呼叫端自帶 hardcoded sys prompt,**繞過所有疊加(含語言 banner)** | `services/llm_client.py` `complete()`(428-454 行) | 15+ 個 stage(見 P4 的表) |

目錄常數:`core/config.py:239-254`(`SCRIPTURE_FILE`、`PERSONAS_DIR`、`PROFILES_DIR`、`TEMPLATES_DIR`、`PROMPTS_DIR`、`OPERATIONS_DIR`、`GUIDELINES_DIR`)。

## 0.1 通用約束(每個 phase 都適用)

1. **一個 phase 一個 branch**:`prompt/p3-fallback`、`prompt/p4-lean-lang`、`prompt/p5-cleanup`、`prompt/p6-docs`。每個 phase 完成後停下等 review,不要往下做。
2. **驗收門檻**:`make check` 全綠(lint + typecheck + test-fast,P2 的 `test_prompt_assets.py` 已在其中)。pre-commit 的 ruff format 可能重排你的檔——commit 被 hook 打斷時重新 `git add` 再 commit 一次即可。
3. **mypy 豁免清單只減不增**(pyproject 內)。
4. **不新增 .env 知識**:行為開關一律走 Scripture(DynamicSettings);本簡報的工作原則上不需要新開關,若你認為需要,先停下來提出。
5. **`lings-desktop/pages/` 是 gitignored 的生成內容**,不要碰。其他 vault 目錄(Scripture/、Templates/)在版控內,可以改。
6. **不要把控制面 hardcoded prompt 搬進 vault**(JSON 萃取、self_improve 的 find/replace 等)——這是明確的架構決定,理由:避免檔案/程式雙源分歧(F3 型問題)。邊界=「內容/風格類進檔案、控制類留 code」。

---

## P3:Fallback 防護(小)

**問題(F3)**:路徑 B 缺檔時靜默降級。`base_agent.py:88-90` 只 `logging.warning` 後回傳 `""`;`recall_agent.py:92` 是 `self._load_prompt("agent_recall") or _FALLBACK_SYSTEM_PROMPT`——檔案版被刪/改名後,行為無聲切到 code 內的舊 fallback,無人察覺。

**改動**:

1. `agents/base_agent.py` `_load_prompt()`:新增 keyword 參數 `required: bool = False`。`required=True` 且檔不存在時,warning 升級為 `logging.error`,訊息含「將使用 hardcoded fallback 或空 prompt,行為可能偏移」字樣。回傳值不變(仍 `""`,不拋例外——agent 不能因缺檔而 crash,fail-open 是既有原則)。
2. `recall_agent.py:92` 改為 `self._load_prompt("agent_recall", required=True) or _FALLBACK_SYSTEM_PROMPT`,並在真的走到 fallback 那條路時,把 `used_fallback_prompt: true` 記進該次執行的 stats/trace metadata(`self.stats` dict 就有了,跟著現有 `input_chars` 的寫法)。
3. 掃其他 agent 的 `_load_prompt` 呼叫點(merge、insight、linter、tag_patrol、counter),對「有 hardcoded fallback」或「缺檔會嚴重劣化」的呼叫點加 `required=True`。純選配的 prompt 不加。
4. `maintenance/health_check.py:15-22` 的 `required_prompts` 清單補齊為與 `tests/test_prompt_assets.py::test_required_agent_prompts_exist` **同一份**(8 檔:system_base、mermaid_rules、agent_counter、agent_insight、agent_linter、agent_merge、agent_recall、agent_tag_patrol)。兩處清單旁互加註記(「另一份在 XXX,同步改」)。

**測試**(加進 `tests/`,可新檔 `test_prompt_fallback.py`):
- `_load_prompt(required=True)` 對不存在的檔:回傳 `""`、log level 是 ERROR(用 `caplog`)。
- `_load_prompt(required=False)` 維持 WARNING(回歸保護)。
- recall 走 fallback 時 stats 有 `used_fallback_prompt`(monkeypatch `PROMPTS_DIR` 指向空 tmp dir;RecallAgent 建構需要 llm——用最小 stub/mock,參考既有 agent 測試怎麼 stub)。

**驗收**:make check 全綠;手動刪掉 agent_recall.md 跑 recall 會看到 ERROR log + stats 標記(在 PR 描述貼證據,測完還原)。

---

## P4:Lean mode 語言審計(中)

**問題(F2)**:路徑 C 沒有語言 banner,輸出語言全靠各 stage 的 sys prompt 恰好是中文。

**⚠️ 關鍵陷阱,先讀**:`learning_artifacts.py` 的 `_LANG_MATCH_RULE`(89-92 行)是**刻意**「跟內容語言」而非「跟 OUTPUT_LANGUAGE」——英文筆記該得到英文圖表。所以 **`artifact_*` 系列 stage 絕對不要掛 OUTPUT_LANGUAGE banner**,它們已有正確的(不同的)語言政策。P4 不是無腦全掛,是逐 stage 審計。

**改動**:

1. **工具**:在 `services/llm/prompt_composer.py` 把 `build_system_prompt()` 內 149-170 行的 lang_banner 字串抽成模組層函式 `language_banner() -> str`(重用現有 `lang_hint()`),`build_system_prompt()` 改呼叫它——行為必須 byte-identical(有既有測試就靠它們守,沒有就補一個 snapshot 測試)。
2. `services/llm_client.py` `complete()` 新增 keyword-only 參數 `pin_language: bool = False`;True 時把 `language_banner()` 前置到 system_prompt(中間空一行)。預設 False = 所有既有呼叫點 byte-identical。
3. **審計**:對下列每個 stage 填一張表(進 PR 描述與 P6 文件):stage / 呼叫檔:行 / 輸出落點(vault 使用者可見?內部 JSON?)/ sys prompt 現在的語言 / 建議(pin / 不 pin / 已有內容語言規則)。stage 清單:`cortex_recall`、`artifact_classify`、`artifact_table`、`artifact_{kind}`、`argument_map`、`self_diagnosis`、`self_improve_edits`、`research_keywords`、`elite_digest`、`patent_table`、`extract_claims`、`generate_structured`、`adjudicate_claims`、counter_agent 的 LingLens extraction(314-318)與 tally(375-379)、`complete`(預設值,查有沒有裸用)。
4. **保守翻開**:只對「輸出使用者可見 且 目前無任何語言保障 且 應跟 OUTPUT_LANGUAGE」的 stage 傳 `pin_language=True`。預期只有少數(candidate:`argument_map`、`elite_digest`——以審計實查為準)。內部 JSON 控制類(classify、self_diagnosis、self_improve_edits、extract/adjudicate_claims、keywords、tally)一律不 pin(banner 會污染嚴格 JSON prompt)。拿不準的:表上標「不確定」留給 review,不要自行 pin。

**測試**:
- `complete(pin_language=True)` 的最終 system prompt 以 banner 開頭;False 時完全不含 banner(mock provider 層攔截實際送出的 prompt,參考既有 llm_client 測試的 stub 方式)。
- `build_system_prompt()` 重構後輸出不變。

**驗收**:make check 全綠;PR 附完整審計表;被 pin 的 stage 逐一列出理由。

---

## P5:資產清理(小,但「先驗證再動手」)

**問題(F6)**:檔案層掃描顯示 2 個 persona(assistant、coder)與 ~12 個 template 疑似無引用;DocType.md 已標 DEPRECATED(2026-06-10);Profiles `operations:` 欄位未被消費(F7,Phase 6 Planner 保留)。

**⚠️ 已知假陽性,示範為何必須重驗**:檔案層掃描曾把 `translation-rpt` 列為 unused,但它是 `Scripture.md` 的 `use_template` 預設值。檔案層看不到 code 端與 Scripture 端的字串引用。

**程序(每個候選逐一過,結果表進 PR)**:

1. 建候選清單:persona `assistant`、`coder`;template 以檔案層掃描的 unused 清單為起點(tech-rpt、insight-rpt、wiki-note、sw-inv-disclosure-rpt 等;review-track 4 個屬 `_pending`,**排除**,它們等待啟用)。
2. 對每個候選名 `X` 依序查,任一命中即**保留**:
   a. `grep -rn "X" System_Engine --include="*.py"`(含測試);
   b. `grep -rn "X" lings-desktop --include="*.md"`(Scripture、Profiles、@ling 命令、**且包含 gitignored 的本地內容**:`pages/` 檔案的 frontmatter 可能有 `synthesis_template: X`——用 `grep -rl "synthesis_template: X" lings-desktop/pages/` 查);
   c. `git log --oneline -5 -- <該檔>` 看近期是否活躍。
3. 確認無引用的:**移到 `lings-desktop/Templates/_archive/`**(persona 移 `lings-desktop/Scripture/Personas/_archive/`),不要刪。語言變體(.zh/.ja)跟 base **一起移**,否則 P2 lint 的孤兒變體檢查會 fail——這是預期中的守門,不要改 lint 來遷就。
4. `DocType.md`:先 `grep -rn "DocType" System_Engine lings-desktop --include="*.py" --include="*.md"` 確認無程式讀取,然後 `git rm`。
5. `Scripture/Profiles/_README.md` 加一小節:「`operations:` 欄位為 Phase 6 Planner 保留,目前由系統定義但不消費;引用完整性由 test_prompt_assets.py 檢查」。

**驗收**:make check 全綠(尤其 `test_prompt_assets.py` 必須仍全過——archive 後若 Scripture/Profile 引用斷了,代表你歸檔錯了東西);PR 附「候選 → 查證結果 → 處置」全表。

---

## P6:架構文件化(小)

**產出**:新檔 `System_Engine/DesignDoc/PromptSystem_architecture.md`,內容:

1. **三路徑地圖**:本簡報 §0 的表為骨架,補上你在 P3/P4 實作後的最新事實(檔案:行號要重新核對,不要照抄本簡報——P3/P4 會移動行號)。
2. **Stage 目錄表**:P4 審計表的最終版(stage / 呼叫點 / 路徑 A|B|C / prompt 真相源檔案或常數 / 語言保障方式 / 輸出落點)。
3. **政策同步點清單**:`math-policy: katex-v2` 的三處(vault mermaid_rules.md、mermaid_repair.py、learning_artifacts.py)+ 哨兵測試;required_prompts 的兩處(health_check、test_prompt_assets)。
4. **邊界原則**:「內容/風格類 prompt 進 vault 檔案(熱編輯);控制類 prompt 留 code(與測試耦合)」+ 理由(F3 型分歧)。
5. **已知未做**:F7 operations 欄位(Phase 6)、lean mode 未 pin 的 stage 與理由(從 P4 表帶過來)。

寫完後在 `Profiles/_README.md`(若 P5 未涵蓋)與 `prompt_composer.py` 模組 docstring 各加一行指向這份文件。

**驗收**:文件內所有檔案:行號錨點實際存在(抽查即可);make check 全綠(docs-only,理論上不影響)。

---

## 執行順序與 review 節奏

P3 → P4 → P5 → P6,嚴格依序(P6 消費 P4 的審計表)。每個 phase:開 branch → 實作 → `make check` 全綠 → commit(訊息含 phase 編號)→ 停下等 review。Review 最多兩輪,之後由審查者接手收尾。

## 完成定義(整體)

- [ ] P3:缺檔告警升級 + fallback 使用可觀測 + health_check/測試清單同步
- [ ] P4:`pin_language` 機制 + 完整 stage 審計表 + 保守翻開
- [ ] P5:查證表 + 歸檔(非刪除)+ DocType.md 移除 + _README 補註
- [ ] P6:PromptSystem_architecture.md + 交叉指標
- [ ] 全程 make check 綠、P2 lint 未被削弱(不得為遷就清理而放寬測試)
