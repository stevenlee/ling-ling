# 📓 @ling-resynthesize 範例指令

把**既有文件**的原始檔重新投入 `Consolidate/`，重跑一次 synthesis（Parts → Stitched →
Synthesis）。sidecar 圖片會一併還原，`images/<標題>/` 連結照樣解析。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案，用 `[[標題]]` 指定要重做的文件（可多個）。
> 原始檔來源是 `raw/consolidate/<標題>.md`；找不到會在報告裡告訴你。

---

## 範例：重做一份
```markdown
@ling-resynthesize [[公部門人工智慧應用參考手冊]]
```

## 範例：一次重做多份
```markdown
@ling-resynthesize [[A 文件]] [[B 文件]]
```

### 小提醒
- 適合在你調整了 persona/template/Profile 後，想用新設定重新生成既有文件時用。
- 它只是把原始檔放回 `Consolidate/`，剩下交給既有 ingestion 管線（不另開新路徑）。
