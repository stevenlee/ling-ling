# Prompt System Architecture

> Map of how Ling-Ling assembles LLM prompts. Written 2026-07-06 at the end of
> the Prompt-System review (P1–P6). Line numbers are accurate as of that commit;
> if they drift, the symbol names are the stable anchors.
>
> Related: `PromptSystem_P3-P6_implementation_plan.md` (the brief this closes),
> memory `prompt-system-review` (findings F1–F7).

## 1. Three prompt paths

There is no single prompt pipeline — there are three, by design. Knowing which
path a call takes tells you where its prompt comes from and what scaffolding
(language, persona, template) it does or doesn't get.

| Path | How it's assembled | Entry point | Used by |
|---|---|---|---|
| **A. PromptComposer** | language banner (front) → Persona → Operation → Template → Visualization → common rules + language restatement (back) | `services/llm/prompt_composer.py` `build_system_prompt()` (~L99) | `answer_query()`, entity pages, synthesis |
| **B. BaseAgent file load** | reads `lings-desktop/Templates/Prompts/*.md`; missing file → `""` or a hardcoded fallback | `agents/base_agent.py` `_load_prompt()` (~L83) | @ling command agents (recall, counter, merge, insight, linter, tag_patrol) |
| **C. Lean mode** | caller-supplied system prompt, verbatim; **bypasses all scaffolding** unless `pin_language=True` | `services/llm_client.py` `complete()` (~L428) | the stages in §2 |

Path A layers the OUTPUT-LANGUAGE banner first AND last (a "sandwich") so an
English persona/template in the middle can't drift the output to English.
Persona and Operation are orthogonal (role vs. methodology). Path C is
deliberately bare for controlled tasks (JSON extraction, Cortex recall) that must
not inherit the Q&A document machinery.

Directory constants: `core/config.py` L239–254 (`SCRIPTURE_FILE`, `PERSONAS_DIR`,
`PROFILES_DIR`, `TEMPLATES_DIR`, `PROMPTS_DIR`, `OPERATIONS_DIR`, `GUIDELINES_DIR`).

## 2. Stage catalog (path C — lean `complete()`)

`stage=` is a trace/audit label; it does not change the LLM input. Every lean
call's language handling was audited in P4:

| stage | site | output | language handling |
|---|---|---|---|
| `artifact_table` | `services/learning_artifacts.py:274` | user-visible table | **content-language** (`_LANG_MATCH_RULE`) — follows the note, not OUTPUT_LANGUAGE, by design |
| `artifact_{kind}` | `services/learning_artifacts.py:287` | user-visible diagram | **content-language** (same) |
| `research_keywords` | `services/research_pipeline.py:190` | JSON array | none (JSON; a banner would pollute the schema) |
| `elite_digest` | `services/research_pipeline.py:249` | JSON array | none (JSON) |
| `patent_table` | `services/research_pipeline.py:367` | JSON array | none (JSON) |
| `cortex_recall` | `agents/recall_agent.py:104` | user-visible prose | **`pin_language=True`** → OUTPUT-LANGUAGE banner (P4; was hardcoded 繁體中文, now config-aware) |

All `_complete_json(...)` stages (artifact_classify, argument_map,
self_diagnosis, self_improve_edits, extract_claims, generate_structured,
adjudicate_claims, …) are strict-JSON extraction → never pinned.

`complete(pin_language=False)` is the default; only a user-visible-prose caller
with no language guarantee of its own should set it True. `cortex_recall` is the
only current user. The banner is shared via `prompt_composer.language_banner()`
(~L48), the same function `build_system_prompt()` uses.

## 3. Policy sync points

Facts stated in more than one place, kept in sync by a test:

- **Mermaid math policy** — marker `math-policy: katex-v2`. Three statements:
  1. `lings-desktop/Templates/Prompts/mermaid_rules.md` (LLM-facing, path B repair)
  2. `core/parsing/mermaid_repair.py` `_MERMAID_NON_KATEX_KINDS` (~L1967) + `repair_mermaid_latex_labels` (~L1970)
  3. `services/learning_artifacts.py` `_MERMAID_RULES_*`
  Guarded by `tests/test_prompt_assets.py::TestMermaidPolicySentinel`. Changing
  the policy = touch all three + bump the marker.
- **Required agent prompts** — the 8-file manifest lives in two places:
  `maintenance/health_check.py::required_prompts` (~L17, human readout) and
  `tests/test_prompt_assets.py::test_required_agent_prompts_exist` (~L98, the
  enforced gate). Cross-referenced; keep in sync.

## 4. Boundary: which prompts live where

- **Content / style prompts → vault files** (`Scripture/`, `Templates/`):
  hot-reloadable, editable without a deploy. Personas, operations, templates,
  agent prompts, mermaid rules.
- **Control prompts → code** (JSON-extraction schemas, `self_improve`'s
  find/replace editor, classifier options): tightly coupled to parsing/repair
  and their tests; moving them to files would reintroduce the file↔code
  divergence that F1/F3 were about. Do NOT relocate these.

A missing *required* file-based prompt (path B) is now observable: ERROR log +
`stats["missing_required_prompts"]`, and recall/counter set
`stats["used_fallback_prompt"]` when they drop to their hardcoded string (P3).

## 5. Known not-done

- **`operations` field** (Profiles) — defined and ref-checked, but not consumed
  until the Phase 6 Planner (F7). See `Scripture/Profiles/_README.md`.
- **Lean stages left unpinned** — all except `cortex_recall`, for the reasons in
  §2 (content-language or JSON). Revisit if a new user-visible lean prose stage
  appears.
- **`coder` persona** — kept (valid manual `be_a:` selection) but not yet wired
  to real work. Planned next: code review, architecture docs, flowchart/state
  diagrams. Give it a profile and/or dedicated capabilities when that lands.
