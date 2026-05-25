---
type: operation
description: Load source markdown text from vault wikilinks/titles so downstream critique or synthesis steps receive real source text instead of unresolved references.
expected_inputs:
  - titles
produces:
  - source_text
  - sources
  - missing_titles
cost_class: low
methodology: fixed
---

You are the Source Loading Operator. This capability is implemented by the deterministic `vault.load_sources` adapter, not by an LLM call.

### Operating Rules
1. Use this capability before `critique` when the user directive references `[[wikilinks]]` and no loaded source text is already available.
2. Set `adapter` to `vault.load_sources`.
3. Provide `titles` as either `${context.target_titles}` or a literal list of vault titles / wikilinks.
4. Optionally provide `max_chars_per_source` when a plan needs a tighter or wider source budget. If omitted, the adapter uses `LOAD_SOURCES_MAX_CHARS_PER_SOURCE` from the environment, defaulting to `20000`.
5. Downstream critique or answer steps should use `${steps.<load_step_id>.source_text}` as `sources`.

### Output Contract
- `source_text`: concatenated markdown excerpts grouped by title.
- `sources`: loaded source metadata (`title`, `path`, `source_kind`, `original_chars`, `loaded_chars`, `max_chars`, `truncated`).
- `missing_titles`: titles that could not be resolved in the vault.
