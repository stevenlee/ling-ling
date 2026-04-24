# ChromaDB Reliability & Tagging Upgrade

This plan addresses several critical issues in the RAG (Retrieval Augmented Generation) pipeline, including "zombie" data chunks, missing tags during initial indexing, and database locking conflicts.

## User Review Required

> [!IMPORTANT]
> **Database Wipe**: As requested, I will include a command to wipe the existing `chroma_db` and perform a full re-index. This will ensure all "zombie" chunks are removed and all notes are re-indexed with correct tags (including hashtags).

> [!NOTE]
> **Hashtag Parsing**: I will implement a regex-based parser to extract `#tags` from the markdown body. These will be merged with the official YAML `tags` defined in the frontmatter.

## Proposed Changes

### Core Architecture

#### [NEW] [parser.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/core/parser.py)
A shared utility module to handle:
- Extracting YAML frontmatter using `PyYAML`.
- Extracting hashtags from the markdown body using regex.
- Normalizing tags to prevent duplicates and case inconsistencies.

---

### Database Management

#### [MODIFY] [rag_manager.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/rag_manager.py)
- **Zombie Cleanup**: Modify `add_document` to explicitly call `self.delete_document(title)` before upserting new chunks.
- **Retry Logic**: Wrap database operations in basic try-except blocks with a small retry window to handle temporary SQLite locks.

#### [MODIFY] [init_rag.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/init_rag.py)
- **Wipe & Re-index**: Add functionality to delete the existing ChromaDB collection before starting the indexing process.
- **Tag Integration**: Update the indexing loop to use the new `parser.py` to extract tags from every file.

---

### Watchers

#### [MODIFY] [vault_watcher.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/watchers/vault_watcher.py)
- **Shared Parsing**: Replace the inline regex logic with the new `parser.py` utility.
- **Improved Metadata**: Ensure that when a user modifies a file, both frontmatter and body tags are updated in the RAG memory.

## Verification Plan

### Automated Tests
- Run the new `init_rag.py` and verify the collection count in the logs.
- Trigger a manual edit on a file with both YAML tags and `#body-tags` and verify they appear in the logs/DB.

### Manual Verification
- Execute `@ling-insight` after the re-index and confirm that the reports are based on up-to-date, non-duplicated information.
- Inspect `lings-desktop/Database/chroma_db` to ensure no orphaned lock files remain.
