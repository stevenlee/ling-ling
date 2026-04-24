# Move `raw` directory to root for Obsidian integration

The user wants to move `lings-desktop/Database/raw` to `lings-desktop/raw` because Obsidian expects it at the root. This change also aligns the implementation with `SCHEMA.md`.

## Proposed Changes

### File System Operations

- Move `/Users/stevenlee/projects/ling-ling/lings-desktop/Database/raw` to `/Users/stevenlee/projects/ling-ling/lings-desktop/raw`.

### System Engine

#### [MODIFY] [config.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/core/config.py)
- Update `RAW_CLIPPINGS_DIR` and `RAW_PROMPTS_DIR` to use the new `raw/` path.
- Add `RAW_ASSETS_DIR` for completeness.

### Documentation

#### [MODIFY] [README.md](file:///Users/stevenlee/projects/ling-ling/lings-desktop/README.md)
- Update path descriptions and directory tree to reflect the move.

#### [MODIFY] [SCHEMA.md](file:///Users/stevenlee/projects/ling-ling/SCHEMA.md)
- Ensure references are consistent (it's already mostly consistent, but I'll double check).

## Verification Plan

### Manual Verification
1. Verify directory move: `ls lings-desktop/raw`.
2. Check `Database` directory: `ls lings-desktop/Database` should only contain `chroma_db`.
3. Verify `config.py` changes.
4. Verify `README.md` updates.
