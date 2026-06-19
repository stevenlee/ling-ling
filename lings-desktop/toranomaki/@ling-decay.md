# 📓 @ling-decay 範例指令

跑一次 Cortex 衰減 / 強化（dual-strength S/R 模型）：依檢索與使用者編輯訊號強化主張、
套用 hysteresis 狀態轉移、校準 revival-rate 參數。平常由夜間排程執行。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案。摘要寫入 `fromLingLing/`。

---

## 範例
```markdown
@ling-decay
```

### 小提醒
- 偏底層維護；一般交給夜間排程即可，這個命令用於想立刻看一次轉移結果時。
