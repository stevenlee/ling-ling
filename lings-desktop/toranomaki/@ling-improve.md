# 🛠️ @ling-improve 範例指令

自我改善的審核佇列(Metacognition M3)。系統會**自評 → 診斷 → 對自己的 prompt/template 產生修訂提案**,
但**永不自動套用**——提案進待審佇列,你看過 diff、核可了才生效,且原檔會先備份(一鍵可回退)。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案,用下面任一格式。報告寫入 `fromLingLing/`。
> 提案存在 `Scripture/Improvements/_pending/`;核可後原檔備份到 `_applied/`,退回的進 `_rejected/`。

---

## 範例 1：產生提案(跑一次完整自評→診斷→改善)
```markdown
@ling-improve generate
```
系統聚合所有品質訊號 → 對紅/黃軸診斷根因 → 對「報告品質」軸把最差的報告型別對應到它的
prompt 檔,產生一份修訂提案放進待審佇列。其餘軸(檢索、Cortex…)若非單一 prompt 可解,會誠實列出「需人工處理」。

## 範例 2：看有哪些待審提案
```markdown
@ling-improve list
```
(或直接 `@ling-improve`,不帶子指令也是列表。)

## 範例 3：細看一份提案的 diff
```markdown
@ling-improve show agent_counter-20260614153000
```
顯示根因、要落實的改善,以及**目前 → 提案**的逐行 diff。

## 範例 4：核可生效(原檔自動備份)
```markdown
@ling-improve approve agent_counter-20260614153000
```
把修訂寫回目標檔。若目標檔自提案產生後被你改過,會**拒絕覆蓋**(避免蓋掉你的編輯),請重新 `generate`。

## 範例 5：退回提案
```markdown
@ling-improve reject agent_counter-20260614153000
```

---

### 子指令一覽
| 子指令 | 作用 |
|---|---|
| `generate` | 自評→診斷→產生修訂提案(只入佇列,不套用) |
| `list`／(空) | 列出待審提案 |
| `show <id>` | 看單一提案的根因 + diff |
| `approve <id>` | 套用提案(原檔備份到 `_applied/`) |
| `reject <id>` | 退回提案到 `_rejected/` |

### 小提醒
- **提案永不自動套用**;這是刻意的安全設計——系統可以建議改自己的 prompt,但人保留最後決定權。
- 核可只會寫入允許的資產目錄(`Templates/`、`Personas/`、`Guidelines/`),不會碰到程式碼。
- 想讓每週維護自動產生提案(仍不自動套用),把 `.env` 的 `SELF_DIAGNOSIS_ENABLED` 與 `SELF_IMPROVE_ENABLED` 開起來。
- 每次核可的原檔都備份在 `Scripture/Improvements/_applied/<id>.original.md`,要回退就把它複製回去。
