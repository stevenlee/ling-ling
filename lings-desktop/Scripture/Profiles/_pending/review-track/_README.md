# Review Track (blog / 報導者・書評人) — staged

A parallel **publishing** track, separate from the ingestion profiles. It turns an
*already-understood* source (its Synthesis) into a transformative, learning-first
review for the blog. It does **not** replace or touch the existing `book` / `paper`
/ `patent` ingestion profiles.

## Pieces
- **Persona:** `Scripture/Personas/reviewer.md` — one shared voice (Ling Ling, 報導者／書評人).
- **Operation:** `Templates/Operations/review.md` — the shared method (verdict-first, transformative, learning-first).
- **Templates:** `Templates/{book-review, explainer-report, paper-review, patent-review}.md` — per-genre sections + domain lens.
- **Profiles (here):** bind persona + template + operation per genre.

| Profile | Genre | Template |
|---|---|---|
| `book-review` | A 書評 | book-review |
| `explainer-report` | B 報導/解說 | explainer-report |
| `paper-review` | C 論文分析 | paper-review |
| `patent-review` | D 專利說明書 | patent-review |

## Why staged in `_pending`
A live profile's `applicable_when` is fed to the auto-router during ingestion. Live
review profiles could hijack normal ingestion (produce a review instead of an
analysis). Staged here, they cannot be selected. Each is also marked
`MANUAL / PUBLISHING ONLY` as a second guard.

## To activate (when ready)
Move the chosen profile(s) up to `Scripture/Profiles/`. Invoke deliberately — set
`profile: book-review` on a document, or wire a dedicated publish command. Real
pipeline wiring of `operations: [review]` awaits the Planner; until then the
review is produced by the persona + template (as the standalone pilot did).
