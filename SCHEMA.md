# LLM Wiki Schema

This document outlines the rules, conventions, and workflows for maintaining the LLM Wiki. As an LLM Agent, I will read and follow these instructions whenever interacting with the wiki.

## Architecture & Layers

1. **Raw Sources (`/raw/`)**: Curated immutable sources (articles, papers, images, etc.). I will read these but MUST NEVER modify them. Images should go in `/raw/assets/`.
2. **The Wiki (`/pages/`)**: LLM-generated markdown files. I have full read/write access to this directory. I will create summaries, entity pages, concept lists, and maintain cross-references.
3. **Index (`index.md`)**: A catalog of everything. Must be updated upon every ingestion.
4. **Log (`log.md`)**: Chronological append-only record. Must be updated after every operation using the `## [YYYY-MM-DD] Operation | Detail` format.

## Workflows

### 1. Ingestion Workflow
When asked to ingest a new source (or when a new source is added to `/raw/`):
- **Read & Analyze**: Understand the source's key points, entities, and concepts.
- **Discuss**: Review key takeaways with the user and confirm what should be emphasized.
- **Generate Summary**: Create a dedicated summary page in `/pages/`. Ensure it has YAML frontmatter (e.g. `title`, `date_ingested`, `source_type`). The summary should be a concise overview of the source, highlighting the key points and concepts.
- **Cross-reference**: Discover connections to existing pages. Update relevant concept/entity pages. If conflicts exist, document the contradiction.
- **Update Index**: Append the new summary page and any new entity/concept pages to `index.md`.
- **Log Operation**: Append a new entry to `log.md`.

### 2. Query Workflow
When the user asks a question via the wiki:
- Read `index.md` first to scan for relevant concepts or entities.
- Drill down into specific pages in `/pages/` for extraction and synthesis.
- Provide a synthesized answer with citations to the markdown pages.
- If the synthesized answer is highly valuable (a comparison, connection, or deep analysis), propose saving it back into `/pages/` as a new concept page.

### 3. Linting Workflow (Health-Check)
When asked to health-check/lint the wiki:
- Scan for orphan pages (pages without inbound links).
- Identify missing pages (concepts frequently mentioned but lacking their own page).
- Flag contradictory claims.
- Suggest further research or new questions to investigate based on gaps.

## Formatting Conventions
- **YAML Frontmatter**: Every generated file in `/pages/` MUST start with a standard YAML block for Obsidian Dataview compatibility:
  ```yaml
  ---
  title: [Page Title]
  type: concept | entity | summary | analysis
  date_created: YYYY-MM-DD
  tags: [list, of, tags]
  ---
  ```
- **Links**: Use standard markdown links pointing to relative files inside `/pages/`, e.g., `[[Concept Name]]` or `[Concept Name](concept_name.md)`.
- **Image Processing Guidelines**:
  - When encountering image files, prioritize extracting alt text or using vision tools to generate a summary.
When referencing images in Wiki pages, always use Obsidian format ![[image.png]].
If an image contains a data chart, the data must be extracted and converted into a Markdown Table or Mermaid diagram stored in the Wiki for future retrieval.