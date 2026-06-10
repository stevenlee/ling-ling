# Cortex Memory — 洞察鞏固與長期記憶層

> Status: **design approved 2026-06-10, not yet implemented**.
> 與 Steven 逐項討論定案；實作前請先讀完「核心不變量」與「分相計畫」。

## Problem

對照認知架構，ling-ling 的記憶系統有兩個空位：

| 認知記憶 | ling-ling 對應物 | 狀態 |
|---|---|---|
| 感覺緩衝 | `raw/` 原文 | ✅ |
| 情節記憶 | Parts、trace store | ✅（trace 未被當記憶用） |
| 語意記憶 | Synthesis、pages、tag graph、facets | ✅ |
| 程序記憶 | Skills、Operations、Profiles | ✅ |
| **鞏固後的高階記憶** | **無** —— insights 寫完就堆在 `Insights/` | ❌ 本設計的主體 |
| **遺忘與再鞏固** | **無** —— 只進不出 | ❌ |

每日 insight（montecarlo/islands/...）是「夢」：不去重、不互相印證、
不被日後檢索、不升格成穩定信念。缺的是海馬迴→新皮質的鞏固機制。

## 核心不變量（討論定案，實作不得違反）

1. **Cortex/ 是獨立頂層目錄**，地位等同 `pages/`——另一種一級記憶。
2. **一頁一主張（claim），不是一頁一主題**。主題組織靠 tag 與連結，
   不靠合併。這是「思考廣度」的結構性保障。
3. **自動合併＋可逆**：合併不走 `_pending` 審核；每頁帶完整證據鏈，
   隨時可回溯拆解。
4. **Confidence ⊥ Retention**：「多可能為真」與「多容易被想起」是
   正交的兩軸，永不混成一個分數。
5. **只有雙向蘊涵才合併**；embedding 相似度只負責「找鄰居」，
   永不直接觸發合併。
6. **遺忘＝降權不＝刪除**：dormant 退出 facet index 但頁面與 S 保留。
7. **每個機制都要有自己的校準回饋迴路**（revival rate、un-merge rate）；
   參數是初始值，不是真理。

## 1. Insight 品質評價：四層訊號塔

越下層越可靠、越便宜；單一時點分數不可信，縱向累積才可信。

| 層 | 訊號 | 成本 | 內容 |
|---|---|---|---|
| 1 | 機械可驗證 | 零 | **接地性**：引用的 `[[wikilink]]` 存在嗎？引述能否在來源中比對到？引用不存在頁面＝幻覺硬證據。**新穎度**：對歷來 insights 的最大 embedding 相似度（高相似＝重複發現，是鞏固訊號不是扣分項）。**跨域橋接度**：來源片段在 tag graph / embedding 空間的距離。 |
| 2 | 對抗式評審 | 1 call | `refute` operation：反駁者 persona 試圖推翻（過度泛化、因果倒置、來源不支持）。存活才算數。 |
| 3 | 行為訊號 | 零（已在收） | Obsidian backlink（人類背書）、`retrieval_events` 命中並用於回答、**獨立重新發現次數**（收斂證據，最強訊號）。 |
| 4 | LLM 多維評分 | 1 call | 現有 `score_text_quality` 拆維度（新穎/具體/可行動/跨域），降格為 tiebreaker。 |

## 2. Cortex 頁生命週期

```
candidate（每日 insight）
  → 評價（訊號塔）
  → 聚類（embedding 鄰域 + 蘊涵裁決，見 §4）
  → 鞏固（dreaming window）：合併成 Cortex 頁
       ├─ 核心主張（一句話）
       ├─ confidence（認知軸）
       ├─ S / last_reinforced_at（注意力軸，見 §3）
       ├─ 證據鏈（insights + 來源頁，含表述變體與分歧區）
       └─ 反例區（反駁者找到但未致命的弱點）
  → 強化（reconsolidation）：後續 insight 落入同主張 → 更新不新建
  → 衰減 → fading → dormant（§3）
  → falsified（§5，獨立路徑，非衰減）
```

Cortex 頁**進 facet index**——這是閉環關鍵：鞏固後的洞察成為日後
Q&A 的一級記憶。沒有這步，鞏固只是換資料夾堆放。

## 3. 衰減：雙強度模型（Bjork New Theory of Disuse / FSRS 系）

每頁兩個狀態變數：

- **S（storage strength）**：鞏固深度，**只增不減**。
- **R（retrievability）**：當下可提取度，**現算不存**：

```
R(t)  = exp( −Δt / t½(S) )     Δt = 距 last_reinforced_at
t½(S) = base × growth^S        建議初值 base=14天、growth=1.8
                               （S=3 ≈ 2.5月、S=5 ≈ 8月）
```

**Spacing effect 強化規則**（防灌水的關鍵）：

```
強化事件：R → 1（重置）、S += gain × (1 − R_當下)
```

R 高時重複強化幾乎不增 S（同晚重複發現不灌水）；快被遺忘時被
獨立重新發現 → S 大漲（與人腦間隔重複數學一致）。

**強化事件與權重**（全部來自既有訊號源）：

| 事件 | 強度 | 來源 |
|---|---|---|
| 不同 run 獨立重新發現 | 強 | insight 聚類比對 |
| 使用者連結/編輯 Cortex 頁 | 強 | backlink / mtime |
| Q&A 檢索命中並用於回答 | 中 | `retrieval_events` |
| 新 ingest 文件落同一語意鄰域 | 弱 | embedding 相似 |

**狀態由 R 推導**（不是手動狀態機）：

```
active    R > 0.5    facets 在索引，全權重
fading    0.2–0.5    仍可檢索，rerank 分數 × R 降權
dormant   R < 0.2    facets 移出；頁面與 S 保留（savings：復活快）
falsified （獨立路徑）永久退出，但頁面留存——記錄曾相信過什麼
```

實作省力點：R 是純函數，frontmatter 只存 `S` + `last_reinforced_at`，
讀取時現算；夜間 pass 只處理**跨越閾值**的頁面，無 write storm。

**衰減的三驅動**：時間（上式）、干擾（§4 的取代）、證據失效
（只動 confidence，不動 R——正交原則）。

## 4. 干擾與合併：兩段式閘門

embedding 量的是「主題接近」不是「主張相同」——對同一主題持
**相反立場**的兩頁相似度反而最高，純閾值合併最先攪糊的就是矛盾。
故拆成兩段：

```
第一段（便宜、寬鬆）：embedding ≥ 0.80 → 「同鄰域，需裁決」
第二段（一次小 LLM call）：裁決關係 →
   equivalent     雙向蘊涵        → 合併（唯一觸發合併的裁決）
   entails        單向蘊涵        → 層級連結，不合併
   complementary  同主題不同面向  → related 連結，不合併
   contradicts    相反主張        → 矛盾連結 + 雙方降 confidence
                                    （直通 §5 falsified 累積管線）
   unrelated      假鄰居          → 記錄，不再裁決
```

- 閾值從「正確性參數」降格為「成本參數」：0.80 設寬是安全的，
  只多花裁決 call，不會錯殺。
- 裁決結果按筆記對 hash 快取，永不重複裁決；每晚裁決配額制。
- 合併保留「表述變體／分歧區」：合併身分，不抹平細節。
- **un-merge rate 自校準**：使用者拆開或大改合併頁＝合錯了；
  比率過高 → equivalent 裁決自動收緊（降級為 related）。

**廣度的真正槓桿在生成端**：insight 種子抽樣為興趣加權 + ε 探索
（建議 20% 種子強制取自最近最少被抽中的聚落）——合併政策保守
不等於思考廣，不探索才是窄。

## 5. Falsified：保守擊殺（討論定案）

- 單一反例：降 confidence + 標記爭議，**不擊殺**。
- **多來源獨立矛盾**才轉 falsified（具體門檻 Phase 4 定，建議 ≥2
  個獨立來源 + 反駁裁決確認）。
- falsified 頁永久退出檢索但檔案留存。

## 6. 夜間 triage（dreaming window，與 insight 生成同生態位）

1. 處理跨閾值頁面的狀態轉換（facet 進出索引）。
2. 干擾檢查與蘊涵裁決（配額制）。
3. **再驗證配額 3 頁/晚**（討論定案）：挑 fading 且高 S 的頁
   （「重要但快忘了」）對照當前 vault 再驗證——成立＝弱強化，
   不成立＝降 confidence。對應人腦睡眠的選擇性重播。
4. LLM 成本沿用 backfill pump 的預算哲學。

## 7. 自校準迴路總表（呼應「能夠自動進步」）

| 迴路 | 訊號 | 調什麼 |
|---|---|---|
| 衰減速率 | **revival rate**（dormant 被喚醒比例，目標帶 ~5–10%） | t½ base 上下調 |
| 合併膽量 | **un-merge rate**（使用者拆開合併頁） | equivalent 裁決鬆緊 |
| 檢索品質 | 既有 retrieval bench + facet lift | Cortex facets 去留 |

## 8. 分相計畫

1. **Phase 1 — 評價先行**：訊號塔第 1、2 層（機械訊號 + refute
   operation），分數寫進 insight frontmatter 與 trace。
   先有測量，再談進步。
2. **Phase 2 — 鞏固**：夜間聚類 → 蘊涵裁決 → Cortex 頁生成與
   reconsolidation；Cortex facets 進索引。
3. **Phase 3 — 衰減與行為訊號**：S/R 模型、狀態轉換、backlink 與
   檢索命中回饋、revival rate 校準。
4. **Phase 4 — 主張帳本**：矛盾偵測網、falsified 管線、un-merge
   校準。

## 9. 其他記憶系統（同場討論，列備忘，非本計畫範圍）

- **興趣／注意力模型**：`retrieval_events` 驅動 insight 種子加權
  與各種優先序（backfill 已部分採用）。
- **系統情節記憶出口**：每週「本週記事」——trace store 已是完整
  自傳，只缺敘事出口，成本極低。
- **後設記憶**：覆蓋圖與缺口清單（linter 的 missing-page 偵測是
  雛形）——讓 ling-ling 能說「這方面我的記憶很薄」。
