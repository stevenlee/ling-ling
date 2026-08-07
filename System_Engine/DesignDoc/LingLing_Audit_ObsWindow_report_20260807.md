# N.2 觀察窗評估報告（2026-08-07）

> 承 `NextPhase_roadmap_2026-07.md` §3.5 N.2 清單。觀察窗 2026-07-24 → 08-07（14 天）。
> 全程唯讀（Insights/Cortex frontmatter、`llm_trace.sqlite` ro、self_assessment_history、A2 報告、_pending）。腳本：scratchpad `n2_eval.py`。
> 期間 stevenlee 於 2026-07-27 另落 4 個 commit（`ee4f010`/`8e7d547`/`457012d`/`b1045a6`），已納入基準。

---

## 一句話結論

**創作面全面站穩、A2 機制證明穩健且有訊號（18% evidential，0 error）、我當時 flag 的兩個 bug（逾時落盤 / novelty null）已被 7/27 那批 commit 修掉。** 唯一仍卡住的結構性缺口是 Cortex 薄證據（active claim 100% 仍薄、零新 merge）——這正是 A2 從 dry-run 轉 apply 的理由。品質閘 parser 修法無回歸但樣本過小。

---

## 逐項

### 1. A2 evidence_traceback dry-run 命中率 ✅（可裁決 apply）
14 份報告、累計掃 70 claim-slots、128 段落判定：

| 關係 | 數 | 佔比 |
|---|---|---|
| supports | 22 | 17% |
| contradicts | 1 | 1% |
| neutral | 105 | 82% |
| unparseable / error | 0 / 0 | 0% |

- **evidential 命中 18%**（23/128）——對「falsifier-first、嚴格排除自我來源」的設計而言是有意義的訊號，不是雜訊；82% neutral 屬預期（多數檢索段落本就不獨立佐證）。
- **0 unparseable、0 error**（128 次 LLM 判定全部乾淨解析）＝機制穩健，`ee4f010`「preserve best traceback candidates」的同源取最近距離也生效。
- supports 實例看來是**真獨立佐證**（不同來源文件互證，例：claim「系統擴張真正極限在結構穩定性」← The Prince 的權力平衡論；claim「HBM 轉向低損耗介電」的反例查詢命中）。
- **1 筆 contradicts**（Intent-Based Ontology-Driven Security Response）＝falsifier-first 設計目標達成：Cortex 第一個 tension 訊號被撈出來。
- **裁決建議：GO（開 apply，保守版）**。supports→append evidence＋溫和 reinforce；contradicts→記 tension（不自動翻案，留人審）；沿用現有排除紀律。理由見 §5。

### 2. novelty by op ✅（站穩，且 null 已修）
創作型穩定高、montecarlo/full 對照低——與 t=0 一致、無回落：

| op | n | mean novelty |
|---|---|---|
| fable | 3 | **0.332** |
| analogy | 3 | 0.293 |
| counterfactual | 3 | 0.261 |
| dialogue | 3 | 0.253 |
| montecarlo | 2 | 0.133 |
| full | 2 | 0.129 |

- **novelty null: 0/16**（N.1 是 2/11）——item 9 已由 `457012d`（harden novelty inputs）修掉。

### 3. grounding 去集中化 ✅（守住）
NEW insight：132 refs、**51 distinct、top-4 share 0.212**（N.1 0.159、pre-fix 0.878）。輕微上升但仍遠離集中化，MMR 持續有效。

### 4. SE insight_dim ✅（穩定上升）
13.79（7/12）→ 13.94 → 14.22 → 15.18（7/27）→ **15.53**（8/03）。連續四點升，語義多樣度確實在擴。

### 5. synthesis 品質閘 ⚠️（無回歸，但樣本過小）
窗內僅 5 篇 synthesis，verdict 看似 3/5 unparseable，但拆開看**修法有效**：
- 4 篇是 7/24 03:xx–04:xx、`engine_build 2026-07-17`＝**parser 修法（23:08 push＋重啟）之前**的夜間產出，反映舊行為、不計入。
- 唯一 post-fix（8/07 AI Political Compass，`engine_build 07-27`、418K chars 超大文件）verdict unparseable——查 critique trace 是**真 #1 型**：模型寫了「建議的摘要結構／結論」段、完全沒有總體判定段。這正是 B2 設計上「無判定段→誠實 unparseable→NeedsReview」的兜底，非 parser miss。
- **結論**：parser 契約修法無回歸；但 post-fix 只有 1 篇，樣本不足以宣稱成功，續觀察。

### 6. item 8 逾時落盤 ✅（已修）
`8e7d547 reject failed generations before delivery`——insight 生成失敗不再落盤成檔。7/22 那種 25-char 垃圾檔不會再出現。

### 7. Cortex 薄證據 ❌（未動，A2 apply 的理由）
active claim **54/54（100%）仍薄**；adjudication `equivalent` 仍只 1（觀察窗零新 merge）。dry-run A2 不寫入，被動三 lever（grounding/decay/merge）依舊不累積 evidence——**證實 evidence 不會自己變厚，要 apply 才動得了**。（薄證據池 64→54 是 decay 讓弱 claim 淡出，非變厚。）

### 8. 🆕 M3 提案佇列慢性重塞（新發現）
`_pending` 又長回 4 筆（7/27＋8/03 各一 agent_insight＋cortex_falsifiability）。M3 每週產提案但**無退回記憶**：我 7/24 退回的 cortex_falsifiability，7/27/8/03 又各生一筆。所幸這兩筆是 rubric 文字微調（沒重犯「加 parser 不讀的 JSON 欄位」老毛病），屬良性。但機制上 approve/reject 端沒人持續消化 → 佇列必然再積。**建議**：①這輪 4 筆擇優 approve/reject；②M3 產前比對 `_rejected/`（同 target＋相似 edit 就跳過），否則陳舊提醒只是治標。

---

## N.3 決策建議（依序）

1. **開 A2 apply（保守版）**——命中率站得住、機制零錯誤、薄證據非 apply 不動。實作：supports→append evidence entry（帶來源頁引用）＋溫和 reinforce；contradicts→記 tension 待人審；維持自我來源排除。仍走 gated 旗標、default-off，先開一段時間量薄證據佔比是否真的降。
2. **清 _pending 4 筆**＋補 M3 退回記憶（比對 `_rejected/` 去重）。
3. **synthesis 續觀察**（樣本太小）＋#1 型 fallback verdict 仍是 backlog。
4. **O1-O4 本體論**：SE 已累四點穩升、Cortex 圖穩定——評估前提開始成立，但建議等 A2 apply 跑一輪、看薄證據能否降到有意義水位再定（apply 可能就補上大半本體論需求）。
