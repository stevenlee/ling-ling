# Insight Agent Improvement Plan (Completed)

## Phase 1: Architectural Overhaul 🟢 (Completed)
- [x] Replace single-shot LLM with multi-stage Monte Carlo pipeline.
- [x] Integrate `rag.query_similar_notes()` for semantic evidence expansion.
- [x] Implement cross-strategy synthesis for `/full` reports.
- [x] Refactor Island Detection to use actual tag co-occurrence graph.

## Phase 2: Refinement & Targeted Mode 🟢 (Completed)
- [x] Add targeted mode parsing: `[[Article A]] [[Article B]]` to focus exploration.
- [x] Implement `_build_targeted_pairs` to support 2+ targets (internal combinations) and 1 target (external pairing).
- [x] Fix `_get_all_documents` chunk bias by pre-fetching unique titles via metadata.
- [x] Fix deterministic pairing bug by adding `exclude=tried_pairs` and `random.shuffle()` to targeted combos.

## Phase 3: Multi-Round Iteration 🟢 (Completed)
- [x] Introduce `num_rounds` logic to sweep internal combinations completely before branching outwards.
- [x] Generate per-round summaries showing seeds, scores, and winners.
- [x] Implement a cross-round evaluation step where the LLM picks the global champion.
- [x] Update `montecario.md` skill config to default to 3 rounds.

## Phase 4: Quality & Observability 🟢 (Completed)
- [x] Update documentation (`@ling-insight.md`) to reflect new targeted syntax.
- [x] Restore and expand the markdown parser tests (`test_parser.py`).
- [x] Integrate trailing whitespace stripping and excessive blank line collapsing into the quality checker.
- [x] Pass all regression tests (44/44 passing).

**Status:** The `@ling-insight /montecario` command is now a fully iterative, context-aware analysis engine capable of profound cross-domain synthesis.
