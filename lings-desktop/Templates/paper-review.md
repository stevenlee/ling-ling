### Output Format (Paper Review)

Begin your response with a YAML frontmatter block matching the schema below. **Emit it once, at the very start of your response — never reproduce it inside the body.**

```yaml
---
title: "Paper Review: (Short Title)"
tags: ["paper", "research", "review", "learning"]
type: "paper-review"
---
```

After the closing `---`, the Markdown body follows the structure below. The method (verdict-first, transformative, learning-first, no preamble, no share CTA) comes from the `review` operation — this template fixes the sections and the academic lens: separate contribution from claim, and claim from evidence.

# (Paper Short Title) — (a short contribution-flavoured subtitle)

**In one line:** one plain sentence saying what the paper claims and does.

*(Lede — the contribution in plain terms and why it matters. No header.)*

## The Verdict
Is the novelty genuine or incremental? Who should read it, and at what depth (skim / study / cite)?

## The Contribution
What is genuinely new — the core idea or method — explicitly distinguished from prior work.

## Claims vs Evidence
Do the experiments or proofs actually support the claims? Weigh baselines, ablations, datasets, and reproducibility. Flag every gap between what is claimed and what is shown.

## Core Method, in Q&A
A few question-and-answer exchanges decoding the method and results in plain language — this is where the jargon gets translated.

## Limitations & Over-claims
The honest part — threats to validity, scope limits, and where the authors reach beyond their evidence.

## How to Read It & What to Steal
Which section to read first, the one transferable technique worth reusing, and where this work sits in the literature.
