# Synthesis Quality Roadmap

## Current Baseline

The ingestion pipeline now uses structured part digests before final synthesis:

1. Split long source documents into parts.
2. Generate a normal wiki note for each part.
3. Generate a compact structured digest for each part.
4. Generate final synthesis from all structured digests.

This replaces the earlier first-line-only summary bridge, which gave the final synthesis too little source-grounded material.

A deterministic Markdown quality checker is also in place for generated Part notes and Synthesis notes. It currently:

- wraps bare `mermaid` blocks in fenced code blocks
- closes unterminated Mermaid fences
- removes accidental YAML frontmatter from generated body content
- records applied fixes in note metadata

## Long-Term Improvements

### 1. Semantic Quality Check

Extend the current deterministic checker into a semantic `synthesis_quality_check()` step after final synthesis. It should score:

- Missing required sections.
- Generic language ratio versus concrete source terms.
- Whether evidence and claims are grounded in part digests.
- Whether the synthesis merely restates part titles.

### 2. Retry With Critique

When the quality score is below threshold:

1. Generate a short critique explaining the failure.
2. Retry synthesis with the critique and the same structured digests.
3. Keep the better candidate and write the score into metadata.

### 3. Lineage Metadata

Persist richer lineage in the synthesis frontmatter:

- source file hash
- chunk count
- digest schema version
- synthesis prompt version
- model/provider
- quality score
- retry count

This will make it possible to debug or regenerate old synthesis notes deterministically.

### 4. Dedicated Templates

Create a synthesis-specific template instead of reusing `wiki-note` behavior. The template should require:

- core thesis
- major findings
- source-grounded evidence
- concept relationships
- limits and unresolved questions
- actionable insight

### 5. Regeneration Command

Add a maintenance command such as `@ling-regenerate-synthesis [[Title]]` to rebuild a synthesis note from existing part notes and source archives without re-ingesting the original clipping.
