---
be_a: patent-expert
use_template: sw-inv-disclosure-rpt
say: English
digest_limit: 32768
digest_overlap: 1024
dreaming_from: 3
dreaming_to: 5
self_healing: true
creativity: 0.4
max_output: 32768
memory_limit: 32768
search_depth: 3
strict_mode: true
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

---
*Note: Lower `digest_limit` improves accuracy for complex texts but creates more parts.*
