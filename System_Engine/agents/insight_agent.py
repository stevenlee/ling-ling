import logging
import random
import re
import yaml
from datetime import datetime
from pathlib import Path
from collections import Counter

from agents.base_agent import BaseAgent
from core.config import SKILLS_DIR, PROMPTS_DIR, WIKI_VAULT_DIR
from core.parser import extract_json_object, extract_json_array


class InsightAgent(BaseAgent):
    """
    Generates insights from the knowledge base using configurable strategies.

    Supports two pipelines:
      - 'single':     One-shot LLM call with strategy-specific context (default).
      - 'montecarlo': Multi-round explore → score → filter → expand → synthesize.
    """

    # Temperature gradient for Monte Carlo phases
    TEMP_SPARK = 0.9     # High creativity for idea generation
    TEMP_EXPAND = 0.5    # Moderate for evidence-backed expansion
    TEMP_SYNTHESIZE = 0.3  # Low for coherent final output

    def __init__(self, llm, rag_manager):
        super().__init__(llm, rag_manager)
        self.insights_dir = WIKI_VAULT_DIR / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()

    def _load_strategies(self) -> dict:
        strategies = {}
        if not self.skills_dir.exists():
            return {}

        for filepath in self.skills_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding='utf-8')
                match = re.search(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
                if match:
                    yaml_data = yaml.safe_load(match.group(1))
                    body_content = content[match.end():].strip()
                    if yaml_data and 'name' in yaml_data:
                        skill_id = yaml_data['name']
                        strategies[skill_id] = yaml_data
                        strategies[skill_id]['system_prompt'] = body_content
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    # ── Public Entry Points ──────────────────────────────────────────

    def execute(self, task_context: dict) -> str:
        strategy_id = task_context.get('strategy_id', "recency")
        user_directive = task_context.get('user_directive', "")
        is_full_report = task_context.get('is_full_report', False)

        if is_full_report:
            return self.generate_full_insight(user_directive)
        else:
            return self.generate_insight(strategy_id, user_directive)

    def generate_insight(self, strategy_id: str, user_directive: str = "") -> str:
        if strategy_id not in self.strategies:
            available = list(self.strategies.keys())
            if not available:
                return "❌ Error: No strategies found."
            strategy_id = random.choice(available)

        config = self.strategies[strategy_id]
        pipeline = config.get('pipeline', 'single')

        if pipeline == 'montecarlo':
            report_content = self._run_montecarlo(config, user_directive)
        else:
            report_content = self._run_single(config, user_directive)

        # Self-correct content (e.g. Mermaid)
        report_content = self._self_correct(report_content)

        # Write report
        meta = {
            "exercise_strategy": strategy_id,
            "exercise_name": config['name'],
            "exercise_description": config['description'],
            "pipeline": pipeline
        }
        output_path = self._write_report(
            f"洞察分析-{config['name']}", report_content, "report_insight", meta
        )

        # Copy to Insights folder for Obsidian
        insight_file = self.insights_dir / f"🎐insight-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        insight_file.write_text(output_path.read_text(encoding='utf-8'), encoding='utf-8')

        return report_content

    def generate_full_insight(self, user_directive: str = "") -> str:
        """Run all strategies, then perform a cross-strategy synthesis."""
        section_results = []
        insight_seeds = []

        for strategy_id, config in self.strategies.items():
            pipeline = config.get('pipeline', 'single')

            if pipeline == 'montecarlo':
                section_content = self._run_montecarlo(config, user_directive)
            else:
                section_content = self._run_single(config, user_directive)

            section_results.append(f"## 📌 分析維度：{config['name']}\n\n{section_content}")

            # Extract seeds for cross-strategy synthesis
            seeds = self._extract_seeds_from_section(section_content, config['name'])
            insight_seeds.extend(seeds)

        # Cross-strategy synthesis pass
        cross_synthesis = self._cross_strategy_synthesis(insight_seeds, user_directive)

        sections_joined = "\n\n---\n\n".join(section_results)
        sections_joined = self._self_correct(sections_joined)

        final_markdown = (
            f"# 🎀 Ling Ling 的練習本 (Full Report)\n\n"
            f"## 🔮 跨維度綜合洞察 (Cross-Strategy Synthesis)\n\n{cross_synthesis}\n\n---\n\n"
            f"{sections_joined}"
        )

        output_path = self._write_report("全方位洞察報告", final_markdown, "report_insight_full")

        insight_file = self.insights_dir / f"🎐full-insight-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        insight_file.write_text(output_path.read_text(encoding='utf-8'), encoding='utf-8')

        return final_markdown

    # ── Pipeline: Single-Shot ────────────────────────────────────────

    def _run_single(self, config: dict, user_directive: str) -> str:
        """Original single-shot pipeline: context → one LLM call → report."""
        selection = config.get('selection', {})
        method = config.get('method') or selection.get('method', 'random')
        limit = config.get('limit') or selection.get('limit', 10)

        context = self._get_context_by_method(method, limit, user_directive)

        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")

        custom_task = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"## 分析指令\n{config.get('system_prompt', 'Analyze this.')}\n\n"
            f"## 知識背景\n{context}"
        )

        return self.llm.answer_query(
            query_content=f"根據設定的策略進行深度分析。\n使用者額外補充：{user_directive if user_directive else '無'}",
            wiki_context="",
            custom_instruction=custom_task
        )

    # ── Pipeline: Monte Carlo ────────────────────────────────────────

    def _run_montecarlo(self, config: dict, user_directive: str) -> str:
        """
        Multi-round Monte Carlo pipeline:
          1. GENERATE: Sample N random note pairs from the KB
          2. SPARK:    For each pair, LLM generates a seed insight + novelty score (high temp)
          3. FILTER:   Keep top-K seeds by novelty score
          4. EXPAND:   Deep-dive winners with semantic search for evidence (medium temp)
          5. SYNTHESIZE: Final LLM pass weaving all expanded seeds (low temp)

        Supports targeted mode:
          - 2+ [[titles]]: Focus exploration around those specific articles
          - 1 [[title]]:   Pair that article with diverse KB documents
          - 0 [[titles]]:  Fully random exploration (default)
        """
        num_sparks = config.get('num_sparks', 6)
        top_k = config.get('top_k', 3)
        limit = config.get('limit', 10)

        # Parse [[target]] references from user directive
        target_titles = [
            m.split('|')[0].strip()
            for m in re.findall(r'\[\[(.*?)\]\]', user_directive)
        ]

        # 1. GENERATE: Build pairs based on targeting mode
        all_docs = self._get_all_documents(limit * 3)
        if len(all_docs) < 2:
            logging.warning("Monte Carlo: Not enough documents for pairing, falling back to single.")
            return self._run_single(config, user_directive)

        if target_titles:
            pairs = self._build_targeted_pairs(all_docs, target_titles, num_sparks)
            logging.info(f"Monte Carlo (targeted): {len(pairs)} pairs around {target_titles}")
        else:
            pairs = self._sample_random_pairs(all_docs, num_sparks)
            logging.info(f"Monte Carlo (random): {len(pairs)} pairs from {len(all_docs)} docs")

        # 2. SPARK: Generate and score seed insights
        from core.ui import ui
        seeds = []
        for i, (doc_a, doc_b) in enumerate(pairs):
            ui.set_status(f"Monte Carlo: Sparking seed {i+1}/{len(pairs)}...")
            seed = self._spark_seed(doc_a, doc_b, config)
            if seed:
                seeds.append(seed)

        if not seeds:
            logging.warning("Monte Carlo: No seeds generated, falling back to single.")
            return self._run_single(config, user_directive)

        # 3. FILTER: Keep top-K by novelty score
        seeds.sort(key=lambda s: s.get('novelty_score', 0), reverse=True)
        winners = seeds[:top_k]
        logging.info(
            f"Monte Carlo: Filtered {len(seeds)} seeds → top {len(winners)} "
            f"(scores: {[s.get('novelty_score', 0) for s in winners]})"
        )

        # 4. EXPAND: Deep-dive winners with semantic search
        expanded = []
        for i, seed in enumerate(winners):
            ui.set_status(f"Monte Carlo: Expanding seed {i+1}/{len(winners)}...")
            expansion = self._expand_seed(seed, config)
            expanded.append(expansion)

        # 5. SYNTHESIZE: Final cohesive report
        ui.set_status("Monte Carlo: Synthesizing final report...")
        return self._synthesize_report(expanded, config, user_directive)

    def _get_all_documents(self, max_docs: int = 50) -> list[dict]:
        """
        Retrieve unique documents from the KB for pairing.

        Two-phase approach to avoid chunk bias:
          1. Fetch ALL metadata to discover unique titles.
          2. For each unique title, fetch one representative chunk.
        This prevents a single long document's chunks from crowding out
        the entire result set.
        """
        try:
            # Phase 1: Discover all unique titles via lightweight metadata fetch
            meta_results = self.rag.collection.get(include=['metadatas'])
            all_meta = meta_results.get('metadatas', [])
            if not all_meta:
                return []

            # Build a map of title → first-seen metadata (for tags, etc.)
            title_meta = {}
            for meta in all_meta:
                title = meta.get('title', 'Unknown')
                if title not in title_meta:
                    title_meta[title] = meta

            # If we have more titles than max_docs, sample randomly
            unique_titles = list(title_meta.keys())
            if len(unique_titles) > max_docs:
                unique_titles = random.sample(unique_titles, max_docs)

            # Phase 2: Fetch a RANDOM chunk per title (not always chunk 0)
            docs = []
            for title in unique_titles:
                try:
                    chunk_results = self.rag.collection.get(
                        where={"title": title},
                        include=['documents'],
                    )
                    chunk_docs = chunk_results.get('documents', [])
                    if chunk_docs:
                        chosen = random.choice(chunk_docs)
                        meta = title_meta[title]
                        docs.append({
                            'title': title,
                            'content': chosen[:2000],
                            'tags': self._parse_stored_tags(meta.get('tags', '')),
                        })
                except Exception as e:
                    logging.debug(f"Monte Carlo: Failed to fetch chunk for '{title}': {e}")

            logging.info(f"Monte Carlo: Discovered {len(title_meta)} unique titles, loaded {len(docs)} docs")
            return docs
        except Exception as e:
            logging.error(f"Monte Carlo: Failed to get documents: {e}")
            return []

    def _sample_random_pairs(self, docs: list[dict], num_pairs: int) -> list[tuple]:
        """Sample random pairs of documents, preferring cross-domain (different tags)."""
        pairs = []
        attempts = 0
        max_attempts = num_pairs * 4

        while len(pairs) < num_pairs and attempts < max_attempts:
            attempts += 1
            a, b = random.sample(docs, 2)

            # Prefer pairs with low tag overlap (cross-domain = more interesting)
            tags_a = set(a.get('tags', []))
            tags_b = set(b.get('tags', []))
            overlap = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)

            # Accept if: cross-domain (overlap < 0.3) or we're running out of attempts
            if overlap < 0.3 or attempts > num_pairs * 2:
                pairs.append((a, b))

        return pairs

    def _build_targeted_pairs(self, all_docs: list[dict], target_titles: list[str], num_pairs: int) -> list[tuple]:
        """
        Build pairs focused around specific target articles.

        Modes:
          - 2+ targets: All combinations between targets + pair each with semantic neighbors
          - 1 target:   Pair the target with diverse docs from the KB
        """
        # Find target docs by title (fuzzy match — stem matching for flexibility)
        target_docs = []
        other_docs = []
        target_titles_lower = [t.lower() for t in target_titles]

        for doc in all_docs:
            title_lower = doc['title'].lower()
            if any(t in title_lower or title_lower in t for t in target_titles_lower):
                target_docs.append(doc)
            else:
                other_docs.append(doc)

        # If targets not found in KB, use semantic search to find the closest matches
        if not target_docs:
            for title in target_titles:
                similar = self.rag.query_similar_notes(title, top_k=2)
                for s in similar:
                    # Build a synthetic doc entry from the search result
                    target_docs.append({
                        'title': title,
                        'content': s[:2000],
                        'tags': []
                    })
            if not target_docs:
                logging.warning(f"Monte Carlo: Targets {target_titles} not found, falling back to random.")
                return self._sample_random_pairs(all_docs, num_pairs)

        pairs = []

        if len(target_docs) >= 2:
            # Mode: Multiple targets — pair them with each other first
            from itertools import combinations
            for a, b in combinations(target_docs, 2):
                pairs.append((a, b))
                if len(pairs) >= num_pairs:
                    break

            # Fill remaining slots: pair targets with diverse neighbors
            if other_docs and len(pairs) < num_pairs:
                for target in target_docs:
                    if len(pairs) >= num_pairs:
                        break
                    neighbor = random.choice(other_docs)
                    pairs.append((target, neighbor))
        else:
            # Mode: Single target — pair it with diverse KB docs
            target = target_docs[0]
            if other_docs:
                candidates = random.sample(other_docs, min(len(other_docs), num_pairs))
                for other in candidates:
                    pairs.append((target, other))
            else:
                pairs.append((target_docs[0], random.choice(all_docs)))

        return pairs[:num_pairs]

    def _spark_seed(self, doc_a: dict, doc_b: dict, config: dict) -> dict | None:
        """Generate a seed insight from two random notes. High temperature for creativity."""
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
                temperature=self.TEMP_SPARK
            )
            seed = extract_json_object(raw)
            if seed and seed.get('idea'):
                seed.setdefault('novelty_score', 5)
                seed.setdefault('source_a', doc_a['title'])
                seed.setdefault('source_b', doc_b['title'])
                logging.info(
                    f"  Spark: score={seed['novelty_score']}, "
                    f"pair=({doc_a['title'][:30]} × {doc_b['title'][:30]})"
                )
                return seed
        except Exception as e:
            logging.warning(f"Monte Carlo spark failed: {e}")
        return None

    def _expand_seed(self, seed: dict, config: dict) -> dict:
        """Expand a winning seed with semantic search for supporting evidence."""
        idea = seed.get('idea', '')

        # Use semantic search to find supporting evidence
        evidence_docs = self.rag.query_similar_notes(idea, top_k=5)
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
            temperature=self.TEMP_EXPAND
        )

        return {
            **seed,
            'expanded': expansion_text,
            'evidence_sources': [doc.split('\n')[0] for doc in evidence_docs[:3]] if evidence_docs else []
        }

    def _synthesize_report(self, expanded_seeds: list[dict], config: dict, user_directive: str) -> str:
        """Synthesize expanded seeds into a cohesive report."""
        sections = []
        for i, seed in enumerate(expanded_seeds, 1):
            sections.append(
                f"### 🌟 Insight {i} (Score: {seed.get('novelty_score', '?')}/10)\n"
                f"**Connection**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
                f"{seed.get('expanded', seed.get('idea', ''))}"
            )

        all_sections = "\n\n---\n\n".join(sections)

        # Meta-data about the exploration
        total_sparks = len(expanded_seeds)
        avg_score = sum(s.get('novelty_score', 0) for s in expanded_seeds) / max(total_sparks, 1)

        synthesis_prompt = (
            f"You are writing the final synthesis of a Monte Carlo insight exploration.\n\n"
            f"## Exploration Statistics\n"
            f"- Seeds generated: multiple random pairs\n"
            f"- Winners selected: {total_sparks} (avg novelty: {avg_score:.1f}/10)\n\n"
            f"## Expanded Insights\n{all_sections}\n\n"
            f"## Task\n"
            f"Write a brief executive summary (3-5 paragraphs) that:\n"
            f"1. Identifies the meta-pattern across all winning insights\n"
            f"2. States the single most actionable takeaway\n"
            f"3. Suggests what areas of the knowledge base to explore next\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User's additional context: {user_directive or '(none)'}"
        )

        executive_summary = self.llm.answer_query(
            query_content="Synthesize the Monte Carlo exploration results.",
            wiki_context="",
            custom_instruction=synthesis_prompt,
            temperature=self.TEMP_SYNTHESIZE
        )

        return (
            f"# 🎲 Monte Carlo Insight Exploration\n\n"
            f"## 📝 Executive Summary\n\n{executive_summary}\n\n"
            f"---\n\n"
            f"## 🔬 Detailed Insights\n\n{all_sections}"
        )

    # ── Cross-Strategy Synthesis (for /full reports) ─────────────────

    def _extract_seeds_from_section(self, section_content: str, strategy_name: str) -> list[dict]:
        """Extract key insight seeds from a completed section for cross-strategy synthesis."""
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
                temperature=0.1
            )
            seeds = extract_json_array(raw)
            for seed in seeds:
                seed['strategy'] = strategy_name
            return seeds if seeds else [{"claim": section_content[:200], "strategy": strategy_name}]
        except Exception as e:
            logging.debug(f"Seed extraction failed for {strategy_name}: {e}")
            return [{"claim": section_content[:200], "strategy": strategy_name}]

    def _cross_strategy_synthesis(self, all_seeds: list[dict], user_directive: str) -> str:
        """Find meta-patterns across all strategy results."""
        if not all_seeds:
            return "(No cross-strategy patterns detected.)"

        seed_text = "\n".join(
            f"- [{s.get('strategy', '?')}] {s.get('claim', '?')}"
            for s in all_seeds[:15]  # Cap to avoid prompt overflow
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
            temperature=self.TEMP_SYNTHESIZE
        )

    # ── Context Retrieval Methods ────────────────────────────────────

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        file_matches = re.findall(r'\[\[(.*?)\]\]', user_directive)
        if file_matches:
            target_file = file_matches[0].split('|')[0].strip()
            if target_file.lower().endswith('.md'):
                target_file = target_file[:-3]
        tag_matches = re.findall(r'#([^\s#]+)', user_directive)
        if tag_matches:
            target_tag = tag_matches[0]

        if method == "recency":
            return self._get_recent_context(limit)
        elif method == "tags":
            return self._get_tag_cluster_context(limit, target_tag)
        elif method == "islands":
            return self._get_island_context(limit, target_file)
        else:
            return self._get_random_sample_context(limit, target_file)

    def _get_recent_context(self, limit: int) -> str:
        try:
            results = self.rag.collection.get(include=['metadatas', 'documents'])
            if not results['documents']:
                return "No documents found."
            docs_with_meta = list(zip(results['documents'], results['metadatas']))
            docs_with_meta.sort(key=lambda x: x[1].get('timestamp', ''), reverse=True)
            # Take the most recent, with some randomness for diversity
            pool_size = min(len(docs_with_meta), limit * 3)
            recent_pool = docs_with_meta[:pool_size]
            selection = random.sample(recent_pool, min(len(recent_pool), limit))
            return "\n---\n".join([x[0] for x in selection])
        except Exception as e:
            logging.debug(f"InsightAgent: recent context retrieval failed: {e}")
            return "No recent data found."

    def _get_tag_cluster_context(self, limit: int, target_tag: str = None) -> str:
        try:
            results = self.rag.collection.get(include=['metadatas', 'documents'])
            if not results['metadatas']:
                return self._get_random_sample_context(limit)

            if not target_tag:
                # Find the most interesting tag by frequency
                tag_counts = Counter()
                for meta in results['metadatas']:
                    tags = self._parse_stored_tags(meta.get('tags', ''))
                    tag_counts.update(tags)
                if not tag_counts:
                    return self._get_random_sample_context(limit)
                # Pick from tags with 2+ documents (interesting clusters)
                interesting_tags = [t for t, count in tag_counts.items() if count >= 2]
                target_tag = random.choice(interesting_tags if interesting_tags else list(tag_counts.keys()))

            # Filter documents by tag membership — proper parsing, not string matching
            cluster_docs = []
            for doc, meta in zip(results['documents'], results['metadatas']):
                doc_tags = self._parse_stored_tags(meta.get('tags', ''))
                if target_tag in doc_tags:
                    cluster_docs.append(doc)

            if not cluster_docs:
                return self._get_random_sample_context(limit)
            selection = random.sample(cluster_docs, min(len(cluster_docs), limit))
            return f"Focusing on Cluster: #{target_tag}\n\n" + "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: tag cluster retrieval failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str = None) -> str:
        """Find isolated notes — low connectivity in the tag co-occurrence graph."""
        if target_island:
            results = self.rag.collection.get(where={"title": target_island}, limit=limit)
            docs = results.get('documents', [])
            if docs:
                return f"Analysis target (Knowledge Island): [[{target_island}]]\n\n" + "\n---\n".join(docs)

        try:
            results = self.rag.collection.get(include=['metadatas', 'documents'])
            if not results['documents']:
                return self._get_random_sample_context(limit)

            # Build connectivity score: how many other docs share tags with each doc
            all_docs_meta = list(zip(results['documents'], results['metadatas']))
            tag_to_titles = {}
            title_to_entry = {}

            for doc, meta in all_docs_meta:
                title = meta.get('title', 'Unknown')
                tags = self._parse_stored_tags(meta.get('tags', ''))
                title_to_entry[title] = (doc, tags)
                for tag in tags:
                    tag_to_titles.setdefault(tag, set()).add(title)

            # Score each title by connectivity (how many other titles share tags)
            connectivity = {}
            for title, (doc, tags) in title_to_entry.items():
                connected = set()
                for tag in tags:
                    connected.update(tag_to_titles.get(tag, set()))
                connected.discard(title)  # Don't count self
                connectivity[title] = len(connected)

            # Find the most isolated titles
            isolated = sorted(connectivity.keys(), key=lambda t: connectivity[t])
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

            return (
                f"Knowledge Islands Detected (lowest connectivity scores):\n\n"
                + "\n---\n".join(island_docs)
            )
        except Exception as e:
            logging.debug(f"InsightAgent: island detection failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_random_sample_context(self, limit: int, target_file: str = None) -> str:
        try:
            if target_file:
                results = self.rag.collection.get(where={"title": target_file})
                docs = results.get('documents', [])
                if docs:
                    return f"Analysis target: [[{target_file}]]\n\n" + "\n---\n".join(docs)
            results = self.rag.collection.get()
            docs = results.get('documents', [])
            if not docs:
                return "Empty KB."
            selection = random.sample(docs, min(len(docs), limit))
            return "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: random sample retrieval failed: {e}")
            return "Error retrieving context."

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_stored_tags(tags_str: str) -> list[str]:
        """Parse the ',tag1,tag2,tag3,' format used in ChromaDB metadata."""
        if not tags_str:
            return []
        return [t.strip() for t in tags_str.split(',') if t.strip()]
