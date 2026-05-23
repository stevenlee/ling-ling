# Thoughtful Splitter — Test Corpus

This directory holds **golden test documents** for the `ThoughtfulSplitter`
implementation. Each file is hand-crafted to exercise a specific behaviour
of the chunking algorithm. They serve two purposes:

1. **Structural snapshot regression** (`tests/snapshots/*.snapshot.json`):
   deterministic chunk-by-chunk record. Any algorithmic change produces a
   diff that must be human-reviewed and explicitly accepted.
2. **LLM coherence scoring**: each file is fed to the splitter, then each
   chunk is scored by `LLMClient.score_text_quality`. We require a median
   score ≥ 6.5/10 to consider the splitter "good enough".

## What each file tests

| File | Primary behaviour exercised |
|---|---|
| `short_essay.md` | No split needed (single chunk path) |
| `long_essay_with_code.md` | H2 boundaries dominate; code fence atomicity |
| `nested_lists_and_tables.md` | Table + bullet list atomicity |
| `obsidian_callouts.md` | Obsidian `> [!note]` callout atomicity |
| `mermaid_heavy.md` | Multiple mermaid blocks; one oversize (> max_size) |
| `chinese_long_essay.md` | Chinese sentence boundaries (`。!?`); paragraph layout |
| `outline_dominant.md` | Long nested list — exercises Gemini Issue A fix (LIST_ITEM_END) |
| `long_unstructured_essay.md` | ~10k chars pure prose, no headers — exercises Phase 4 LLM topic refinement and the fallback path when LLM is off |

## Maintenance rules

- **Do not edit a corpus file after its snapshot exists** unless you are
  intentionally changing the test. Edits invalidate the snapshot.
- Keep each file under ~12 KB so the test suite stays fast.
- Real-vault content is fine — sanitize personal info before committing.
- When adding a new file, also add a row to the table above and generate
  its snapshot in the same commit.
