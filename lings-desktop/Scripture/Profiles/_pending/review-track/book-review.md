---
persona: reviewer
template: book-review
operations: [review]
description: 書評（報導者／書評人立場，助學習）— genre A
applicable_when: MANUAL / PUBLISHING ONLY. Select only when the user explicitly asks to publish a learning-oriented book review of an already-understood non-fiction book. NEVER auto-select during normal ingestion — the existing `book` profile owns ingestion of books.
---

Blog/publishing track. Turns an already-understood non-fiction book into a transformative, learning-first review.

Binds the shared `reviewer` persona + `book-review` template + `review` operation.

**Staged in `_pending` — does not auto-activate.** Promote to `Scripture/Profiles/` only when the review track is ready to go live. While here it cannot be selected by the router.
