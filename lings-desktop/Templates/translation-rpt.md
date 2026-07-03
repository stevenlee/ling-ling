### Output Format (Translation Note)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Translation Note: (Title)"
tags: ["translation", "original-language"]
type: "translation"
---
```

After the closing `---`, the Markdown body. The first-line H1 must be **one sentence capturing this part's core** (concrete, like a headline) — do **not** use a generic title like "Translation & Comparison Report" that repeats on every part:

# (One sentence capturing this part's core, as the title)

## Summary

## Translation Body

> Render statutory provisions and clause numbering (e.g. §2713, (a)(1)(A)) as **nested bullet lists** that preserve the source hierarchy. **Never** use a table to emulate clause/line numbering (e.g. `| 1 | ... |`). Reserve tables for genuine comparative data where the columns are semantically related.

## Glossary & Key Terms
