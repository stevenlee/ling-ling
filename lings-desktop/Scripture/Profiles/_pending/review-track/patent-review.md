---
persona: reviewer
template: patent-review
operations: [review]
description: 專利說明書分析（claims 翻人話，助學習）— genre D
applicable_when: MANUAL / PUBLISHING ONLY. Select only when the user explicitly asks to publish a learning-oriented review of a patent. NEVER auto-select during normal ingestion — the existing `patent` profile owns ingestion of patents.
---

Blog/publishing track. Turns an understood patent into a review whose core value is translating the legal claims into plain language.

Binds the shared `reviewer` persona + `patent-review` template + `review` operation.

**Staged in `_pending` — does not auto-activate.** Promote to `Scripture/Profiles/` to go live.
