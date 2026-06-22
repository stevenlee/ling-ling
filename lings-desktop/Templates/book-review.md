### Output Format (Book Review)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Book Review: (Title)"
tags: ["book", "review", "learning"]
type: "book-review"
---
```

After the closing `---`, the Markdown body follows the structure below. The review *method* (verdict-first, transformative, learning-first, no preamble, no share CTA) comes from the `review` operation — this template only fixes the sections and the book-specific lens.

# (Book Title) — (a short verdict-flavoured subtitle)

**In one line:** one plain sentence saying what this book is.

*(Opening hook — 1–2 sentences on why it matters and who it's for. No header.)*

## The Verdict
Is it worth the reader's time? For whom, and who should skip it. Argue the judgement from the book's actual content.

## What You'll Take Away
The 1–3 transferable ideas, concretely — name each concept and where it applies, not "it's insightful."

## Core Ideas, in Q&A
A few question-and-answer exchanges posing the book's central questions (the ones a curious reader would ask) and answering them in plain language.

## The Honest Part
Real strengths and weaknesses; who it's *not* for; where it over-reaches, dates badly, or leans on its era.

## How to Read It
Entry point, reading path (which chapter or case to start with), and the background a reader needs — or a plain reason to skip it.
