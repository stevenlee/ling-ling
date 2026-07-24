# N.1 觀察窗重稽核報告（2026-07-24 補跑）

> 依 `NextPhase_roadmap_2026-07.md` §N.1 到期執行清單執行（原排 2026-07-20，延後 4 天＝觀察窗實際累積 11 天，樣本更足）。
> 全程純讀取：Insights/Cortex frontmatter、`self_assessment_history.json`、`llm_trace.sqlite`（ro URI）、`Scripture/Improvements/_pending/`。
> 量測腳本：session scratchpad `reaudit_n1.py`。

---

## 1. 指標 vs 期望（NEW-only，date_created ≥ 2026-07-13）

| 指標 | t=0 (7/13) | 觀察窗實測 (NEW only) | 期望 | 判定 |
|---|---|---|---|---|
| novelty by op | analogy .212 / cf .203 / dialogue .229 / fable .314 / mc .126 | analogy .294 / cf .283 (n=3) / dialogue .297 (n=2) / mc .128；手動 full .134 (n=2) | 創意型維持高、不回落 | ✅ **有效且穩定**（創意型 2.2× montecarlo） |
| grounding distinct / top-4 share | 39 / 0.662（pre-fix 12 / 0.878） | **107 refs、57 distinct、top-4 share 0.159**；舊 hub 命中僅 8/107 | distinct 續升、share 續降 | ✅✅ **大成功** |
| SE insight_dim | 13.9 | 13.79 → 13.94 → **14.22**（3 點，週頻） | 續升或持平 | ✅ 緩升（點數仍少） |
| synthesis verdicts（窗內） | 7d 窗 unparseable 4（存量） | **keep 1 / unparseable 11 / None 2** | unparseable → ~0 | ❌ **惡化——根因已鎖定，見 §2** |
| pending 提案 | 1（28d） | **4 筆、最老 40 天、0 筆被審** | 陳舊被消化 | ⚠️ 產出端通、消化端停（見 §3） |
| Cortex thin-evidence / merges | 薄證據 ~52/53、merge 期望 >0 | **79/80（98.8%）薄**；adjudication equivalent 仍 1（無新 merge）；contradictions 0 | 佔比下降、merge >0 | ❌ **未降 → A2 觸發條件成立** |

輔助讀數（7/20 self_assessment）：報告品質 🥀 rate=1.0（如實反映 §2 的洞——計量修正 c949f6a 工作正常）；Cortex 信念 🥀（thin 76、dogmatic 2）；LLM 健康／檢索／SE 皆 🌸。

---

## 2. 頭條發現：synthesis 品質閘再次失效——**prompt 與 parser 的格式契約自相矛盾**（自傷型）

**症狀**：觀察窗 14 篇 synthesis 裡 11 篇 unparseable（全部 `critique_attempts: 2`——bb811db 的重試有跑、也誠實記錄，沒有靜默放行；但重試後仍讀不出）。另 2 篇 verdict None（attempts=1）。

**根因 A（主要）**：`bb811db` 把 critique.md 的判定段設為 REQUIRED，字面規定 header 為 **`**總體判定 (Overall Verdict)**`**。gemma 現在**忠實照寫**（實查 7/24 三筆 critique trace：header 一字不差，判定 `keep`/`revise` 在下一行、常帶反引號）。但 `critique_loop.py` 的兩條 regex 都不認這個形態：
- `_VERDICT_RE` 要求 header 同行有冒號——規定格式沒有冒號；
- `_VERDICT_SECTION_RE` 要求 header 行尾只能是 `[\s:：*]*`——` (Overall Verdict)**` 的括號英文尾直接 fail。

即：**閘門 prompt 要求的格式，閘門 parser 自己讀不懂**。模型越合規、unparseable 越多——這正是 unparseable 從 7/13 起每篇必中的原因。

**根因 B（次要）**：7/24 有 3 筆 critique 呼叫**回應全空**（reasoning model 把內容丟 reasoning channel、content 空——與 A3 診斷 adjudicate_claims 同族）。空回應 → `critique_once` 回 `("", None)` → 無 section 不重試 → **該 2 篇 synthesis 無閘出貨**（verdict NULL）。adjudicate 已用 reroll 修過（3945e3f），critique_text 還沒有。

**修法建議**（下一動，小修）：
1. `_VERDICT_SECTION_RE`／`_VERDICT_RE` 允許 CJK header 後掛括號英文尾（如 `(?:\([^)\n]{0,30}\))?`）；判定行已可吃反引號、不用動。
2. critique_text 空回應套 A3 式 reroll（attempt1 temp0、升溫重試）。
3. **加「格式契約測試」**：單元測試直接以 critique.md 規定的字面格式為 fixture——凡改 prompt 規定格式，parser 測試必須同步紅燈。這是本 bug 的結構性教訓：header 形態已第三次漂移（判定/評定/評價 → 結論/裁定 → 雙語括號），每次都是事後追認；契約測試把它變成事前擋下。

---

## 3. 其餘軸線判讀

**M-arc（C 軸）**：接通驗證**成功一半**。a6aa6d1＋a16694a 之後，觀察窗內產出 2 筆新提案（7/20：agent_insight＋**cortex_falsifiability**——M3 對 Cortex 軸出提案了，A1 wiring 實證有效）。但 `_pending` 4 筆全數未審、最老 agent_counter 已 **40 天**。產出端修好了，消化端（人審 `@ling-improve`）沒有動——弧線現在斷在最後一哩。

**Cortex thin-evidence（A2 觸發判斷）**：79/80 薄（含觀察窗新 claim 9 筆、被更新舊 claim 34 筆——decay/reinforce 在動，但 evidence 累積沒動）；adjudication cache 中 equivalent 仍只 1 筆＝觀察窗零 merge。**roadmap A2 的觸發條件「三 lever 到期仍未降」成立**——建議建針對性證據追溯任務（gated、idle 補做契約）。

**產出頻率**：觀察窗 12 天出 9 篇排程型＋2 篇手動 full。缺 7/15、7/17、7/18 三個夜間 slot——與 7/18–7/20 的重 ingestion（Mathematics for CS 等，數百筆 part timing artifacts）時段重疊，疑似被 busy 排擠，非 regression；續觀察。

**兩筆退化檔（小項）**：
- 7/22 fable：`output_chars: 25`（本體近空）、novelty null、grounded_on 空——一次失敗生成照樣落盤；且其種子之一是 Cortex claim 標題（「High-quality allegorical insights…」）＝自我引用種子，有 echo 之嫌。建議清掉該檔＋查 seed 池是否該排除 claim 型種子。
- 7/23 montecarlo：refuted、novelty null、grounded_on 恰好是 3 個舊 hub（該篇只取 3 條 prior）。單篇不構成趨勢，但 novelty null 2/11 值得查 signals 計算為何跳過。

---

## 4. 干預成效趨勢小結（一段話）

**創作面三修法（殼改造／輪替／MMR grounding）全部站住**：novelty 創意型穩在 0.28–0.30（montecarlo 對照 0.128 不變）、grounding top-4 share 0.662→**0.159**、SE 緩升——不需回退、不需加碼。**品質閘卻在觀察窗全程失效**：不是回落，而是 bb811db 自己引入的 prompt/parser 格式契約矛盾（＋critique 空回應無重試），11/14 unparseable、2 篇無閘出貨；修法小、方向明確。**Cortex 薄證據三 lever 無效實錘**（98.8% 薄、零 merge）→ A2 證據追溯任務該建了。M-arc 產出端已通、卡在人審佇列（4 筆 pending、最老 40 天）。

## 5. N.2 決策建議（依序）

1. **先修品質閘**（§2 三項：regex 括號尾＋critique reroll＋格式契約測試）——RED 軸止血，半天工。
2. **建 A2 證據追溯任務**——觸發條件已成立（gated、default-off、走既有 idle/maintenance 契約）。
3. **人側：清 `_pending` 佇列**（4 筆，`@ling-improve` 審）。
4. **O1-O4 維持暫緩**——SE 才 3 個點、thin-evidence 未解前，概念層評估的前提還不成立；A2 跑一輪後再看。
5. 小項：刪 7/22 退化 fable 檔（或 resynthesize）、查 novelty null 計算跳過原因、seed 池排除 claim 型自我引用。
