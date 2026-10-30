# A2 支持證據品質稽核 — 2026-10-30

## 結論

歷史 A2 有將局部支持、跨域類比或不同機制提升為整條主張獨立佐證的風險。
本次可逐段還原的 8 筆 supports，人工複核為局部支持 5、類比 2、機制不相符 1；
以「該段落能否支持整條 claim 的關鍵機制、條件及結論」為標準，沒有一筆足以無保留
評為完整支持。這是此次人工審閱判斷，不是重新呼叫模型的裁決，更不是證明 claim 為假。

建議優先修支持判定的範圍契約與可覆核證據，再重新觀察；不要調大距離門檻或強化幅度。
既有 evidence／confidence／S／索引維持原狀，不依本報告自動撤回。

## 範圍與限制

- 唯讀盤點 2026-08-08 起保存的 EvidenceTrace apply 報告、目前 Cortex 頁面與
  SQLite trace（`mode=ro`）。共 28 筆 supports／24 條 claim，均仍有對應有效 marker。
- 只有 8 筆（7 條目前 active claim，報告日期 10/14–10/22）仍有當次 supports
  呼叫的完整輸入及輸出。逐筆確認 trace 的 CLAIM 與目前 claim 文字一致。
  同 claim／source 的其他 neutral 或失敗嘗試不混作 supports 複核樣本。
- 其餘 20 筆只有報告理由及目前來源頁面等資料，無法還原當次送入的確切段落；
  不以現行整頁內容代替歷史 retrieval snapshot，也不為它們指定確定的逐段品質分類。
- 8 筆是 trace retention 留下的可觀測集合，並非隨機抽樣。不能推算全部 28 筆的
  錯誤率，也不能推算新距離契約上線後的支持品質。
- trace 的時間為 UTC；本報告表格日期沿用台北時間日報。來源是 vault 中的衍生
  筆記／學習產物，這輪未查原出版物，不宣稱已驗證轉述忠實度或原出版物的真實性。

## 逐段複核

| 日期／claim_id | 來源 | 人工分類 | 當次段落能支持什麼／仍缺什麼 |
|---|---|---|---|
| 10/14 `cortex-b168ba8d255bceb2` | NIST.AI.600-1 (Stitched) | 局部支持 | 有來源驗證、紅隊與持續監控等 AI 治理組件；未驗證將資安供應鏈受託審核轉移到法律 AI 插件後，能形成 claim 所述標準合規程序。 |
| 10/14 `cortex-bd7c3ca4ab644a40` | AITBM (Part 1) | 局部支持 | 圖中有權重、CRM、衰減模型；未呈現 Beta-Binomial、由全量檢查轉向特徵提取，或辨認風險波動主成分的效果。 |
| 10/16 `cortex-09e04fd42940b554` | Cordon Semantic Transactions… (Stitched) | 機制不相符 | 內容是線上工具調用攔截、操作歷史及事務驗證；不等於把 CoT 預先編譯成形式驗證的原子工具。 |
| 10/21 `cortex-f8588c9f8bdb2d0f` | The Prince (Part 9) | 類比 | 討論新政權、自主武力及政治根基。不能直接佐證高精準、零容錯物理結構在隨機環境振動下崩潰；falsifier 明確使用物理振動語境。 |
| 10/21 `cortex-a3ba274d81690132` | OWASP-Top-10-for-Agentic-Applications… (Stitched) | 局部支持 | 支持隔離、最小權限與故障遏制等控制手段；未建立「複雜度／擴張速度超越驗證極限」這個條件或准入措施的普遍必要性。 |
| 10/22 `cortex-01df8a68d39c051a` | 20260510Agent_台大楊立偉博士… (Part 5) | 局部支持 | 說明 API 與機器可讀環境；僅涵蓋複合 claim 的 API 組件，不足以單獨證立完整的組織決策 DNA／Skills 治理轉型建議。 |
| 10/22 同上 | 20260502AI新趨勢與產業新機會… (Part 5) | 局部支持 | 說明 SOP／流程知識封裝為 Skills；可作另一組件的依據，但規範性「應」仍須說明適用條件與目標。兩篇可共同提供組件線索，不自動等於兩份完整獨立驗證。 |
| 10/22 `cortex-b17b918671a3f7f8` | 納瓦爾寶典 (Part 12) | 類比 | 段落描述一般財富創造與資本／勞力／代碼槓桿，未提供併購、整合成效或單位成本等 claim／falsifier 所需的實例。 |

局部支持或類比仍可能有探索價值；問題在於目前被計入完整 evidence 並觸發整體強化，
而非它們毫無用途。若要評估多段組合後的 coverage，須另有明確的 claim 分解與合併規則，
不能把每段只有一部分的支持直接各算一次完整佐證。

## 可覆核 trace 索引

| claim_id／來源簡稱 | supports trace_id |
|---|---|
| b168／NIST | `llm_52c15fc78ab940cfa6988b1f05f32832` |
| bd7c／AITBM | `llm_82051246fa754520a55e5bdd81eab878` |
| 09e0／Cordon | `llm_30fe4c4eb8ef4f3788b87f7c19f6a61b` |
| f858／The Prince | `llm_215b1523e5354b88a679624cfe930ac3` |
| a3ba／OWASP | `llm_8ba1fef912fd4837ab5b08c5da7d603d` |
| 01df／API | `llm_b80fbb3f19004f26824ac4bfe0d271a0` |
| 01df／Skills | `llm_f83b84f54963420ab6c93d15921d0c9d` |
| b17b／槓桿 | `llm_46dca3ffb37d4982a3608f28999e532f` |

復核原料另保存在 `/private/tmp/ling-a2-support-quality.json`，包含 28 筆報告索引、
當前 claim／falsifier、存留輸入輸出與上述 trace 對照；腳本為同目錄
`ling-a2-support-quality.py`。tmp 檔不是永久交付資產，且 trace 會到期；本文件保存
人工判斷依據與限制，不把整段 vault 內容複製進版本控制。

## 程式契約缺口與後續

1. 現有 JSON 只有 relation／reason，無法表達「局部支持／類比」與 claim coverage，
   後續 apply 對 supports 一律新增或刷新佐證，新來源會觸發溫和強化。
2. 應將支持範圍及引文／缺口設為可驗證輸出，僅直接、完整支持進 apply；局部、類比
   留在報告，不增加完整 evidence 計數與整體信心。字面引文匹配只能證明引用存在，
   不能單獨證明語義蕴涵，仍需固定回歸案例與隔離判讀實測。
3. 保留當次送審段落的內容 hash、來源路徑／範圍及 claim basis；可恢復的 audit 資料
   應避免依賴 30 天 trace retention。不能把舊 verdict 套在已改變的 claim／來源上。
4. 來源獨立性另需追溯原始文件 lineage。不同 title 只通過目前排除規則，不等於
   不同原始證據。8 筆對應的原始 insight evidence 的 sources 均為空，無法只靠這個
   欄位宣稱已完整排除同源循環。此輪沒有證明它們真的同源循環。
5. `_is_independent` 的註解仍說誤放獨立來源「只多一次判讀，不會 state write」，
   已不符合現行 apply 行為，後續修支持契約時須一併更正。
6. contradicts apply 至今沒有可驗收樣本；不能因它為零便判定反證路徑正常或失效。

本輪只新增稽核與交接文件，未改 Python code、未跑 LLM／maintenance、未操作
live vault／DB／索引，未回滾歷史信念。未重跑 `make check`（文件變更）；
執行 `git diff --check`。最近程式交付檢查為 exit 0：1934 passed、1 skipped。
