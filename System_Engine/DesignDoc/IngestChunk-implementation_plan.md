# Implementation Plan: Robust Multilingual Chunking System

This document outlines the final design for processing long-form documents with high stability and semantic integrity.

## Goal
To handle large-scale document ingestion (>10k chars) while maintaining target language accuracy, consistent metadata, and seamless user navigation in the Wiki.

## 1. Pillar 1: Splitting Robustness (Syntax-Aware Exit)

### Logic: "Look-Ahead Safe Exit"
1.  **Threshold**: Start seeking an exit after `CHUNK_SIZE` characters (defined in `.env`).
2.  **Anchor Priority**:
    - **P1**: Next `# Header` or `## Header`. (Keeps topics together).
    - **P2**: Next `\n\n` (Paragraph break).
    - **P3**: Next sentence end (`. `, `。`, `! `).
3.  **Inhibitors (No-Split Zones)**:
    - **Code/Math**: Must have even counts of ` ``` ` or `$$` from the beginning.
    - **Tables**: Current line must not contain `|` if the surrounding context looks like a table.
4.  **Syntax Repair**: If a split MUST happen (e.g., at a max safety limit), automatically close open ` ``` ` in Part $N$ and reopen in Part $N+1$.

## 2. Pillar 2: Memory Continuity (The Context Bridge)

### Instruction Injection
For Part $N$ (where $N > 1$), the `system_prompt` or `user_msg` will be modified to include:
- **Pending Concepts Bridge**: In Part $N$, the LLM is instructed to output a `pending_concepts` field in YAML. This "memo" is extracted by Python and passed to Part $N+1$ as an explicit continuation target.
- **Contextual Bridge**: Provide the last 200 characters of the previous segment as a `[REFERENCE_ONLY]` block to ensure sentence-level continuity.
- **De-duplication Instruction**: "You have already covered [Pending Concepts from previous part]. Focus exclusively on the remaining information. Do NOT repeat or re-summarize established points."

### 3. Pillar 3: Metadata & Reading Continuity (Shared Soul)

### Hierarchical Page Organization
- **Chunked Page Folder**: For documents that are split, create a subdirectory in `pages/[Title]/`.
- **Organized Storage**: Save all Wiki parts (`Part N.md`) into this subdirectory to prevent cluttering the root `pages/` folder.
- **Chunked Archive**: Similarly, create a subdirectory in `raw/clippings/[Title]/` for original source segments.
- **Bi-directional Reference**:
    - Each Wiki page `pages/[Title]/Part N.md` will link to `[[raw/clippings/[Title]/Part N.md]]`.

### Tag & Title Management
- **Master Tags**: Inherit tags from Part 1 to maintain grouping.
- **Serial Titles**: Format: `Original Title (Part N)`.
- **Navigation UI**:
    - **Header**: Automatically add `[[Part N-1]] | Part N / M | [[Part N+1]]`.
    - **Footer**: Localized navigation buttons (Chinese/Japanese/English).

## Proposed Files to Modify/Create

### [NEW] [text_splitter.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/services/text_splitter.py)
- Implements the "Syntax-Aware Safe Exit" algorithm.

### [MODIFY] [config.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/core/config.py)
- Add `CHUNK_SIZE` and `CHUNK_OVERLAP` settings.

### [MODIFY] [clipping_watcher.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/watchers/clipping_watcher.py)
- Implements the sequential loop.
- Manages summary carry-over and tag inheritance.

### [MODIFY] [llm_client.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/services/llm_client.py)
- Update `generate_entity_page` to accept `context_hint`.

## Verification Plan

1.  **Visual Audit**: Check if Part 2 correctly links to Part 1 and uses the same tags.
2.  **Stress Test**: Feed a Markdown file with a code block spanning the chunk boundary. Verify syntax is repaired.
3.  **Language Check**: Verify Japanese navigation (`前へ / 次へ`) appears when language is set to Japanese.
