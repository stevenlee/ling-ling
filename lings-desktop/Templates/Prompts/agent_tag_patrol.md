# Tag Patrol Agent - Instruction

## 🎯 Task / 任務
Scan the vault for tag inconsistencies and generate a "Tag Repair Report".
巡邏知識庫，找出標籤不一致的地方，並生成「標籤修復報告」。

## 🔍 Audit Rules / 審核規則
1. **Format Check**: Ensure tags are lowercase, use hyphens instead of spaces, and follow the normalized format.
2. **Bilingual Pair Check**: Ensure tags have their corresponding English/Chinese pairs (e.g., #蘋果 and #Apple).
3. **Missing Pairs**: If a pair is missing, suggest adding it or mark it as `[AUTO-LEARN]`.

## 📋 Report Requirements / 報告要求
- Use a tip callout `> [!TIP]` to explain how to fix.
- Group issues by type.
- List affected files for each issue.
- Use a checkbox format for user interaction: `- [ ] Reason: `bad` -> `good` (Affects: ...) | PATHS: `...``

---
*Let's keep the garden's labels neat and tidy! (ラベルをきれいに整理しましょう!)*
