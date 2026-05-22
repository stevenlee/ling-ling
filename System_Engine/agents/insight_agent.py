import logging
import random
import re
import yaml
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import SKILLS_DIR, WIKI_VAULT_DIR
from core.parser import extract_json_array, extract_json_object


_WIKILINK_RE = re.compile(r'\[\[(.*?)\]\]')
_HASHTAG_RE = re.compile(r'#([^\s#]+)')
_SKILL_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


class InsightAgent(BaseAgent):
    """Generate insights from the knowledge base.

    Two pipelines:
      - 'single':     One-shot LLM call with strategy-specific context.
      - 'montecarlo': Multi-round explore → score → filter → expand → synthesize.
    """

    TEMP_SPARK = 0.9
    TEMP_EXPAND = 0.5
    TEMP_SYNTHESIZE = 0.3

    def __init__(self, llm, rag_manager):
        super().__init__(llm, rag_manager)
        self.insights_dir = WIKI_VAULT_DIR / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()

    def _load_strategies(self) -> dict:
        if not self.skills_dir.exists():
            return {}

        strategies: dict = {}
        for filepath in self.skills_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding="utf-8")
                match = _SKILL_FRONTMATTER_RE.search(content)
                if not match:
                    continue
                yaml_data = yaml.safe_load(match.group(1))
                if not isinstance(yaml_data, dict) or "name" not in yaml_data:
                    continue
                yaml_data["system_prompt"] = content[match.end():].strip()
                strategies[yaml_data["name"]] = yaml_data
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    # ── Public Entry Points ─────────────────────────────────────────────

    def execute(self, task_context: dict) -> str:
        strategy_id = task_context.get("strategy_id", "recency")
        user_directive = task_context.get("user_directive", "")
        is_full_report = task_context.get("is_full_report", False)
        forced_template = task_context.get("forced_template")

        if is_full_report:
            return self.generate_full_insight(user_directive, forced_template=forced_template)
        return self.generate_insight(strategy_id, user_directive, forced_template=forced_template)

    def generate_insight(self, strategy_id: str, user_directive: str = "", forced_template: str | None = None) -> str:
        if strategy_id not in self.strategies:
            if not self.strategies:
                return "❌ Error: No strategies found."
            strategy_id = random.choice(list(self.strategies.keys()))

        config = self.strategies[strategy_id]
        pipeline = config.get("pipeline", "single")
        resolved_template = forced_template or config.get("template")

        if pipeline == "montecarlo":
            report_content = self._run_montecarlo(config, user_directive, resolved_template)
        else:
            report_content = self._run_single(config, user_directive, resolved_template)

        meta = {
            "exercise_strategy": strategy_id,
            "exercise_name": config["name"],
            "exercise_description": config["description"],
            "pipeline": pipeline,
        }
        _, full_markdown = self._write_report(
            f"洞察分析-{config['name']}", report_content, "report_insight", meta
        )
        self._mirror_to_insights(full_markdown, prefix="🎐insight")
        return full_markdown

    def generate_full_insight(self, user_directive: str = "", forced_template: str | None = None) -> str:
        """Run all strategies, then perform a cross-strategy synthesis."""
        section_results = []
        insight_seeds = []

        for strategy_id, config in self.strategies.items():
            pipeline = config.get("pipeline", "single")
            resolved_template = forced_template or config.get("template")

            if pipeline == "montecarlo":
                section_content = self._run_montecarlo(config, user_directive, resolved_template)
            else:
                section_content = self._run_single(config, user_directive, resolved_template)

            section_results.append(f"## 📌 分析維度：{config['name']}\n\n{section_content}")
            insight_seeds.extend(self._extract_seeds_from_section(section_content, config["name"]))

        cross_synthesis = self._cross_strategy_synthesis(insight_seeds, user_directive)
        sections_joined = "\n\n---\n\n".join(section_results)

        final_markdown = (
            f"# 🎀 Ling Ling 的練習本 (Full Report)\n\n"
            f"## 🔮 跨維度綜合洞察 (Cross-Strategy Synthesis)\n\n{cross_synthesis}\n\n---\n\n"
            f"{sections_joined}"
        )

        _, full_markdown = self._write_report("全方位洞察報告", final_markdown, "report_insight_full")
        self._mirror_to_insights(full_markdown, prefix="🎐full-insight")
        return full_markdown

    def _mirror_to_insights(self, full_markdown: str, prefix: str) -> None:
        """Drop a byte-identical copy of the canonical report in Insights/.

        We re-write the same full markdown (frontmatter + body) that
        `_write_report` just wrote to FROM_LLM_DIR, so the Insights/ copy
        stays indexable in Obsidian with the full title/type/version/stats
        frontmatter.
        """
        insight_file = self.insights_dir / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        insight_file.write_text(full_markdown, encoding="utf-8")

    # ── Pipeline: Single-Shot ────────────────────────────────────────

    def _run_single(self, config: dict, user_directive: str, resolved_template: str | None = None) -> str:
        selection = config.get("selection", {})
        method = config.get("method") or selection.get("method", "random")
        limit = config.get("limit") or selection.get("limit", 10)

        context = self._get_context_by_method(method, limit, user_directive)
        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")

        custom_task = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"## 分析指令\n{config.get('system_prompt', 'Analyze this.')}\n\n"
            f"## 知識背景\n{context}"
        )

        return self.llm.answer_query(
            query_content=(
                "根據設定的策略進行深度分析。\n"
                f"使用者額外補充：{user_directive if user_directive else '無'}"
            ),
            wiki_context="",
            custom_instruction=custom_task,
            forced_template=resolved_template,
            default_template="insight-rpt",
        )

    # ── Pipeline: Monte Carlo ────────────────────────────────────────

    def _run_montecarlo(self, config: dict, user_directive: str, resolved_template: str | None = None) -> str:
        num_sparks = config.get("num_sparks", 6)
        top_k = config.get("top_k", 3)
        num_rounds = config.get("num_rounds", 3)
        limit = config.get("limit", 10)

        from core.ui import ui

        target_titles = [m.split("|")[0].strip() for m in _WIKILINK_RE.findall(user_directive)]

        # Fetch the full metadata table ONCE up front. Both _get_all_documents
        # and _resolve_target_doc need it; previously each call re-issued the
        # same scan, which dominated runtime on large vaults.
        title_meta = self._fetch_all_title_meta()
        all_docs = self._get_all_documents(limit * 5, title_meta=title_meta)
        if len(all_docs) < 2:
            logging.warning("Monte Carlo: not enough documents for pairing, falling back to single.")
            return self._run_single(config, user_directive, resolved_template)

        tried_pairs: set[tuple[str, str]] = set()
        round_results: list[dict] = []

        for round_num in range(1, num_rounds + 1):
            ui.set_status(f"Monte Carlo Round {round_num}/{num_rounds}: Generating pairs...")
            logging.info(f"Monte Carlo: starting round {round_num}/{num_rounds}")

            if target_titles:
                pairs = self._build_targeted_pairs(
                    all_docs, target_titles, num_sparks,
                    exclude=tried_pairs, title_meta=title_meta,
                )
            else:
                pairs = self._sample_random_pairs(all_docs, num_sparks, exclude=tried_pairs)

            if not pairs:
                logging.info(f"Monte Carlo round {round_num}: no new pairs, stopping.")
                break

            for a, b in pairs:
                tried_pairs.add(tuple(sorted([a["title"], b["title"]])))

            seeds = self._spark_pairs(pairs, config, round_num, ui)
            if not seeds:
                logging.info(f"Monte Carlo round {round_num}: no seeds generated.")
                round_results.append({
                    "round": round_num, "pairs_tried": len(pairs),
                    "seeds": 0, "winners": [], "expanded": [],
                })
                continue

            seeds.sort(key=lambda s: s.get("novelty_score", 0), reverse=True)
            winners = seeds[:top_k]
            expanded = self._expand_winners(winners, config, round_num, ui)

            round_results.append({
                "round": round_num,
                "pairs_tried": len(pairs),
                "seeds": len(seeds),
                "winners": winners,
                "expanded": expanded,
                "all_scores": [s.get("novelty_score", 0) for s in seeds],
            })

            logging.info(
                f"Monte Carlo round {round_num}: {len(pairs)} pairs → "
                f"{len(seeds)} seeds → top {len(winners)} "
                f"(scores: {[s.get('novelty_score', 0) for s in winners]})"
            )

        if not any(r.get("expanded") for r in round_results):
            logging.warning("Monte Carlo: no insights from any round, falling back to single.")
            return self._run_single(config, user_directive, resolved_template)

        ui.set_status("Monte Carlo: cross-round evaluation & synthesis...")
        return self._synthesize_multi_round(round_results, config, user_directive, resolved_template)

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

    def _fetch_all_title_meta(self) -> dict[str, dict]:
        """Single metadata scan: title → first-seen metadata dict.

        Used by both `_get_all_documents` and `_resolve_target_doc` so a
        Monte Carlo run with N targets doesn't issue N+1 full scans.
        """
        try:
            results = self.rag.collection.get(include=["metadatas"])
            metadatas = results.get("metadatas", []) or []
        except Exception as e:
            logging.error(f"Monte Carlo: failed to fetch metadata: {e}")
            return {}

        out: dict[str, dict] = {}
        for meta in metadatas:
            title = (meta or {}).get("title", "Unknown")
            out.setdefault(title, meta)
        return out

    def _get_all_documents(self, max_docs: int = 50, title_meta: dict | None = None) -> list[dict]:
        """Get up to max_docs unique-by-title docs, each with one random chunk.

        Two-phase approach avoids chunk bias: we first discover unique titles
        (no chunk content fetched), then fetch one representative chunk per
        title.
        """
        if title_meta is None:
            title_meta = self._fetch_all_title_meta()
        if not title_meta:
            return []

        unique_titles = list(title_meta.keys())
        if len(unique_titles) > max_docs:
            unique_titles = random.sample(unique_titles, max_docs)

        docs = []
        for title in unique_titles:
            doc = self._doc_from_rag_title(
                title,
                tags=self._parse_stored_tags(title_meta[title].get("tags", "")),
            )
            if doc:
                docs.append(doc)

        logging.info(f"Monte Carlo: discovered {len(title_meta)} unique titles, loaded {len(docs)} docs")
        return docs

    def _doc_from_rag_title(self, title: str, tags: list[str] | None = None) -> dict | None:
        """Fetch one representative chunk for an exact indexed title."""
        try:
            chunk_results = self.rag.collection.get(
                where={"title": title},
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logging.debug(f"Monte Carlo: failed to fetch chunk for '{title}': {e}")
            return None

        chunk_docs = chunk_results.get("documents", []) or []
        if not chunk_docs:
            return None
        metadatas = chunk_results.get("metadatas", []) or []
        if tags is None:
            tags = self._parse_stored_tags((metadatas[0] if metadatas else {}).get("tags", ""))
        return {
            "title": title,
            "content": random.choice(chunk_docs)[:2000],
            "tags": tags,
        }

    # ── Sampling ─────────────────────────────────────────────────────

    def _sample_random_pairs(self, docs: list[dict], num_pairs: int, exclude: set | None = None) -> list[tuple]:
        pairs = []
        exclude = exclude or set()
        max_attempts = num_pairs * 4

        for attempt in range(1, max_attempts + 1):
            if len(pairs) >= num_pairs:
                break
            a, b = random.sample(docs, 2)
            key = tuple(sorted([a["title"], b["title"]]))
            if key in exclude:
                continue

            tags_a = set(a.get("tags", []))
            tags_b = set(b.get("tags", []))
            overlap = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)

            if overlap < 0.3 or attempt > num_pairs * 2:
                pairs.append((a, b))

        return pairs

    @staticmethod
    def _normalize_title(title: str) -> str:
        return (title or "").split("|", 1)[0].strip().lower()

    def _target_match_score(self, requested_title: str, candidate_title: str) -> int:
        requested = self._normalize_title(requested_title)
        candidate = self._normalize_title(candidate_title)
        if not requested or not candidate:
            return 0
        if candidate == requested:
            return 100
        if candidate == f"{requested} (stitched)":
            return 95
        if candidate == f"{requested} (synthesis)":
            return 90
        if requested in candidate or candidate in requested:
            return 60
        return 0

    def _resolve_target_doc(
        self,
        requested_title: str,
        all_docs: list[dict],
        title_meta: dict | None = None,
    ) -> dict | None:
        """Resolve one [[target]] to a representative document.

        Prefer exact / Stitched / Synthesis matches across the full RAG index,
        not only the sampled pool — otherwise a requested book that wasn't
        sampled gets silently dropped.
        """
        best_doc, best_score = None, 0

        for doc in all_docs:
            score = self._target_match_score(requested_title, doc.get("title", ""))
            if score > best_score:
                best_doc, best_score = doc, score

        if title_meta is None:
            title_meta = self._fetch_all_title_meta()

        for title, meta in title_meta.items():
            score = self._target_match_score(requested_title, title)
            if score > best_score:
                best_score = score
                best_doc = self._doc_from_rag_title(
                    title,
                    tags=self._parse_stored_tags((meta or {}).get("tags", "")),
                )

        if best_doc:
            logging.info(f"Monte Carlo: target '{requested_title}' resolved to '{best_doc['title']}'")
            return best_doc

        try:
            similar = self.rag.query_similar_notes(requested_title, top_k=1)
        except Exception as e:
            logging.debug(f"Monte Carlo: semantic search for '{requested_title}' failed: {e}")
            similar = []

        if similar:
            logging.info(f"Monte Carlo: target '{requested_title}' resolved via semantic search")
            return {"title": requested_title, "content": similar[0][:2000], "tags": []}

        logging.warning(f"Monte Carlo: target '{requested_title}' not found.")
        return None

    def _build_targeted_pairs(
        self,
        all_docs: list[dict],
        target_titles: list[str],
        num_pairs: int,
        exclude: set | None = None,
        title_meta: dict | None = None,
    ) -> list[tuple]:
        exclude = exclude or set()

        target_docs = []
        seen_target_titles: set[str] = set()
        for title in target_titles:
            doc = self._resolve_target_doc(title, all_docs, title_meta=title_meta)
            if doc and doc["title"] not in seen_target_titles:
                target_docs.append(doc)
                seen_target_titles.add(doc["title"])

        if not target_docs:
            logging.warning(f"Monte Carlo: targets {target_titles} not found, falling back to random.")
            return self._sample_random_pairs(all_docs, num_pairs, exclude=exclude)

        target_title_set = {doc["title"] for doc in target_docs}
        other_docs = [
            doc for doc in all_docs
            if doc["title"] not in target_title_set
            and not any(self._target_match_score(t, doc["title"]) for t in target_titles)
        ]

        pairs: list[tuple] = []

        if len(target_docs) >= 2:
            all_combos = list(combinations(target_docs, 2))
            random.shuffle(all_combos)
            for a, b in all_combos:
                if tuple(sorted([a["title"], b["title"]])) in exclude:
                    continue
                pairs.append((a, b))
                if len(pairs) >= num_pairs:
                    break

            if other_docs and len(pairs) < num_pairs:
                shuffled_targets = list(target_docs)
                random.shuffle(shuffled_targets)
                for target in shuffled_targets:
                    if len(pairs) >= num_pairs:
                        break
                    neighbor = random.choice(other_docs)
                    if tuple(sorted([target["title"], neighbor["title"]])) not in exclude:
                        pairs.append((target, neighbor))
        else:
            target = target_docs[0]
            if other_docs:
                candidates = random.sample(other_docs, min(len(other_docs), num_pairs * 2))
                for other in candidates:
                    if len(pairs) >= num_pairs:
                        break
                    if tuple(sorted([target["title"], other["title"]])) not in exclude:
                        pairs.append((target, other))
            if not pairs:
                pairs.append((target_docs[0], random.choice(all_docs)))

        return pairs[:num_pairs]

    # ── Spark / Expand ───────────────────────────────────────────────

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
            raw = self.llm.answer_query(
                query_content=user_msg,
                wiki_context="",
                custom_instruction=system_prompt,
                temperature=self.TEMP_SPARK,
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

    def _expand_seed(self, seed: dict, config: dict) -> dict:
        idea = seed.get("idea", "")
        try:
            evidence_docs = self.rag.query_similar_notes(idea, top_k=5)
        except Exception as e:
            logging.debug(f"Monte Carlo: evidence search failed: {e}")
            evidence_docs = []
        evidence_context = "\n\n".join(evidence_docs) if evidence_docs else "(No supporting evidence found.)"

        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")

        expand_prompt = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"## 任務\n"
            f"You are developing a winning insight seed into a full analysis.\n\n"
            f"## Seed Insight\n"
            f"**Idea**: {idea}\n"
            f"**Novelty Score**: {seed.get('novelty_score', '?')}/10\n"
            f"**Reasoning**: {seed.get('reasoning', '')}\n"
            f"**Sources**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
            f"## Supporting Evidence (from semantic search)\n{evidence_context}\n\n"
            f"## Instructions\n"
            f"Develop this seed into a structured analysis section with:\n"
            f"1. A clear thesis statement grounded in the source notes\n"
            f"2. Key arguments supported by evidence from the knowledge base\n"
            f"3. Practical implications and actionable takeaways\n"
            f"4. A Mermaid diagram if it adds clarity\n"
            f"Cite source notes using [[title]] notation."
        )

        expansion_text = self.llm.answer_query(
            query_content=f"Expand this insight seed: {idea}",
            wiki_context="",
            custom_instruction=expand_prompt,
            temperature=self.TEMP_EXPAND,
        )

        return {
            **seed,
            "expanded": expansion_text,
            "evidence_sources": [doc.split("\n")[0] for doc in evidence_docs[:3]] if evidence_docs else [],
        }

    # ── Multi-round synthesis ────────────────────────────────────────

    def _synthesize_multi_round(
        self,
        round_results: list[dict],
        config: dict,
        user_directive: str,
        resolved_template: str | None = None,
    ) -> str:
        num_rounds = len(round_results)
        scorecard = self._build_scorecard(round_results)
        round_sections, all_expanded = self._build_round_sections(round_results)
        evaluation = self._cross_round_evaluation(
            scorecard, all_expanded, num_rounds, user_directive, resolved_template
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
            f"## 🔬 Per-Round Details\n\n"
            + "\n\n---\n\n".join(round_sections)
        )

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
            "|:-----:|:-----:|:-----:|:---------:|:----:|:---------------|\n"
            + "\n".join(rows)
        )

    @staticmethod
    def _build_round_sections(round_results: list[dict]) -> tuple[list[str], list[dict]]:
        round_sections = []
        all_expanded: list[dict] = []
        for r in round_results:
            expanded = r.get("expanded", [])
            if not expanded:
                round_sections.append(f"### Round {r['round']}\n\n_(No insights generated this round.)_")
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
            f"4. Identifies meta-patterns that emerged across rounds\n"
            f"5. Gives 2-3 concrete action items for the knowledge base owner\n\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User context: {user_directive or '(none)'}"
        )
        return self.llm.answer_query(
            query_content="Evaluate the multi-round Monte Carlo exploration.",
            wiki_context="",
            custom_instruction=eval_prompt,
            temperature=self.TEMP_SYNTHESIZE,
            forced_template=resolved_template,
            default_template="insight-rpt",
        )

    # ── Cross-Strategy Synthesis ────────────────────────────────────

    def _extract_seeds_from_section(self, section_content: str, strategy_name: str) -> list[dict]:
        extract_prompt = (
            "Extract the 2-3 most important insight claims from this analysis section.\n"
            "Return a JSON array of objects: [{\"claim\": \"...\", \"strategy\": \"...\"}]\n"
            "Each claim should be a single declarative sentence."
        )
        try:
            raw = self.llm.answer_query(
                query_content=section_content[:3000],
                wiki_context="",
                custom_instruction=extract_prompt,
                temperature=0.1,
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
            f"- [{s.get('strategy', '?')}] {s.get('claim', '?')}"
            for s in all_seeds[:15]
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
            temperature=self.TEMP_SYNTHESIZE,
        )

    # ── Context Retrieval ────────────────────────────────────────────

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        if file_matches := _WIKILINK_RE.findall(user_directive):
            target_file = file_matches[0].split("|")[0].strip()
            if target_file.lower().endswith(".md"):
                target_file = target_file[:-3]
        if tag_matches := _HASHTAG_RE.findall(user_directive):
            target_tag = tag_matches[0]

        if method == "recency":
            return self._get_recent_context(limit)
        if method == "tags":
            return self._get_tag_cluster_context(limit, target_tag)
        if method == "islands":
            return self._get_island_context(limit, target_file)
        return self._get_random_sample_context(limit, target_file)

    def _get_recent_context(self, limit: int) -> str:
        try:
            results = self.rag.collection.get(include=["metadatas", "documents"])
            if not results.get("documents"):
                return "No documents found."
            docs_with_meta = list(zip(results["documents"], results["metadatas"]))
            docs_with_meta.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
            pool_size = min(len(docs_with_meta), limit * 3)
            selection = random.sample(docs_with_meta[:pool_size], min(pool_size, limit))
            return "\n---\n".join(x[0] for x in selection)
        except Exception as e:
            logging.debug(f"InsightAgent: recent context retrieval failed: {e}")
            return "No recent data found."

    def _get_tag_cluster_context(self, limit: int, target_tag: str | None = None) -> str:
        try:
            results = self.rag.collection.get(include=["metadatas", "documents"])
            if not results.get("metadatas"):
                return self._get_random_sample_context(limit)

            if not target_tag:
                tag_counts: Counter = Counter()
                for meta in results["metadatas"]:
                    tag_counts.update(self._parse_stored_tags(meta.get("tags", "")))
                if not tag_counts:
                    return self._get_random_sample_context(limit)
                interesting = [t for t, c in tag_counts.items() if c >= 2]
                target_tag = random.choice(interesting if interesting else list(tag_counts))

            cluster_docs = [
                doc for doc, meta in zip(results["documents"], results["metadatas"])
                if target_tag in self._parse_stored_tags(meta.get("tags", ""))
            ]
            if not cluster_docs:
                return self._get_random_sample_context(limit)
            selection = random.sample(cluster_docs, min(len(cluster_docs), limit))
            return f"Focusing on Cluster: #{target_tag}\n\n" + "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: tag cluster retrieval failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str | None = None) -> str:
        if target_island:
            try:
                results = self.rag.collection.get(where={"title": target_island}, limit=limit)
            except Exception as e:
                logging.debug(f"InsightAgent: targeted island fetch failed: {e}")
                results = {}
            docs = results.get("documents", []) if results else []
            if docs:
                return f"Analysis target (Knowledge Island): [[{target_island}]]\n\n" + "\n---\n".join(docs)

        try:
            results = self.rag.collection.get(include=["metadatas", "documents"])
            if not results.get("documents"):
                return self._get_random_sample_context(limit)

            all_docs_meta = list(zip(results["documents"], results["metadatas"]))
            tag_to_titles: dict[str, set[str]] = {}
            title_to_entry: dict[str, tuple[str, list[str]]] = {}

            for doc, meta in all_docs_meta:
                title = meta.get("title", "Unknown")
                tags = self._parse_stored_tags(meta.get("tags", ""))
                title_to_entry[title] = (doc, tags)
                for tag in tags:
                    tag_to_titles.setdefault(tag, set()).add(title)

            connectivity = {}
            for title, (_, tags) in title_to_entry.items():
                connected = set()
                for tag in tags:
                    connected.update(tag_to_titles.get(tag, set()))
                connected.discard(title)
                connectivity[title] = len(connected)

            isolated = sorted(connectivity, key=connectivity.get)
            island_titles = isolated[:limit]
            if not island_titles:
                return self._get_random_sample_context(limit)

            island_docs = []
            for title in island_titles:
                if title in title_to_entry:
                    doc, tags = title_to_entry[title]
                    island_docs.append(
                        f"### 🏝️ [[{title}]] (connectivity: {connectivity[title]})\n"
                        f"Tags: {', '.join(tags) if tags else '(none)'}\n\n{doc}"
                    )
            return "Knowledge Islands Detected (lowest connectivity scores):\n\n" + "\n---\n".join(island_docs)
        except Exception as e:
            logging.debug(f"InsightAgent: island detection failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_random_sample_context(self, limit: int, target_file: str | None = None) -> str:
        try:
            if target_file:
                results = self.rag.collection.get(where={"title": target_file})
                docs = results.get("documents", [])
                if docs:
                    return f"Analysis target: [[{target_file}]]\n\n" + "\n---\n".join(docs)
            results = self.rag.collection.get()
            docs = results.get("documents", [])
            if not docs:
                return "Empty KB."
            selection = random.sample(docs, min(len(docs), limit))
            return "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: random sample retrieval failed: {e}")
            return "Error retrieving context."

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_stored_tags(tags_str: str) -> list[str]:
        """Parse the ',tag1,tag2,tag3,' format used in ChromaDB metadata."""
        if not tags_str:
            return []
        return [t.strip() for t in tags_str.split(",") if t.strip()]
