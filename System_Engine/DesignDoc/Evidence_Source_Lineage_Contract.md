# A04：原始文件來源鏈與補證資格

## 判定範圍

`services.evidence_lineage.SourceLineageResolver` 只讀取 vault 檔案，不呼叫 LLM、不建構
RAG／Chroma、不寫回舊文件。`independent` 指**可完整追溯且不重疊的已記錄原始文件**，
不代表不同作者、不同實驗或不存在轉載／引文依賴；沒有資料就不聲稱知道。

每次 compare 保存 candidate／origin 兩個 closure：roots、families、claims、nodes、
reasons、complete。來源關係為以下三種，順序不可交換：

| status | 條件 | A2 行為 |
|---|---|---|
| shared | 任一已知 root、文件 family 或 grounded claim 有交集，即使其餘來源不完整 | 檢索候選直接排除，不送模型，不新增票數 |
| independent | 兩邊皆有 roots、皆無缺口，且上述集合皆不重疊 | 才能與 A02 有效判定一起取得 apply 資格 |
| unknown | 無已知交集，但至少一邊不完整 | 可判讀、稽核、留報告；不新增 evidence／counterpoint，不強化 |

不同 display title 或不同 generated page 路徑本身都不是原始證據；同標題的不同原文
也不能合併成同一個 evidence。判讀語義與來源資格分開：full_support 並不自動代表
independent，contradicts 也不繞過來源 gate。A01 真模型語義評估仍是另一個驗收。

## 追溯與同源識別

- 候選必須有可在 vault 內唯一定位的來源路徑，且實際送審的 passage prefix 必須存在於
  當次來源檔案 snapshot；不把舊索引片段配上新版來源 metadata 來套用。
- 解析全部 related_docs／related_titles／target_titles／sources 欄位，不沿 consolidation
  舊的「第一個非空欄、最多五項」裁切。所有明示依賴都必須成功解析，不能只挑可讀的。
- `evidence.insight` 在 Insights 命名空間解析；同名 raw 檔不能冒充已遺失的 insight。
  來源引用可為 vault 相對／絕對路徑或唯一 basename；重名、明示路徑不存在、越出 vault
  的 symlink 都是未知，不回退到其他同名檔案。任意正文 wikilink 不自動當成完整 provenance。
- 原始文件的 roots 包含 raw 相對路徑、剝除 frontmatter 後的完整 body SHA-256，以及
  metadata 明示的有效 HTTP(S) url／source_url／source。相同 body 即使改名或 metadata
  不同仍共用 root；同 URL 即使內容修訂也共用 root。URL 只標準化 scheme／host 與 fragment，
  保留 query，不猜測 redirect、tracking alias、翻譯／轉載關係。僅有 URL、沒有可追溯原文
  snapshot 的引用，可提示同源，但不能作獨立性的正面證明。
- 已知 ingestion layout 的 `pages/<base>/<base> (Part N|Stitched|Synthesis).md`
  共用 document family。唯一同名 raw 檔只提供可能同源的負面提示；沒有明確來源引用
  仍 unknown。歸檔撞名的 `_YYYYMMDD-HHMMSS` 變體也納入歧義檢查，不誤認第一份為唯一原文。
- grounded_on 沿 claim_id 查 Cortex metadata，不假設人類可讀的檔名等於 claim_id；
  duplicate claim_id、缺檔、循環與 schema 錯誤都保留缺口。來源 closure 只消費
  active_evidence；已 superseded 的紀錄不污染目前 support 的來源集合。
- 新 evidence 持久化的 source_lineage 可保留歷史 candidate roots，原始來源後續移除或
  display title 改變不會把既有 provenance 抹掉；壞版本或 closure 不一致則 unknown。
  不為舊 evidence 自動補來源、不把舊 supports 當作新契約合格紀錄。

來源檔案每 batch 只讀一次；apply 以全新 resolver 重讀，再於 index intent 準備後重驗。
來源 metadata、檔案內容或 origin closure 改變即拒絕該 claim 的套用。既有 Cortex raw
basis 檢查、audit write-ahead 與 index repair commit 語義保留；沒有新增長駐 worker。
這仍是 optimistic 檢查，沒有宣稱能禁止不遵守 busy lock 的外部編輯器在最後瞬間競寫。

## 稽核、身份與相容性

新 audit source 附 lineage v1；A03 record schema_version 仍為 1，舊 audit 沒有 lineage
時仍可唯讀驗證，但不能因此取得新 apply 資格。nodes 保存相對路徑、snapshot hash、
refs 與 grounded_on；完整判讀 passage／prompt／claim basis 沿 A03 私有 snapshot 保存。
來源缺口、shared 排除及候選同源去重數出現在報告；trace artifact 附 lineage_status。

檢索與 apply 均排除候選間已知相同 roots；一批找到兩個獨立原文可新增兩筆 evidence，
整條 claim 仍只 reinforce 一次。marker 保留 `[EvidenceTrace]` 與 display title，另帶
`[origin:<roots 的 SHA-256>]`，counterpoint 也用這個鍵去重；不再按 title 合併不同原文。
已知 shared 的既有 evidence 不刷新，保留其歷史摘要與 audit，刪除舊刷新分支；返回值的
refreshed 固定為 0 以保留呼叫端欄位相容。舊 marker／counterpoint 不自動升級或重寫。

`EVIDENCE_LINEAGE_MAX_DEPTH`（預設 8）、`EVIDENCE_LINEAGE_MAX_NODES`（預設 128）由
config 讀 env。第二個上限涵蓋 traversal visits 與 refs／evidence fanout，不只不同檔案數，
避免 diamond/cyclic graph 在少數檔案間重複展開。到限額仍 unknown，不略過欠缺部分來放行。
檔名 inventory 與 grounded claim_id 索引為一次性的唯讀查找，不是持久化 done-list。

## 驗證與現有資料限制

新增 41 個隔離檔案測試驗證多 view／alias 共用原文、body 與 URL 同源、完整來源欄位、grounded
claim 的人類檔名、重名／缺檔／循環／schema／symlink／I/O／budget 未知行為、stale passage、
來源變動的兩次 commit guard、舊來源刪除後的已保存 closure、同標題不同原文的身份、
shared 不刷新、獨立原文只強化一次，以及 supports／contradicts 的 apply gate。

原 transport／schema／audit／index transaction 單元測試改用簽名相容的 known-provenance
fixture，讓失敗注入仍打在原接縫；真正的 filesystem lineage 與套用守衛由新測試端到端驗證。
舊「刷新同名 marker」測試依契約改成「已知原文不刷新、不新增、不強化」，另補 real-source
回歸案例。其餘既有故障案例與門檻保留，沒有放寬 correctness assertion 來接受 unknown apply。

2026-10-31 唯讀盤點目前 12 條 active 薄證據主張：完整 origin closure **0/12**，能追到至少
一個 root 為 **7/12**；缺引用、grounding 循環、缺檔及 traversal budget 缺口不能
因為有部分 root 就省略。這是來源資料可用性盤點，不是錯誤率、A2 成效或新版 live run。
本輪未重跑 ingestion、未改舊 evidence／confidence／S、未另開 DB writer、未手動觸發 A2。

後續先針對少量具體樣本補明確 provenance／審閱 grounding 來源，再做 A05；不能為追求
新增 evidence 數量放寬 unknown gate。任何新資料格式補寫或歷史修復要有獨立授權、basis、
備份與冪等方案；本段不包含批量回填或重新計費生成。

本段完整驗證：`make check` exit 0，ruff clean、mypy 172 source files 無錯誤；
2087 passed、1 skipped、1 個既有 Chroma DeprecationWarning（16.67 秒）。
`git diff --check` exit 0。
