# Profile System — Design & Implementation Notes

> Status: **landed 2026-06-10**. Replaces the `Scripture/DocType.md` table.

## Problem

Persona 與 Template 的配對只存在 DocType.md 一張表裡，三個結構性缺陷：

1. **鬆散配對**：`cookery-curator` persona 配上 `universal-document-template`
   時，persona 要求的段落模板裡沒有，STRICT_MODE 只能硬壓一邊。
2. **資產不自我描述**：Operations/Skills 有 frontmatter，Personas/Templates
   是裸 markdown，系統無法「看見」自己有哪些角色可用，只能開放式分類後查表，
   分類落空率高且不可控。
3. **自動生成無品管**：分類落空時 `generate_persona_and_template()` 直接寫檔
   進 vault 並註冊生效，長期累積未審視的低品質資產。

## Design

### ProfileSpec / ProfileManager（services/profile_manager.py）

- 一個 profile = `Scripture/Profiles/<name>.md`，檔名即正式 id。
- Frontmatter 契約：`persona`（必填）、`template`（必填）、`operations`
  （選填，保留給 PipelineRunner/Planner，依 adapter registry 約束消費）、
  `description`、`applicable_when`（餵給 LLM 選擇器的 hint）。
- 掃描規則與 CapabilityManager 一致：跳過底線開頭、語系變體（`foo.zh.md`）、
  `_pending/`；缺 persona/template 的檔案略過並 warning。
- 每次 ingestion 重建 registry（讀數個小檔，成本可忽略），vault 編輯即時生效。

### 三層解析（ingestion_pipeline._resolve_routing）

```
1. 手動層   frontmatter synthesis_persona/synthesis_template 或 profile: 名稱
2. 自動層   document_type 命中 profile → 直接用（零 LLM call）
            否則 LLMClient.select_profile() 封閉式選擇（temperature 0）
3. 預設層   default profile → Scripture 設定（be_a / use_template）
```

`select_profile` 與舊 `classify_document` 的差異：前者只能回答已註冊的
profile 名稱或 `none`，答案永遠可執行；後者保留，僅用於替新類型命名 slug。

### 審核佇列（quality over immediacy）

無法分類 → `queue_pending()` 寫三件套到 `_pending/<category>/`（檔名即目標
檔名，搬三次即生效）＋ `fromLingLing/` 審核通知。`has_pending()` 防止同類型
重複草擬。當次 ingestion 用 `default` profile，草稿永不自動生效。

### 統一資產契約

`_build_system_prompt` 的 persona/template 載入從 `_load_localized_content`
改為 `_load_capability_body`（先做語系 fallback、再剝 frontmatter）。
`strip_body_frontmatter` 只在開頭 `---` 區塊可解析為 YAML mapping 時才剝，
無 frontmatter 的既有檔案行為完全不變。此後 Personas/Templates 可以安全加上
與 Operations 同款 metadata（為 trace 與未來的 capability 註冊鋪路）。

### 遷移

`ProfileManager.migrate_from_doctype()`：registry 為空且 DocType.md 存在時，
把表格列逐一轉成 profile 檔（已存在的不覆寫，使用者編輯永遠優先），之後
DocType.md 不再被讀取。Vault 中的 DocType.md 已加退役註記，確認後可刪。

## Trace hooks

`doc_config` 多帶 `profile`（選中的 profile 名）與 `operations`，
`select_profile` 的 trace metadata 記錄候選清單（`options`）——這是後續
「路由決策追蹤／fallback 率分析」功能的資料基礎。

## Tests

- `tests/test_profile_manager.py`：掃描規則、遷移冪等、`_pending` 不生效。
- `tests/test_dynamic_pipeline.py`：三層解析全路徑、長文 parts/synthesis
  分流、未知類型送審 + default fallback、重複送審防護。
