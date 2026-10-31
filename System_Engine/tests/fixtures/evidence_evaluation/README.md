# A2 固定判讀評估集 v1（A01）

24 個合成案例與離線評分器。目的：為 A02 的支持範圍契約建立可重跑的驗收基準，
不改目前 production A2 的 relation JSON，也不呼叫 LLM、RAG 或 maintenance。

## 標註地位

`annotation_status: assistant_authored_pending_review` 表示參考答案由助理編寫、尚待
獨立複核。它們不是已經由使用者核定的真值，也不是當時私有 passage 的逐字副本。
前 8 例重現 [10/30 稽核](../../../DesignDoc/A2_support_quality_audit_2026-10-30.md)
的失敗機制；其餘補上正反對照與邊界情境。公開 fixture 只有新寫的短文。

| 分類 | 數量 | 參考標準 |
|---|---:|---|
| full_support | 6 | 同一範圍、條件及機制下，段落涵蓋主張所有重要部分；可用有界觀察或完整形式推導 |
| partial_support | 6 | 有相關證據，但只支持組件、較窄母體或部分條件；不作完整支持，也不表示已反駁 |
| analogy | 2 | 來自不同領域／情境的結構相似或理論啟發，沒有驗證目標主張 |
| contradiction | 4 | 直接不相容的觀察／論證，或符合主張條件的明確反例；不是單純沒有提及 |
| neutral | 6 | 話題相近但無證據、不同機制、未知、佔位文字或惡意指令，既非支持也非反證 |

複合主張按**當次提供的單一段落**評估，不合併未提供的其他來源。描述 API／Skills
可行性不自動證立所有組織「應該」採用的價值主張。段落逐字包含 claim 也可能是在
引用並否定它；引文存在不能取代語義判斷。段落中的 prompt injection 不具控制權。

這個五分類是評估用的標準表示，**不是已上線的 A2 輸出 schema**。現行三值 relation
缺乏 partial／analogy 訊息，不能直接冒充五分類結果；後續 adapter 及模型判讀另行實作。
來源獨立性、引文錨定、claim basis、live apply 交易也不是本工具已驗證的能力。

## 使用

在 repository root 執行：

```bash
PYTHONPATH=System_Engine venv/bin/python -m scripts.evaluate_evidence_cases \
  --inputs > /tmp/a2-evaluation-inputs.json
```

匯出只有 dataset_id／dataset_sha256 與 id／claim／falsifier／source_title／passage。
不把 expected_label、rationale、tags 或歷史稽核索引送給候選 judge。
資料格式／參考標註有實質修改時應更新 dataset_id；SHA-256 另防止同 id 的內容改動
誤用舊判讀。只變 JSON 排版不改 hash；參考答案／理由改動也會換 hash。

保存候選系統結果的格式（hash 從匯出輸入複製，以下只示意一筆）：

```json
{
  "dataset_id": "a2-evidence-scope-v1",
  "dataset_sha256": "<匯出的完整 SHA-256>",
  "predictions": [
    {"case_id": "legal_plugin_transfer", "label": "partial_support"}
  ]
}
```

```bash
PYTHONPATH=System_Engine venv/bin/python -m scripts.evaluate_evidence_cases \
  --predictions /tmp/a2-predictions.json > /tmp/a2-evaluation-result.json
```

本輪不附會自動初始化 LLMClient 的 runner；實際模型測試要由後續隔離 adapter 留下
model／prompt／contract 版本與呼叫結果，不能從 unit-test Fake 推算模型準確率。

## 分數與失敗

- 顯示整體 accuracy、逐分類 recall、confusion、錯把其他類別當完整支持的
  unsafe_full_support，以及逐案參考理由。單一 accuracy 不能掩蓋支持／反證全部漏掉。
- `passed` 只有全部 24 例精確符合參考分類才為 true。它只代表符合這版標註，
  **不是可以開啟 apply 的授權**，更不是代表標註已獨立驗證或真實世界安全性已證明。
- 缺少案例仍留在總分母，記 `missing`。呼叫／解析失敗請明示 `label: "error"`，
  不轉成 neutral、不刪案例。重複／未知 case_id、錯版 hash、非法分類或 malformed
  JSON 會報輸入錯誤；不取最後一筆或靜默忽略。
- 全 neutral 會失去支持及反證 recall；全 full_support 會暴露過度支持；兩者均不能通過。
- CLI exit 0：成功匯出，或分類全部符合參考；exit 1：合法結果但未通過；exit 2：輸入錯誤。
  只寫 stdout／stderr，若要保存結果由呼叫端明確 redirect 到離線檔案。

## 維護

新增案例須提供 claim、可對照 falsifier、合成 passage、來源識別、分類、理由與 tags。
passage 不超過現行 A2 的 2000 字預算，避免測試判斷看到 production 會截掉的證據。
若參考答案有歧義，先修改條件或註記待裁決，不以目前模型偏好倒改 gold 讓分數變綠。
保留 direct／contradiction 對照；不能用刪掉難例或全部判 neutral 解決誤支持。
