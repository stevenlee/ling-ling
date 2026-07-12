"""Multi-round explore -> score -> expand -> synthesize pipeline (spark/expand/grounding/cross-round/cross-strategy).

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging

from core.parser import extract_json_array, extract_json_object

from agents.insight.common import (
    _WIKILINK_RE,
)


class MonteCarloMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    llm: Any
    rag: Any
    strategies: dict
    _load_prompt: Any
    _pair_key: Any
    _fetch_all_title_meta: Any
    _get_all_documents: Any
    _sample_random_pairs: Any
    _build_targeted_pairs: Any
    _run_single: Any
    TEMP_SPARK: float
    TEMP_EXPAND: float
    TEMP_SYNTHESIZE: float
    _temp_spark: float
    _temp_expand: float
    _temp_synthesize: float

    @staticmethod
    def _operation_lens(config: dict | None) -> str:
        """The skill file's `# System Prompt` body, injected into all three
        pipeline stages as the strategy-specific lens. This is what makes two
        montecarlo-pipeline skills (e.g. counterfactual vs fable) produce
        differently-shaped output instead of converging on one template."""
        lens = ((config or {}).get("system_prompt") or "").strip()
        if not lens:
            return ""
        return f"\n\n## Operation Lens (strategy-specific)\n{lens}"

    @staticmethod
    def _is_lean(config: dict | None) -> bool:
        """Skill frontmatter `report_mode: creative|lean` — the operation carries
        its OWN output shape (a fable, a dialogue, a structural mapping table, a
        counterfactual stress-test), so it should NOT be wrapped in the montecarlo
        analytical scaffolding. Two effects: the expand stage follows the lens
        instead of imposing a thesis/arguments structure, and the report skips the
        scorecard / cross-round / 戰略建議 boilerplate — which is the 43% homogeni-
        zation cluster the 2026-07-12 audit found (95% of 戰略建議 was template
        fill) AND dilutes the novelty signal (~80% of a scaffolded report is
        shared boilerplate). `creative` (fable/dialogue) and `lean` (analogy/
        counterfactual) behave identically here; the two labels just document
        whether the output is an artifact or a structured analysis."""
        return ((config or {}).get("report_mode") or "").strip().lower() in ("creative", "lean")

    def _run_montecarlo(
        self, config: dict, user_directive: str, resolved_template: str | None = None
    ) -> str:
        num_sparks = config.get("num_sparks", 6)
        top_k = config.get("top_k", 3)
        num_rounds = config.get("num_rounds", 3)
        limit = config.get("limit", 10)
        chunks_per_book = config.get("chunks_per_book", 5)

        from core.ui import ui

        target_titles = [m.split("|")[0].strip() for m in _WIKILINK_RE.findall(user_directive)]

        # Fetch the full metadata table ONCE up front. Both _get_all_documents
        # and _resolve_target_doc need it; previously each call re-issued the
        # same scan, which dominated runtime on large vaults.
        title_meta = self._fetch_all_title_meta()
        all_docs = self._get_all_documents(
            limit * 5,
            chunks_per_book=chunks_per_book,
            title_meta=title_meta,
        )
        if len(all_docs) < 2:
            logging.warning(
                "Monte Carlo: not enough documents for pairing, falling back to single."
            )
            return self._run_single(config, user_directive, resolved_template)

        tried_pairs: set[tuple[str, str]] = set()
        round_results: list[dict] = []

        for round_num in range(1, num_rounds + 1):
            ui.set_status(f"Monte Carlo Round {round_num}/{num_rounds}: Generating pairs...")
            logging.info(f"Monte Carlo: starting round {round_num}/{num_rounds}")

            if target_titles:
                pairs = self._build_targeted_pairs(
                    all_docs,
                    target_titles,
                    num_sparks,
                    exclude=tried_pairs,
                    title_meta=title_meta,
                )
            else:
                pairs = self._sample_random_pairs(all_docs, num_sparks, exclude=tried_pairs)

            if not pairs:
                logging.info(f"Monte Carlo round {round_num}: no new pairs, stopping.")
                break

            for a, b in pairs:
                tried_pairs.add(self._pair_key(a, b))

            seeds = self._spark_pairs(pairs, config, round_num, ui)
            if not seeds:
                logging.info(f"Monte Carlo round {round_num}: no seeds generated.")
                round_results.append(
                    {
                        "round": round_num,
                        "pairs_tried": len(pairs),
                        "seeds": 0,
                        "winners": [],
                        "expanded": [],
                    }
                )
                continue

            seeds.sort(key=lambda s: s.get("novelty_score", 0), reverse=True)
            winners = seeds[:top_k]
            expanded = self._expand_winners(winners, config, round_num, ui)

            round_results.append(
                {
                    "round": round_num,
                    "pairs_tried": len(pairs),
                    "seeds": len(seeds),
                    "winners": winners,
                    "expanded": expanded,
                    "all_scores": [s.get("novelty_score", 0) for s in seeds],
                }
            )

            logging.info(
                f"Monte Carlo round {round_num}: {len(pairs)} pairs → "
                f"{len(seeds)} seeds → top {len(winners)} "
                f"(scores: {[s.get('novelty_score', 0) for s in winners]})"
            )

        if not any(r.get("expanded") for r in round_results):
            logging.warning("Monte Carlo: no insights from any round, falling back to single.")
            return self._run_single(config, user_directive, resolved_template)

        ui.set_status("Monte Carlo: cross-round evaluation & synthesis...")
        return self._synthesize_multi_round(
            round_results, config, user_directive, resolved_template
        )

    def _spark_pairs(self, pairs, config, round_num, ui):
        seeds = []
        for i, (doc_a, doc_b) in enumerate(pairs):
            ui.set_status(f"Round {round_num}: Sparking {i + 1}/{len(pairs)}...")
            seed = self._spark_seed(doc_a, doc_b, config)
            if seed:
                seed["round"] = round_num
                seeds.append(seed)
        return seeds

    def _expand_winners(self, winners, config, round_num, ui):
        expanded = []
        for i, seed in enumerate(winners):
            ui.set_status(f"Round {round_num}: Expanding {i + 1}/{len(winners)}...")
            expanded.append(self._expand_seed(seed, config))
        return expanded

    # ── RAG-backed retrieval ─────────────────────────────────────────

    def _spark_seed(self, doc_a: dict, doc_b: dict, config: dict) -> dict | None:
        system_prompt = (
            "You are an Epistemologist evaluating random idea combinations for novel cross-domain insights.\n"
            "Return ONLY a JSON object with this schema:\n"
            '{"idea": "2-3 sentence insight seed", "novelty_score": 1-10, '
            '"reasoning": "why this combination is interesting", '
            '"source_a": "title of note A", "source_b": "title of note B"}\n\n'
            "Scoring guide:\n"
            "- 8-10: Genuinely surprising cross-domain connection with practical implications\n"
            "- 5-7: Interesting analogy but somewhat expected\n"
            "- 1-4: Superficial or forced connection\n"
            "Be HONEST with scoring. Most random pairs deserve 3-5. Reserve 8+ for truly novel connections."
            f"{self._operation_lens(config)}"
        )
        user_msg = (
            f"## Note A: {doc_a['title']}\n"
            f"Tags: {', '.join(doc_a.get('tags', []))}\n"
            f"{doc_a['content'][:1500]}\n\n"
            f"## Note B: {doc_b['title']}\n"
            f"Tags: {', '.join(doc_b.get('tags', []))}\n"
            f"{doc_b['content'][:1500]}\n\n"
            f"Find a novel, non-obvious connection between these two knowledge fragments."
        )

        try:
            # JSON output: opt out of the template/persona axes, or the
            # default wiki-note template overrides the JSON instruction.
            raw = self.llm.answer_query(
                query_content=user_msg,
                wiki_context="",
                custom_instruction=system_prompt,
                temperature=self._temp_spark,
                forced_template="none",
                persona="none",
            )
            seed = extract_json_object(raw)
        except Exception as e:
            logging.warning(f"Monte Carlo spark failed: {e}")
            return None

        if not (seed and seed.get("idea")):
            return None

        seed.setdefault("novelty_score", 5)
        seed.setdefault("source_a", doc_a["title"])
        seed.setdefault("source_b", doc_b["title"])
        logging.info(
            f"  Spark: score={seed['novelty_score']}, "
            f"pair=({doc_a['title'][:30]} × {doc_b['title'][:30]})"
        )
        return seed

    def _should_ground(self, idea: str) -> bool:
        """Deterministically pick GROUND_FRACTION of seeds to ground — the rest
        stay cold so the echo-chamber canary has a control group. Hash-based, so
        it's reproducible and testable (not random)."""
        from core.config import CORTEX_GROUNDED_INSIGHT_ENABLED, CORTEX_GROUND_FRACTION

        if not CORTEX_GROUNDED_INSIGHT_ENABLED:
            return False
        # M4: the fraction may be auto-tuned against the echo canary; get_tuned
        # returns the config default unless AUTOTUNE_ENABLED has nudged it.
        from services.autotune_store import get_tuned

        fraction = get_tuned("CORTEX_GROUND_FRACTION", CORTEX_GROUND_FRACTION)
        import hashlib

        bucket = int(hashlib.sha256(idea.encode("utf-8")).hexdigest(), 16) % 100
        return bucket < int(round(fraction * 100))

    def _cortex_priors(self, idea: str) -> list:
        """Relevant Cortex claims to use as DIALECTICAL priors. Falsifiability-
        gated (defense 3): unfalsifiable beliefs can't be wrong, so they only
        self-reinforce — never let them anchor generation. Returns CortexPages.

        Selection is MMR-diversified, not pure top-k relevance: the central
        "從 X 轉向 Y" hub claims win every relevance race, so top-k alone grounds
        insight after insight on the same handful of claims (2026-07-12 audit:
        top-4 ids carried 88% of grounding while ~50 falsifiable claims never
        anchored anything). MMR pulls the 2nd/3rd priors toward distinct claims.
        """
        from core.config import (
            CORTEX_DIR,
            CORTEX_GROUND_MIN_FALSIFIABILITY,
            CORTEX_GROUND_MMR_LAMBDA,
            CORTEX_GROUND_TOP_K,
        )
        from services.cortex_store import load_all_pages

        falsifiable = [
            p
            for p in load_all_pages(CORTEX_DIR)
            if p.claim.strip()
            and p.status in ("active", "dormant")
            and p.falsifiability is not None
            and p.falsifiability >= CORTEX_GROUND_MIN_FALSIFIABILITY
        ]
        if len(falsifiable) <= CORTEX_GROUND_TOP_K:
            return falsifiable
        # Rank by relevance, then MMR-diversify. Restrict to the falsifiable set
        # BEFORE truncating: recall_claims ranks all active/dormant claims (it
        # doesn't apply the falsifiability gate), so a large top_k + post-filter
        # keeps defense-3 intact instead of letting unfalsifiable claims through.
        from services.cortex_recall import recall_claims, select_diverse

        fids = {p.claim_id for p in falsifiable}
        ranked = recall_claims(self.rag, idea, cortex_dir=CORTEX_DIR, top_k=10_000, min_score=0.0)
        pool = [(s, p) for s, p in ranked if p.claim_id in fids]
        if not pool:
            return falsifiable[:CORTEX_GROUND_TOP_K]
        return select_diverse(self.rag, pool, CORTEX_GROUND_TOP_K, lambda_=CORTEX_GROUND_MMR_LAMBDA)

    def _grounding_block(self, priors: list) -> str:
        lines = [
            "## 你對相關主題已有的信念（請挑戰，不要附和）",
            "",
        ]
        for p in priors:
            fz = "—" if p.falsifiability is None else f"{p.falsifiability:.2f}"
            line = f"- {p.claim.strip()}（可反駁性 {fz}"
            if p.falsifier:
                line += f"；反例：{p.falsifier}"
            lines.append(line + "）")
        lines += [
            "",
            "這份新分析在哪裡【推翻 / 修正 / 延伸】上述既有信念？最有價值的輸出是**張力與反例**，"
            "不是複述。若新材料只是附和既有信念，明說「無新增」而不要硬湊。",
            "",
        ]
        return "\n".join(lines)

    def _expand_seed(self, seed: dict, config: dict) -> dict:
        idea = seed.get("idea", "")
        try:
            evidence_docs = self.rag.query_similar_notes(idea, top_k=5)
        except Exception as e:
            logging.debug(f"Monte Carlo: evidence search failed: {e}")
            evidence_docs = []
        evidence_context = (
            "\n\n".join(evidence_docs) if evidence_docs else "(No supporting evidence found.)"
        )

        system_base = self._load_prompt("system_base.md", required=True)
        agent_instruction = self._load_prompt("agent_insight.md", required=True)

        # Cortex-grounded insight (Phase 5 F1, flag-gated, default OFF). Inject
        # relevant falsifiable claims as DIALECTICAL priors — to challenge, not
        # confirm. grounded_on records provenance for the consolidation firewall.
        grounded_on: list[str] = []
        grounding_section = ""
        if self._should_ground(idea):
            priors = self._cortex_priors(idea)
            if priors:
                grounded_on = [p.claim_id for p in priors]
                grounding_section = self._grounding_block(priors)
                # Accumulate for the report frontmatter so consolidation's
                # firewall knows which claims this insight was grounded on.
                if not hasattr(self, "_grounded_on_acc"):
                    self._grounded_on_acc = set()
                self._grounded_on_acc.update(grounded_on)

        seed_block = (
            f"## Seed Insight\n"
            f"**Idea**: {idea}\n"
            f"**Novelty Score**: {seed.get('novelty_score', '?')}/10\n"
            f"**Reasoning**: {seed.get('reasoning', '')}\n"
            f"**Sources**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
            f"## Supporting Evidence (from semantic search)\n{evidence_context}\n\n"
        )
        if self._is_lean(config):
            # Lean mode: the lens's own Expansion phase IS the instruction —
            # don't impose the generic thesis/arguments/implications scaffold
            # (that structure was overriding the lens, e.g. dialogue coming out
            # as an analytical report instead of a staged dialogue, and
            # counterfactual regressing to a standard analysis in ~40% of seeds).
            instructions = (
                "## 任務\n"
                "依下方 **Operation Lens** 的 `Expansion` 指引，把這個種子展開成它要求的"
                "**產出形態本身**（例如一則寓言、一段對話、一張結構映射表、一段反事實推演）。"
                "務必忠於來源的實際機制、用 [[title]] 標註來源；但**絕不要**寫成"
                " thesis／arguments／implications 的分析結構，也不要「戰略建議／實務影響」"
                "這類段落——那會毀掉這個 operation。\n"
            )
        else:
            instructions = (
                "## 任務\n"
                "You are developing a winning insight seed into a full analysis.\n\n"
                "## Instructions\n"
                "Develop this seed into a structured analysis section with:\n"
                "1. A clear thesis statement grounded in the source notes\n"
                "2. Key arguments supported by evidence from the knowledge base\n"
                "3. Practical implications and actionable takeaways\n"
                "4. A Mermaid diagram if it adds clarity\n"
                "Cite source notes using [[title]] notation."
            )
        expand_prompt = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"{grounding_section}"
            f"{instructions}\n"
            f"{seed_block}"
            f"{self._operation_lens(config)}"
        )

        expansion_text = self.llm.answer_query(
            query_content=f"Expand this insight seed: {idea}",
            wiki_context="",
            custom_instruction=expand_prompt,
            temperature=self._temp_expand,
        )

        return {
            **seed,
            "expanded": expansion_text,
            "evidence_sources": [doc.split("\n")[0] for doc in evidence_docs[:3]]
            if evidence_docs
            else [],
            "grounded_on": grounded_on,
        }

    # ── Multi-round synthesis ────────────────────────────────────────

    def _synthesize_multi_round(
        self,
        round_results: list[dict],
        config: dict,
        user_directive: str,
        resolved_template: str | None = None,
    ) -> str:
        # Lean operations skip the scorecard/cross-round/戰略建議 scaffolding —
        # it's tonally wrong for their output and is the audit's homogenization
        # cluster. montecarlo (the analytical operation) keeps its scorecard.
        if self._is_lean(config):
            return self._synthesize_lean(round_results, config, user_directive)

        num_rounds = len(round_results)
        scorecard = self._build_scorecard(round_results)
        round_sections, all_expanded = self._build_round_sections(round_results)
        evaluation = self._cross_round_evaluation(
            scorecard, all_expanded, num_rounds, user_directive, resolved_template, config
        )

        total_pairs = sum(r["pairs_tried"] for r in round_results)
        total_seeds = sum(r.get("seeds", 0) for r in round_results)
        total_winners = len(all_expanded)

        return (
            f"# 🎲 Monte Carlo Insight Exploration ({num_rounds} Rounds)\n\n"
            f"## 📊 Round Scorecard\n\n{scorecard}\n\n"
            f"> **Exploration scope**: {total_pairs} pairs tried → {total_seeds} seeds → "
            f"{total_winners} winners expanded across {num_rounds} rounds\n\n"
            f"---\n\n"
            f"## 🏆 Cross-Round Evaluation\n\n{evaluation}\n\n"
            f"---\n\n"
            f"## 🔬 Per-Round Details\n\n" + "\n\n---\n\n".join(round_sections)
        )

    def _synthesize_lean(self, round_results: list[dict], config: dict, user_directive: str) -> str:
        """Lean report for lean/creative operations: emit the expanded outputs
        (fables, dialogues, mapping tables, stress-tests) directly with minimal
        framing, plus one short editorial closing. No scorecard, no cross-round
        productivity analysis, no 戰略建議 template."""
        pieces = [
            seed
            for r in round_results
            for seed in r.get("expanded", [])
            if (seed.get("expanded") or "").strip()
        ]
        if not pieces:
            return "（本次未產出可用的片段。）"

        op_name = config.get("name", "insight")
        blocks = []
        for seed in pieces:
            src = f"[[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]"
            blocks.append(f"{seed.get('expanded', '').strip()}\n\n<sub>— {src}</sub>")
        body = "\n\n---\n\n".join(blocks)

        closing = self._lean_closing(pieces, config)
        header = f"# ✨ {op_name}\n\n> 本次探索產出 {len(pieces)} 則成品。\n\n"
        tail = f"\n\n---\n\n## 🧵 綜合短評\n\n{closing}" if closing else ""
        return header + body + tail

    def _lean_closing(self, pieces: list[dict], config: dict) -> str:
        """A 2-3 sentence editorial note — which piece best realized the
        operation. Deliberately NOT the analytical scorecard/strategy block."""
        listing = "\n".join(
            f"{i + 1}. [[{s.get('source_a', '?')}]] × [[{s.get('source_b', '?')}]]"
            for i, s in enumerate(pieces)
        )
        # Do NOT inject the full operation lens here: the lens carries the
        # Spark/Expansion/Synthesis phase instructions, so re-injecting it makes
        # the model re-run the whole operation (observed: the "closing note"
        # came back as another full 火花/擴張/綜合 piece). Give it only the
        # operation's name + one-line description for tone.
        prompt = (
            f"本次「{config.get('name', 'creative')}」探索"
            f"（{config.get('description', '')}）產出了 {len(pieces)} 則創作片段，"
            f"來源配對：\n{listing}\n\n"
            "用 2-3 句話寫一段編輯短評：哪一則最成功地實現了這個 operation 的意圖、為什麼。"
            "只輸出這 2-3 句話本身——不要任何標題或分節、不要條列、不要重寫作品、"
            "不要『火花／擴張／綜合／戰略建議』這類段落。\n"
            f"Output language: {self.llm._get_lang_hint()}"
        )
        try:
            note = self.llm.answer_query(
                query_content="Creative closing note.",
                wiki_context="",
                custom_instruction=prompt,
                temperature=self._temp_synthesize,
                persona="none",
                forced_template="none",
            )
        except Exception as e:
            logging.debug(f"Monte Carlo: creative closing failed: {e}")
            return ""
        # Defensive: a 2-3 sentence note has no headers — strip any the model
        # adds so it can't reintroduce section scaffolding under the closing.
        lines = [ln for ln in note.splitlines() if not ln.lstrip().startswith("#")]
        return "\n".join(lines).strip()

    @staticmethod
    def _build_scorecard(round_results: list[dict]) -> str:
        rows = []
        for r in round_results:
            if not r.get("winners"):
                rows.append(f"| {r['round']} | {r['pairs_tried']} | 0 | — | — | — |")
                continue
            top = r["winners"][0]
            scores = r.get("all_scores", [top.get("novelty_score", 0)])
            avg = sum(scores) / max(len(scores), 1)
            rows.append(
                f"| {r['round']} | {r['pairs_tried']} | {r['seeds']} | {avg:.1f} | "
                f"{top.get('novelty_score', '?')}/10 | "
                f"[[{top.get('source_a', '?')}]] × [[{top.get('source_b', '?')}]] |"
            )
        return (
            "| Round | Pairs | Seeds | Avg Score | Best | Top Connection |\n"
            "|:-----:|:-----:|:-----:|:---------:|:----:|:---------------|\n" + "\n".join(rows)
        )

    @staticmethod
    def _build_round_sections(round_results: list[dict]) -> tuple[list[str], list[dict]]:
        round_sections = []
        all_expanded: list[dict] = []
        for r in round_results:
            expanded = r.get("expanded", [])
            if not expanded:
                round_sections.append(
                    f"### Round {r['round']}\n\n_(No insights generated this round.)_"
                )
                continue

            insights = []
            for i, seed in enumerate(expanded, 1):
                insights.append(
                    f"#### 🌟 R{r['round']}-{i} (Score: {seed.get('novelty_score', '?')}/10)\n"
                    f"**Connection**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
                    f"{seed.get('expanded', seed.get('idea', ''))}"
                )
                all_expanded.append(seed)

            round_sections.append(f"### Round {r['round']}\n\n" + "\n\n---\n\n".join(insights))
        return round_sections, all_expanded

    def _cross_round_evaluation(
        self,
        scorecard: str,
        all_expanded: list[dict],
        num_rounds: int,
        user_directive: str,
        resolved_template: str | None,
        config: dict | None = None,
    ) -> str:
        winner_lines = [
            f"- [R{s.get('round', '?')}, score={s.get('novelty_score', '?')}] "
            f"({s.get('source_a', '?')} × {s.get('source_b', '?')}): {s.get('idea', '?')}"
            for s in all_expanded
        ]
        eval_prompt = (
            f"You are evaluating {num_rounds} rounds of Monte Carlo insight exploration.\n\n"
            f"## Scorecard\n{scorecard}\n\n"
            f"## All Winners Across Rounds\n" + "\n".join(winner_lines) + "\n\n"
            f"## Task\n"
            f"Write a cross-round evaluation (3-5 paragraphs) that:\n"
            f"1. Compares the quality and novelty across rounds\n"
            f"2. Identifies the single **global champion** insight and explains why it's the best\n"
            f"3. Notes which rounds were most/least productive and why\n"
            f"4. Identifies meta-patterns that emerged across rounds\n\n"
            f"Write flowing analytical prose. Do NOT emit a fixed 核心發現／語意關聯分析／"
            f"戰略建議／視覺化地圖 skeleton, and do NOT append a generic "
            f"'建立／開發／強化 ... 框架／模板／庫' action-item list — the 2026-07-12 audit "
            f"found that section was 95% interchangeable template fill across reports. "
            f"Any recommendation you make must name the specific champion insight it follows "
            f"from, or omit it.\n\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User context: {user_directive or '(none)'}"
            f"{self._operation_lens(config)}"
        )
        # default_template="none": the insight-rpt template imposed the very
        # 核心發現/語意關聯分析/戰略建議/視覺化地圖 skeleton the audit flagged;
        # a caller-forced template still wins via resolved_template.
        return self.llm.answer_query(
            query_content="Evaluate the multi-round Monte Carlo exploration.",
            wiki_context="",
            custom_instruction=eval_prompt,
            temperature=self._temp_synthesize,
            forced_template=resolved_template,
            default_template="none",
            persona="none",
            operation="synthesize",
        )

    # ── Cross-Strategy Synthesis ────────────────────────────────────

    def _extract_seeds_from_section(self, section_content: str, strategy_name: str) -> list[dict]:
        extract_prompt = (
            "Extract the 2-3 most important insight claims from this analysis section.\n"
            'Return a JSON array of objects: [{"claim": "...", "strategy": "..."}]\n'
            "Each claim should be a single declarative sentence."
        )
        try:
            # JSON output: same template/persona opt-out as the spark call.
            raw = self.llm.answer_query(
                query_content=section_content[:3000],
                wiki_context="",
                custom_instruction=extract_prompt,
                temperature=0.1,
                forced_template="none",
                persona="none",
            )
            seeds = extract_json_array(raw)
        except Exception as e:
            logging.debug(f"Seed extraction failed for {strategy_name}: {e}")
            seeds = []

        if not seeds:
            return [{"claim": section_content[:200], "strategy": strategy_name}]
        for seed in seeds:
            seed["strategy"] = strategy_name
        return seeds

    def _cross_strategy_synthesis(self, all_seeds: list[dict], user_directive: str) -> str:
        if not all_seeds:
            return "(No cross-strategy patterns detected.)"

        seed_text = "\n".join(
            f"- [{s.get('strategy', '?')}] {s.get('claim', '?')}" for s in all_seeds[:15]
        )
        synthesis_prompt = (
            f"You have key insights extracted from {len(set(s.get('strategy') for s in all_seeds))} "
            f"different analytical strategies (montecarlo, meta-methods, tag-cluster, recency, islands).\n\n"
            f"## Insight Seeds from All Strategies\n{seed_text}\n\n"
            f"## Task\n"
            f"Identify 2-3 **meta-patterns** that appear across MULTIPLE strategies.\n"
            f"These are higher-order insights that no single strategy would have found alone.\n"
            f"For each meta-pattern:\n"
            f"1. State the pattern clearly\n"
            f"2. Name which strategies contributed to it\n"
            f"3. Explain why this cross-pollination matters\n"
            f"4. Give one concrete action item\n\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User context: {user_directive or '(none)'}"
        )
        return self.llm.answer_query(
            query_content="Perform cross-strategy synthesis.",
            wiki_context="",
            custom_instruction=synthesis_prompt,
            temperature=self._temp_synthesize,
            persona="none",
            operation="synthesize",
        )

    # ── Context Retrieval ────────────────────────────────────────────
