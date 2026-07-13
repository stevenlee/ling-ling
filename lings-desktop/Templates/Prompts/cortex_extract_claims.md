You distill an insight report into atomic claims for a long-term memory store.
Extract AT MOST 3 claims. Each claim must be:
- ONE declarative sentence that can be judged true or false on its own
  (NOT a topic label like 'memory and learning').
- In the same language as the report.
- Self-contained: no dangling pronouns or 'this/it' references.
- 'Atomic' does not mean unconditional. Condition-based claims (e.g. 'Under X, A causes B') are better than vague absolutes.

Return ONLY a JSON array:
[{"claim": "<one sentence>", "summary": "<one-line gist>", "applies_when": "<specific context/condition this applies to>"}]
No prose outside the JSON. Return [] if the report contains no real claim.
