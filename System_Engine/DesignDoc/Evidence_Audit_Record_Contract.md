# A03：A2 私有判讀稽核紀錄

## 範圍與資料

A2 的每個有效 v2 判定（含 dry-run、局部支持、類比與 neutral）保存為
`Database/evidence_audits/<audit_id>.json`。此目錄沿既有 Database 的 Git ignore，
不進公開 Git、不送 RAG 索引；檔案由同目錄 temp 原子 replace，檔案與目錄 fsync
成功後才可被新 evidence 引用。caller 沿 maintenance scheduler 的 global busy lock
序列化；不新增 writer／thread，也沒有第二個程序開啟 Chroma。

record schema_version=1，含 audit_id、recorded_at 與 payload。audit_id 是 payload 的
canonical JSON（排序 key、禁止 NaN／Infinity、UTF-8）的 SHA-256；記錄時間不參與 ID。
相同 payload 重用第一次的檔案與時間，不重寫。讀取時驗 schema、內容 ID、內部 hash、
basis 主張與判定契約；損壞的既有 slot 不自動覆寫，回報失敗等待調查。

payload 包含：

- basis：Cortex 路徑、判讀當次完整 raw snapshot、SHA-256、claim_id／claim／falsifier。
  頁面由同一 raw snapshot 解析；不是在判讀完成後重讀另一版來補 basis。
- source：title／path／family、chunk ID（未知為空）、實際送審的前 2000 字與 hash、
  measured distance，以及相對於 retrieved chunk 的 `[0, len(passage))` 範圍。
  這不是原文件 offset；未知資料不偽造。family_method=`path_stem_v1_unverified`，
  明示尚未完成 A04 的原始來源獨立性追溯。
- verdict：A02 的六欄 v2 判定；parse／transport 失敗沒有偽造判定，仍保留在報告／trace。
- judge：實際載入且剝除 frontmatter 的 prompt body、內容 hash、asset 名稱、
  contract_version、provider／configured model ID、scope_attempt、trace_run_id。
  無 trace 時明示 null；模型輸出不能覆蓋 client 所附的 judge metadata。

prompt hash 是該次 prompt 的內容版本；保存 body 後，即使 prompt 修改或 trace 到期，
仍可重建當次輸入。model ID 是設定的 provider model identifier，不是模型權重或遠端
alias 所指版本的不可變 digest；不能據此保證跨時間的 bit-for-bit 重播。

## Commit 與恢復

1. 取得 basis、檢索並判讀，先保存有效判定的 audit。無法落盤即標示失敗；apply 延後。
2. apply 邊界重驗整批六欄契約、page／scan 的 claim basis、磁碟 raw snapshot 與路徑，
   並重驗／確認 audit 可持久化。任何一筆失敗，整條 claim 不部分強化。
3. 在 page 副本準備 evidence／counterpoint 及單次 reinforce，先寫既有 index intent。
   提交前再比對磁碟 snapshot；有任何改動則拒絕，保留人工編輯。
4. **Commit point = `save_cortex_page` 的原子 vault replace。** 新 evidence 持久化
   audit_id，反證 counterpoint 內帶 `[audit:<id>]`。Cortex reader 保留引用，之後重寫
   不丟失；舊 evidence 不自動補 audit 或升級成合格 v2 證據。
5. vault commit 後沿既有 index repair 更新正文／facets。索引失敗保留意圖並如實回報
   已生效待索引；下一次 A2 即使薄證據池為空，也只追補索引，不重做 reinforcement。

audit 是「判讀已記錄」，不是 applied/done ledger。先有 audit 但 vault 未 commit 的崩潰，
只留下 preparation，不推定已套用、不自動重播 mutation；之後正常掃描可重用同一 payload。
commit 後的引用以 Cortex 當前 evidence／counterpoint 為 ground truth，不維護第二份
applied done-list。重複來源仍沿既有 marker 刷新／不重複強化規則。

這是 daemon writer 的序列化與提交前 optimistic basis 檢查，不是 OS 跨程序 compare-and-swap；
不使用 busy lock 的外部編輯器仍可能在最後檢查與 replace 的極短間隙競寫。發現可重現案例時
應另訂跨程序協作，不能宣稱本實作提供檔案系統沒有的 CAS。

## 保存、診斷與剩餘驗收

不自動到期或刪除任何 audit，包括未引用的 preparation。保留期與 trace TTL 無關；
Cortex 或引用後續被人工移除，也不自動清除歷史 audit。人工清理前須先做引用盤點與
私有備份，再另定 prune 契約；此版本不提供 prune。這避免 audit 在 trace 到期後再度消失，
代價是私有快照持續占用磁碟空間。記錄檔維持 atomic writer 的 owner-only temp 檔權限。

報告列 audit ID 與失敗原因；`services.evidence_audit.load_audit(audit_dir, id)` 提供帶
完整性檢查的唯讀載入。trace 的 evidence_audit artifact 只記 ID／路徑／claim／relation，
原始內容留私有 snapshot；trace artifact 寫入失敗不推翻已持久化的 audit 或阻止其套用。

隔離測試涵蓋 hash／schema／引用損壞、同內容去重、舊與未引用紀錄保留、原子 replace 與
目錄 fsync 故障、稽核無法落盤不得套用、讀取 basis 失敗仍輪轉、判讀期間及 intent 準備後
人工編輯、commit 前／後崩潰，以及索引恢復不重複強化。測試不操作 live vault／DB。

A01 標註獨立複核與隔離真模型語義評估、A04 lineage、A05 新版正常排程的端到端驗收仍欠。
A03 提供可覆核資料，不證明模型判得對，也不將 A02/A03 離線測試通過當成 live 品質改善。
