---
be_a: translator
use_template: translation-rpt
say: Traditional Chinese
digest_limit: 8192
digest_overlap: 1024
dreaming_from: 3
dreaming_to: 5
daydream: true
daydream_spontaneous: true
daydream_consolidation_budget: 10
daydream_bite_adjudications: 4
daydream_insight_budget: 1
daydream_spontaneous_budget: 1
self_healing: true
creativity: 0.4
max_output: 16384
memory_limit: 32768
search_depth: 3
strict_mode: true
visual_router: true
argument_map_mermaid: true
highlight_spans: true
highlight_max: 5
use_thoughtful_splitter: true
thoughtful_use_llm: true
---

# 📜 Scripture (Settings)

This file controls Ling Ling's behavior and performance. Changes take effect immediately.

### 🎭 Persona & Language
- **be_a**: Determines the AI's identity (e.g., `assistant`, `researcher`, `coder`).
- **use_template**: (Optional) Forces a specific template to override the default (e.g., `translation-rpt`, `wiki-note`).
- **say**: The output language (e.g., `Traditional Chinese`, `English`, `Japanese`).
- **creativity**: Temperature (0.1 - 1.0). Higher means more creative/random.
- **max_output**: Maximum tokens per response (approx. 3000-5000 characters).
- **memory_limit**: Context window size (crucial for Ollama).
- **search_depth**: Number of related notes to retrieve during RAG (1-10).
- **strict_mode**: If `true`, the AI will follow templates strictly and reduce "chatty" personality.

### 🍽️ Digestion (Chunking)
- **digest_limit**: Maximum characters processed in one go (Target size for split parts).
- **digest_overlap**: The context bridge (characters reused between parts to maintain memory).

### 🌙 Dreaming (Background Operations)
- **dreaming_from**: The hour (0-23) when background analysis and maintenance start.
- **dreaming_to**: The hour (0-23) when background operations stop.
- **self_healing**: If `true`, Ling Ling will automatically repair tag mappings and index inconsistencies.

### 🖼️ Learning Aids (Phase 6)
- **visual_router**: If `true`, long-doc Synthesis pages and Insight reports automatically get a "🖼️ 學習輔助" section — Ling Ling picks the right visual (comparison table / flowchart / mindmap / timeline / quadrant / concept map / argument map) for the content's structure, or attaches nothing if there's no strong structure. Each attach costs one extra LLM round. The on-demand `@ling-visualize` command always works regardless of this setting.
- **argument_map_mermaid**: If `true`, argument maps (Toulmin structure) additionally get a deterministic Mermaid graph below the Markdown. Pure structure-to-graph, no extra LLM call.
- **highlight_spans**: If `true`, each Part note gets its key sentences wrapped in `== ==` highlights. The spans ride along on the existing Part Digest call (no extra LLM round); a deterministic pass marks only verbatim matches, never touching the original wording.
- **highlight_max**: Maximum number of `== ==` highlights per Part note (default 5). Hard cap even if the model proposes more.

### 🔪 Chunking (how documents are split into Parts)
- **use_thoughtful_splitter**: If `true`, split documents structure-aware — cut at chapter/heading boundaries, pack short sections up to the size budget, and recursively sub-split sections that are too long. If `false`, fall back to mechanical character-count chunks. (Takes effect on the next pipeline build / daemon restart.)
- **thoughtful_use_llm**: If `true`, allow the splitter's LLM topic-shift refinement. *Currently a no-op placeholder (the real refinement lands in a later phase); structural chapter-splitting works regardless.*

---
*Note: Lower `digest_limit` improves accuracy for complex texts but creates more parts.*
