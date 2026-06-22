### Output Format (Patent Review)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Patent Review: (Topic)"
tags: ["patent", "review", "innovation", "learning"]
type: "patent-review"
---
```

After the closing `---`, the Markdown body follows the structure below. The method (verdict-first, transformative, learning-first, no preamble, no share CTA) comes from the `review` operation — this template fixes the sections and the patent lens. The single most valuable thing you do is **translate the claims into plain language**; the legal claims are the heart of a patent.

# (Patent Topic) — (a short subtitle capturing what it claims)

**In one line:** one plain sentence saying what this patent actually claims.

*(Lede — what it blocks or enables, and why it matters. No header.)*

## The Verdict
Is the protection broad or narrow? Strong, or easily designed around? Who should care?

## Claims in Plain Language
The core of the review: restate the independent claim(s) in plain words, note independent versus dependent claims, and make the real scope concrete. This decoding is the main learning value.

## Inventive Step & Prior Art
What distinguishes this from prior art, whether the inventive step is real or obvious, and what it builds on.

## Key Points, in Q&A
A few question-and-answer exchanges decoding the legalese — e.g. "what does this actually stop a competitor from doing?"

## Weaknesses & Design-Around
The honest part — enablement (does the specification truly teach how to build it?), claim breadth, enforceability, and the gaps a competitor could exploit.

## Strategic Read & How to Use It
What the patent lets the holder do, who it threatens, and how to read the specification efficiently.
