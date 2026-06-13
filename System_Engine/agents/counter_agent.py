import json
import logging
import re
from bisect import bisect_right
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, RAW_CONSOLIDATE_DIR, WIKI_VAULT_DIR
from core.parser import extract_json_array, extract_json_object
from core.ui import ui
from services.text_splitter import TextSplitter


_CONCEPT_INLINE_RE = re.compile(r'(?:Count|計算|算)\s*[:：]\s*(.+)', re.IGNORECASE)
_CONCEPT_BLOCK_RE = re.compile(r'(?:Count|計算|算)\s*[:：]\s*\n((?:\s*[-•]\s+.+\n?)+)', re.IGNORECASE)
_BULLET_RE = re.compile(r'[-•]\s+(.+)')
_CMD_TOKEN_RE = re.compile(r'(?:@ling-lens|@ling-count|/lens|/count)\b', re.IGNORECASE)
_WIKILINK_RE = re.compile(r'\[\[.*?\]\]')
_CONFIDENCE_TOKEN_RE = re.compile(r'(?:Confidence|信心)\s*[:：]\s*\S+', re.IGNORECASE)
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', re.MULTILINE)
_PART_HEADING_RE = re.compile(r'^\s{0,3}##\s+(Part\s+\d+)(?::.*?)?\s*#*\s*$', re.MULTILINE)
_PART_ANY_RE = re.compile(r'^\s{0,3}##\s+Part\s+\d+(?::.*?)?\s*#*\s*$', re.MULTILINE)
_WS_RE = re.compile(r'\s+')


class CounterAgent(BaseAgent):
    """LingLens — scans long articles through user-defined conceptual lenses.

    Supports multiple files × multiple concepts. Two-pass LLM pipeline per
    (article, concept) pair:
      1. Extract: scan each chunk for instances of the target concept.
      2. Tally:   deduplicate cross-chunk overlaps, produce a final count.
    """

    def __init__(self, llm, rag=None):
        super().__init__(llm, rag)
        from core.config import THOUGHTFUL_USE_LLM_FOR_COUNTER, USE_THOUGHTFUL_SPLITTER
        # LingLens scans for concept instances per-chunk; the chunk's
        # "self-containedness" is not the quality metric we care about,
        # and adding extra boundaries can slightly hurt recall around
        # cuts. So P5 LLM refinement defaults OFF for this agent even
        # when it's ON for ingestion.
        if USE_THOUGHTFUL_SPLITTER:
            from services.thoughtful_splitter import ThoughtfulSplitter
            self.splitter = ThoughtfulSplitter(
                default_use_llm=THOUGHTFUL_USE_LLM_FOR_COUNTER,
                # Pass llm regardless — counter's `default_use_llm=False` is
                # what actually skips P5, but the LLM is available if a
                # caller bumps `use_llm=True` explicitly.
                llm=self.llm,
            )
        else:
            self.splitter = TextSplitter()

    # ── Public entry point ─────────────────────────────────────────────

    def execute(self, task_context: dict) -> str:
        target_titles = task_context.get("target_titles", [])
        user_directive = task_context.get("user_directive", "")
        confidence = task_context.get("confidence", "medium")

        concepts = self._parse_concepts(user_directive)
        if not concepts:
            ui.error("🔎 LingLens: 無法判斷要觀察的概念鏡片")
            return self._error_report(
                "Could not determine what to count. "
                "Please include a description after the command, e.g.:\n"
                "`@ling-lens [[Article]] Count: appeals to authority`"
            )

        ui.set_status(f"🔢 正在搜尋文章：{target_titles}")
        articles = self._resolve_articles(target_titles, user_directive)
        if not articles:
            ui.error(f"🔢 找不到任何文章：{target_titles or '(none specified)'}")
            return self._error_report(
                f"Could not find article content for: {target_titles or '(none specified)'}. "
                "Please use `[[WikiLink]]` to reference the article."
            )

        self._show_job_summary(concepts, articles, confidence)

        results_matrix = self._run_all_jobs(articles, concepts, confidence)

        ui.set_status("🔢 正在生成報告...")
        is_matrix = len(articles) > 1 or len(concepts) > 1
        if is_matrix:
            report = self._format_matrix_report(concepts, articles, results_matrix)
        else:
            article_title, _, resolved_path = articles[0]
            concept = concepts[0]
            tally = results_matrix[article_title][concept]
            report = self._format_report(concept, article_title, tally, resolved_path)

        verification = self._verify_quote_grounding(results_matrix)
        report = report.rstrip() + "\n\n" + self._format_quote_verification(verification) + "\n"

        grand_total = sum(
            tally.get("total_count", 0)
            for tallies in results_matrix.values()
            for tally in tallies.values()
        )
        meta = {
            "target_concepts": concepts,
            "source_articles": [a[0] for a in articles],
            "total_count": grand_total,
            "matrix_mode": is_matrix,
            "quotes_total": verification["total"],
            "quotes_grounded": verification["grounded"],
        }
        if verification["verdict"]:
            meta["quality_verdict"] = verification["verdict"]
        title_slug = concepts[0] if len(concepts) == 1 else f"{len(concepts)} concepts"
        article_slug = articles[0][0] if len(articles) == 1 else f"{len(articles)} articles"
        self._write_report(
            f"LingLens: {title_slug} in {article_slug}",
            report,
            "lens_report",
            meta,
        )
        ui.success(
            f"🔎 LingLens 完成！{len(concepts)} 概念 × {len(articles)} 篇文章 → 共 {grand_total} 個實例"
        )
        return report

    def _show_job_summary(self, concepts, articles, confidence):
        ui.info(f"🔎 LingLens 啟動")
        ui.info(f"   📌 計算目標 ({len(concepts)} 個概念):")
        for c in concepts:
            ui.info(f"      • [bold yellow]{c}[/bold yellow]")
        ui.info(f"   📎 分析文章 ({len(articles)} 篇):")
        for title, _, path in articles:
            path_hint = f" [dim]({path})[/dim]" if path else ""
            ui.info(f"      • [bold green]{title}[/bold green]{path_hint}")
        ui.info(f"   🎯 信心門檻: [green]{confidence}[/green]")

    def _run_all_jobs(self, articles, concepts, confidence):
        results_matrix: dict[str, dict] = {}
        total_jobs = len(articles) * len(concepts)
        job_idx = 0

        for article_title, article_text, _ in articles:
            results_matrix[article_title] = {}
            chunks = self.splitter.split_text(article_text)
            total_parts = len(chunks)
            ui.info(f"\n   📄 [{article_title}] — {len(article_text):,} chars → {total_parts} chunks")

            # Precompute heading & part-anchor offsets once per article — N
            # instances would otherwise each re-scan from start of file.
            location_index = _LocationIndex(article_text)

            for concept in concepts:
                job_idx += 1
                ui.info(
                    f"\n   ── 任務 {job_idx}/{total_jobs}: "
                    f"[yellow]{concept}[/yellow] × [green]{article_title}[/green] ──"
                )
                tally = self._run_single_count(concept, chunks, total_parts, confidence, job_idx, total_jobs)
                self._ground_tally_locations(tally, article_text, location_index)
                results_matrix[article_title][concept] = tally

                ui.info(
                    f"   🏁 結果: [bold]{tally.get('total_count', 0)}[/bold] 個實例 "
                    f"(🟢{tally.get('high_confidence_count', 0)} "
                    f"🟡{tally.get('medium_confidence_count', 0)} "
                    f"🔴{tally.get('low_confidence_count', 0)})"
                )
        return results_matrix

    # ── Single (article, concept) pipeline ─────────────────────────────

    def _run_single_count(self, concept, chunks, total_parts, confidence, job_idx, total_jobs):
        all_instances: list[dict] = []

        for i, chunk in enumerate(chunks):
            ui.set_status(
                f"🔢 [{job_idx}/{total_jobs}] Extract: chunk {i + 1}/{total_parts} — 累計 {len(all_instances)}"
            )
            instances = self._extract_from_chunk(concept, chunk, i + 1, total_parts, confidence)
            all_instances.extend(instances)

            if instances:
                chunk_high = sum(1 for x in instances if x.get("confidence") == "high")
                chunk_med = sum(1 for x in instances if x.get("confidence") == "medium")
                chunk_low = sum(1 for x in instances if x.get("confidence") == "low")
                ui.info(
                    f"      ✅ Chunk {i + 1}/{total_parts}: +{len(instances)} "
                    f"(🟢{chunk_high} 🟡{chunk_med} 🔴{chunk_low}) | 累計: {len(all_instances)}"
                )
            else:
                ui.info(f"      ⬜ Chunk {i + 1}/{total_parts}: 0 | 累計: {len(all_instances)}")

        if not all_instances:
            return self._build_tally_locally(concept, [])

        ui.set_status(
            f"🔢 [{job_idx}/{total_jobs}] 正在整理找到的 {len(all_instances)} 個線索，合併重複項目"
        )
        return self._tally_instances(concept, all_instances, total_parts)

    # ── Concept / article resolution ───────────────────────────────────

    def _parse_concepts(self, user_directive: str) -> list[str]:
        """Extract one or more counting targets from the user directive."""
        concepts: list[str] = []

        for match in _CONCEPT_INLINE_RE.finditer(user_directive):
            val = match.group(1).strip().rstrip("?？")
            if val:
                concepts.append(val)

        for match in _CONCEPT_BLOCK_RE.finditer(user_directive):
            for bullet in _BULLET_RE.findall(match.group(1)):
                val = bullet.strip().rstrip("?？")
                if val and val not in concepts:
                    concepts.append(val)

        if concepts:
            return concepts

        cleaned = _CMD_TOKEN_RE.sub("", user_directive)
        cleaned = _WIKILINK_RE.sub("", cleaned).strip()
        cleaned = _CONFIDENCE_TOKEN_RE.sub("", cleaned).strip()
        cleaned = cleaned.strip("?？\n\r\t ")
        return [cleaned] if len(cleaned) > 3 else []

    def _resolve_articles(self, target_titles: list[str], user_directive: str) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        for title in target_titles:
            if title in seen:
                continue
            text, resolved_path = self._find_in_pages(title)
            if text:
                results.append((title, text, resolved_path))
                seen.add(title)
                ui.info(f"   📄 已找到: [green]{title}[/green] ({len(text):,} chars)")
            else:
                ui.info(f"   ⚠️  未找到: [dim]{title}[/dim]")

        if not results and self.rag and user_directive:
            ui.info("   🔍 檔案系統未找到任何文章，嘗試 RAG 語意檢索...")
            try:
                rag_results = self.rag.query_similar_notes(user_directive, top_k=1)
            except Exception as e:
                logging.debug(f"LingLens: RAG fallback failed: {e}")
                rag_results = []
            if rag_results:
                ui.info("   📡 RAG 檢索成功")
                results.append(("(RAG result)", rag_results[0], ""))

        return results

    def _find_in_pages(self, title: str) -> tuple[str, str]:
        title_clean = title.strip()
        if not title_clean:
            return "", ""

        folder = PAGES_DIR / title_clean
        if folder.is_dir():
            for pattern in (
                f"{title_clean} (Stitched).md",
                f"{title_clean} (Synthesis).md",
                f"{title_clean}.md",
            ):
                candidate = folder / pattern
                if candidate.exists():
                    return candidate.read_text(encoding="utf-8"), str(candidate)
            parts = sorted(folder.glob("*.md"))
            if parts:
                ui.info(f"   📑 組合 {len(parts)} 個 Part 檔案作為分析來源")
                combined = "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in parts)
                return combined, str(folder)

        direct = PAGES_DIR / f"{title_clean}.md"
        if direct.exists():
            return direct.read_text(encoding="utf-8"), str(direct)

        for md in PAGES_DIR.rglob(f"*{title_clean}*.md"):
            return md.read_text(encoding="utf-8"), str(md)

        notes_dir = WIKI_VAULT_DIR / "Notes"
        if notes_dir.exists():
            for md in notes_dir.rglob(f"*{title_clean}*.md"):
                return md.read_text(encoding="utf-8"), str(md)

        return "", ""

    # ── Pass 1: Extraction ─────────────────────────────────────────────

    def _extract_from_chunk(self, concept, chunk, part, total, confidence):
        system_prompt = self._load_prompt("agent_counter") or (
            "You are a precise textual analyst. Your job is to scan source text "
            "and identify every instance of a user-defined concept. "
            "Return ONLY a valid JSON array. No markdown, no commentary."
        )
        user_prompt = (
            f'Target concept: "{concept}"\n'
            f"Minimum confidence: {confidence}\n\n"
            f"Source text (Part {part}/{total}):\n{chunk}\n\n"
            "Find every instance where the target concept appears in the source text.\n"
            "Return a JSON array. Each element must have this exact structure:\n"
            "{\n"
            '  "quote": "exact or near-exact quote from the source (max 120 chars)",\n'
            '  "reasoning": "brief explanation of why this qualifies",\n'
            '  "confidence": "high" or "medium" or "low",\n'
            '  "closest_heading": "the exact text of the closest preceding markdown heading (without the # symbols)"\n'
            "}\n\n"
            "Rules:\n"
            "- Be thorough: scan every paragraph.\n"
            "- Only include genuine instances — do not fabricate quotes.\n"
            "- If zero instances found, return an empty array: []\n"
            "- Return ONLY the JSON array, nothing else.\n"
        )
        instances = []
        # Reasoning models intermittently emit the whole reply into the
        # reasoning channel without the final JSON array — retry once before
        # treating the chunk as empty. A literal [] in the reply is a genuine
        # zero, not a parse failure.
        for attempt in range(2):
            try:
                # JSON output: opt out of the template/persona axes, or the
                # default wiki-note template (STRICT ADHERENCE) overrides the
                # JSON instruction and the model writes a note instead.
                raw = self.llm.answer_query(
                    user_prompt,
                    wiki_context="",
                    custom_instruction=system_prompt,
                    forced_template="none",
                    persona="none",
                )
            except Exception as e:
                ui.error(f"      ❌ Chunk {part} extraction 失敗: {e}")
                logging.error(f"LingLens extraction failed for chunk {part}: {e}")
                return []
            instances = extract_json_array(raw)
            if instances or "[]" in _WS_RE.sub("", raw):
                break
            logging.warning(
                f"LingLens chunk {part}: reply had no JSON array (attempt {attempt + 1})."
            )
        for inst in instances:
            inst["source_part"] = part
        return instances

    # ── Pass 2: Tally ──────────────────────────────────────────────────

    def _tally_instances(self, concept, all_instances, total_parts):
        if len(all_instances) <= 3:
            return self._build_tally_locally(concept, all_instances)

        system_prompt = (
            "You are a deduplication and tallying expert. "
            "Merge duplicates and produce a clean final count. "
            "Return ONLY valid JSON. No markdown, no commentary."
        )
        instances_json = json.dumps(all_instances, ensure_ascii=False, indent=2)
        user_prompt = (
            f'Candidate instances of "{concept}" from a {total_parts}-part article:\n\n'
            f"{instances_json}\n\n"
            "Tasks:\n"
            "1. Remove exact duplicates (same quote from overlapping chunks).\n"
            "2. Merge near-duplicates (keep the best quote).\n"
            "3. Assign final confidence to each unique instance.\n"
            "4. Number sequentially from 1.\n"
            "5. Return JSON:\n"
            "{\n"
            f'  "concept": "{concept}",\n'
            '  "total_count": <int>,\n'
            '  "high_confidence_count": <int>,\n'
            '  "medium_confidence_count": <int>,\n'
            '  "low_confidence_count": <int>,\n'
            '  "instances": [\n'
            '    {"id": 1, "quote": "...", "reasoning": "...", "confidence": "high|medium|low", "closest_heading": "..."}\n'
            "  ],\n"
            '  "methodology_note": "Brief description"\n'
            "}\n"
            "Return ONLY the JSON object.\n"
        )
        # Same reasoning-channel hazard as _extract_from_chunk: gemma may emit
        # the reply into the reasoning channel without the JSON object. Retry
        # once before falling back to the cruder local dedup.
        for attempt in range(2):
            try:
                # JSON output: same template/persona opt-out as _extract_from_chunk.
                raw = self.llm.answer_query(
                    user_prompt,
                    wiki_context="",
                    custom_instruction=system_prompt,
                    forced_template="none",
                    persona="none",
                )
                tally = extract_json_object(raw)
                if tally and "total_count" in tally:
                    return tally
                logging.warning(
                    f"LingLens tally: reply had no usable JSON object (attempt {attempt + 1})."
                )
            except Exception as e:
                logging.error(f"LingLens tally pass failed (attempt {attempt + 1}): {e}")

        return self._build_tally_locally(concept, all_instances)

    @staticmethod
    def _build_tally_locally(concept, instances):
        seen: set[str] = set()
        unique = []
        for inst in instances:
            q = inst.get("quote", "").strip().lower()
            if q and q not in seen:
                seen.add(q)
                unique.append(inst)
        high = sum(1 for i in unique if i.get("confidence") == "high")
        med = sum(1 for i in unique if i.get("confidence") == "medium")
        low = sum(1 for i in unique if i.get("confidence") == "low")
        for idx, inst in enumerate(unique, 1):
            inst["id"] = idx
        return {
            "concept": concept,
            "total_count": len(unique),
            "high_confidence_count": high,
            "medium_confidence_count": med,
            "low_confidence_count": low,
            "instances": unique,
            "methodology_note": "Extracted per-chunk, deduplicated by exact quote match.",
        }

    # ── Source grounding ───────────────────────────────────────────────

    def _ground_tally_locations(self, tally, article_text, location_index=None):
        """Attach deterministic source-location hints based on quoted source text."""
        if location_index is None:
            location_index = _LocationIndex(article_text)

        for inst in tally.get("instances", []):
            start = self._find_quote_offset(article_text, inst.get("quote", ""))
            if start < 0:
                continue
            inst["source_offset"] = start

            heading = location_index.closest_heading(start)
            part_anchor = location_index.closest_part(start)
            if part_anchor:
                inst["source_part_anchor"] = part_anchor
                source_range = self._part_source_range_for_anchor(article_text, part_anchor)
                if source_range:
                    inst["original_source_range"] = source_range
            if heading:
                inst["closest_heading"] = heading
                inst["source_anchor"] = part_anchor or heading

    @staticmethod
    def _verify_quote_grounding(results_matrix) -> dict:
        """Surface the deterministic grounding signal as a per-report verdict.

        `_ground_tally_locations` already tried to locate every quote in the
        source; instances without a `source_offset` are the ones it could
        not find. Below LENS_QUOTE_MIN_GROUNDED_RATIO the report is marked
        "revise" — same vocabulary as the synthesis critique postcheck, so
        artifact verdicts stay comparable across report types.
        """
        from core.config import LENS_QUOTE_MIN_GROUNDED_RATIO

        total = 0
        grounded = 0
        ungrounded = []
        for article_title, tallies in results_matrix.items():
            for concept, tally in (tallies or {}).items():
                for inst in (tally or {}).get("instances", []):
                    total += 1
                    if inst.get("source_offset") is not None:
                        grounded += 1
                    else:
                        ungrounded.append({
                            "article": article_title,
                            "concept": concept,
                            "id": inst.get("id"),
                            "quote": (inst.get("quote") or "").strip()[:120],
                        })
        ratio = (grounded / total) if total else None
        verdict = None
        if total:
            verdict = "keep" if ratio >= LENS_QUOTE_MIN_GROUNDED_RATIO else "revise"
        return {
            "total": total,
            "grounded": grounded,
            "ratio": ratio,
            "ungrounded": ungrounded,
            "verdict": verdict,
        }

    @staticmethod
    def _format_quote_verification(verification) -> str:
        lines = ["## 🔍 Quote Verification", ""]
        total = verification["total"]
        if not total:
            lines.append("（沒有實例可驗證）")
            return "\n".join(lines)

        grounded = verification["grounded"]
        ratio = verification["ratio"]
        lines.append(
            f"**{grounded}/{total}** 個引文在原文中定位成功"
            f"（{ratio:.0%}）— verdict: **{verification['verdict']}**"
        )
        if verification["ungrounded"]:
            lines.extend([
                "",
                "以下引文無法在原文中定位（可能是改寫、翻譯，或模型虛構——請人工抽查）：",
                "",
            ])
            for item in verification["ungrounded"][:10]:
                label = f"#{item['id']}" if item.get("id") is not None else "#?"
                lines.append(f"- {label}（{item['concept']} @ [[{item['article']}]]）：「{item['quote']}」")
            hidden = len(verification["ungrounded"]) - 10
            if hidden > 0:
                lines.append(f"- ……及其他 {hidden} 條")
        return "\n".join(lines)

    @staticmethod
    def _find_quote_offset(article_text, quote):
        quote = (quote or "").strip().strip('"“”')
        if not quote:
            return -1

        exact = article_text.find(quote)
        if exact >= 0:
            return exact

        normalized_quote = _WS_RE.sub(" ", quote)
        if len(normalized_quote) < 12:
            return -1

        needle = re.escape(normalized_quote[:80])
        flexible = re.sub(r'\\ ', r'\\s+', needle)
        match = re.search(flexible, article_text, flags=re.DOTALL)
        return match.start() if match else -1

    @staticmethod
    def _part_source_range_for_anchor(article_text, part_anchor):
        pattern = rf'^\s{{0,3}}##\s+{re.escape(part_anchor)}(?::.*?)?\s*#*\s*$'
        match = re.search(pattern, article_text, flags=re.MULTILINE)
        if not match:
            return {}

        next_part = _PART_ANY_RE.search(article_text, match.end())
        section_end = next_part.start() if next_part else len(article_text)
        section = article_text[match.end():section_end]

        line_match = re.search(r'Original range:\s*lines\s*(\d+)\s*-\s*(\d+)', section)
        char_match = re.search(r'Original chars:\s*(\d+)\s*-\s*(\d+)', section)
        source_range = {}
        if line_match:
            source_range["start_line"] = int(line_match.group(1))
            source_range["end_line"] = int(line_match.group(2))
        if char_match:
            source_range["start_char"] = int(char_match.group(1))
            source_range["end_char"] = int(char_match.group(2))
        return source_range

    # ── Report: Matrix format ──────────────────────────────────────────

    def _format_matrix_report(self, concepts, articles, results_matrix):
        lines = ["# 🔎 LingLens — Cross Analysis", ""]

        lines.append("## 📊 Summary Matrix")
        lines.append("")
        header = "| Article |" + "|".join(f" {c} " for c in concepts) + "| Total |"
        sep = "|" + "|".join("---" for _ in range(len(concepts) + 2)) + "|"
        lines.extend([header, sep])

        for article_title, _, _ in articles:
            row_counts = []
            row_total = 0
            for concept in concepts:
                count = results_matrix.get(article_title, {}).get(concept, {}).get("total_count", 0)
                row_counts.append(str(count))
                row_total += count
            lines.append(
                f"| [[{article_title}]] |"
                + "|".join(f" {c} " for c in row_counts)
                + f"| **{row_total}** |"
            )

        col_totals = []
        grand_total = 0
        for concept in concepts:
            col_sum = sum(
                results_matrix.get(a[0], {}).get(concept, {}).get("total_count", 0)
                for a in articles
            )
            col_totals.append(str(col_sum))
            grand_total += col_sum
        lines.append(
            "| **Total** |"
            + "|".join(f" **{c}** " for c in col_totals)
            + f"| **{grand_total}** |"
        )
        lines.append("")

        for article_title, _, resolved_path in articles:
            reference_title = self._reference_title(article_title, resolved_path)
            lines.append("---")
            lines.append(f"## 📄 {article_title}")
            lines.append("")
            for concept in concepts:
                tally = results_matrix.get(article_title, {}).get(concept, {})
                total = tally.get("total_count", 0)
                instances = tally.get("instances", [])
                lines.append(f"### 📌 {concept} — {total} instances")
                lines.append("")
                if not instances:
                    lines.extend(["> 未發現實例", ""])
                    continue
                lines.append("| # | Confidence | Quote | Reasoning | Reference |")
                lines.append("|---|------------|-------|-----------|--------|")
                for inst in instances:
                    lines.append(self._matrix_row(inst, article_title, reference_title, resolved_path))
                lines.append("")

        lines.append("---")
        lines.append("## 🔗 Navigation")
        for article_title, _, resolved_path in articles:
            reference_title = self._reference_title(article_title, resolved_path)
            lines.append(f"- [[{reference_title}|查看分析來源]]")
            original_title = self._original_source_title(article_title)
            if original_title != reference_title:
                lines.append(f"- [[{original_title}|查看原始檔]]")

        # _write_report runs the markdown-quality pipeline downstream; no need
        # to also run it here.
        return "\n".join(lines)

    _CONF_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}

    def _matrix_row(self, inst, article_title, reference_title, resolved_path):
        iid = inst.get("id", "?")
        conf = inst.get("confidence", "medium")
        emoji = self._CONF_EMOJI.get(conf, "⚪")
        quote = self._table_cell(inst.get("quote", ""), 80)
        reasoning = self._table_cell(inst.get("reasoning", ""), 80)
        heading = self._instance_anchor(inst)
        reference = self._reference_cell(article_title, reference_title, resolved_path, heading, inst)
        return f"| {iid} | {emoji} {conf} | {quote} | {reasoning} | {reference} |"

    # ── Report: Single article × single concept ────────────────────────

    def _format_report(self, concept, article_title, tally, resolved_path=""):
        reference_title = self._reference_title(article_title, resolved_path)
        reference_label = "Analysis source" if self._is_stitched_path(resolved_path) else "Source"
        total = tally.get("total_count", 0)
        high = tally.get("high_confidence_count", 0)
        medium = tally.get("medium_confidence_count", 0)
        low = tally.get("low_confidence_count", 0)
        methodology = tally.get("methodology_note", "")
        instances = tally.get("instances", [])

        lines = [
            f"# 🔎 LingLens: {concept}", "",
            f"> {reference_label}: [[{reference_title}]] | Total instances found: **{total}**", "",
            "## 📊 Summary", "",
            "| Confidence | Count |", "|------------|-------|",
            f"| 🟢 High   | {high}     |",
            f"| 🟡 Medium | {medium}     |",
            f"| 🔴 Low    | {low}     |", "",
        ]
        if methodology:
            lines.extend(["## 📝 Methodology", "", methodology, ""])

        lines.extend(["## 📋 Evidence", ""])
        original_title = self._original_source_title(article_title)
        for inst in instances:
            lines.extend(self._format_instance(inst, reference_title, original_title, resolved_path))

        lines.extend(["---", "## 🔗 Navigation", f"- [[{reference_title}|查看分析來源]]"])
        if original_title != reference_title:
            lines.append(f"- [[{original_title}|查看原始檔]]")

        return "\n".join(lines)

    def _format_instance(self, inst, reference_title, original_title, resolved_path):
        iid = inst.get("id", "?")
        conf = inst.get("confidence", "medium")
        emoji = self._CONF_EMOJI.get(conf, "⚪")

        lines = [
            f"### Instance {iid} ({emoji} {conf.capitalize()})",
            f'> "{inst.get("quote", "(no quote)")}"',
            "",
        ]
        if inst.get("reasoning"):
            lines.append(f"**Reasoning**: {inst['reasoning']}")

        heading = self._instance_anchor(inst)
        original_link = self._source_link(original_title, "", "🔗 查看原始檔")
        if heading:
            alias = "🔗 點擊查看分析錨點" if self._is_stitched_path(resolved_path) else "🔗 點擊查看原文錨點"
            lines.append(f"**Analysis Reference**: {self._source_link(reference_title, heading, alias)}")
        else:
            alias = "🔗 查看分析來源" if self._is_stitched_path(resolved_path) else "🔗 查看原文"
            lines.append(f"**Analysis Reference**: {self._source_link(reference_title, '', alias)}")

        physical_link = self._physical_source_link(original_title, inst)
        if original_title != reference_title:
            range_text = self._original_range_text(inst)
            suffix = f" ({range_text})" if range_text else ""
            lines.append(f"**Original Reference**: {original_link}{suffix}")
        if physical_link:
            lines.append(f"**Open in editor**: {physical_link}")
        lines.append("")
        return lines

    def _error_report(self, message):
        report = f"# ❌ LingLens Error\n\n{message}\n"
        self._write_report("LingLens Error", report, "lens_report")
        return report

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _source_link(article_title, heading="", alias="🔗原文"):
        safe_title = str(article_title).replace("|", "-").strip()
        safe_heading = str(heading or "").replace("|", "-").strip()
        target = f"{safe_title}#{safe_heading}" if safe_heading else safe_title
        return f"[[{target}|{alias}]]"

    def _reference_cell(self, article_title, reference_title, resolved_path, heading, inst):
        if heading:
            alias = "🔗分析錨點" if self._is_stitched_path(resolved_path) else "🔗原文錨點"
            analysis_link = self._source_link(reference_title, heading, alias)
        else:
            alias = "🔗分析來源" if self._is_stitched_path(resolved_path) else "🔗原文"
            analysis_link = self._source_link(reference_title, "", alias)

        original_title = self._original_source_title(article_title)
        range_text = self._original_range_text(inst)
        physical_link = self._physical_source_link(original_title, inst)

        parts = [analysis_link]
        if original_title != reference_title:
            original_link = self._source_link(original_title, "", "🔗原始檔")
            if range_text:
                original_link = f"{original_link} ({range_text})"
            parts.append(original_link)
        elif range_text:
            parts.append(range_text)
        if physical_link:
            parts.append(physical_link)
        return "<br>".join(parts)

    @staticmethod
    def _instance_anchor(inst):
        anchor = inst.get("source_anchor") or inst.get("closest_heading") or ""
        return str(anchor).replace("#", "").strip()

    @staticmethod
    def _original_range_text(inst):
        source_range = inst.get("original_source_range") or {}
        start_line = source_range.get("start_line")
        end_line = source_range.get("end_line")
        return f"原文 lines {start_line}-{end_line}" if start_line and end_line else ""

    @staticmethod
    def _reference_title(article_title, resolved_path=""):
        if resolved_path:
            path = Path(resolved_path)
            if path.suffix.lower() == ".md":
                return path.stem
        return article_title

    @staticmethod
    def _original_source_title(article_title):
        direct = PAGES_DIR / f"{article_title}.md"
        if direct.exists():
            return direct.stem
        raw = RAW_CONSOLIDATE_DIR / f"{article_title}.md"
        if raw.exists():
            return raw.stem
        for candidate in RAW_CONSOLIDATE_DIR.glob(f"{article_title}_*.md"):
            return candidate.stem
        return article_title

    @staticmethod
    def _resolve_original_source_path(article_title):
        """Return the absolute filesystem path of the original source, or None.

        Looks in the same locations as `_original_source_title` but returns
        the Path object so we can render a `file:///` link to it.
        """
        direct = PAGES_DIR / f"{article_title}.md"
        if direct.exists():
            return direct.resolve()
        raw = RAW_CONSOLIDATE_DIR / f"{article_title}.md"
        if raw.exists():
            return raw.resolve()
        for candidate in RAW_CONSOLIDATE_DIR.glob(f"{article_title}_*.md"):
            return candidate.resolve()
        return None

    @staticmethod
    def _file_url_with_range(path, start_line=None, end_line=None):
        """Build a `file:///<abs_path>#L<a>-L<b>` URL for editor jumps.

        Uses `Path.as_uri()` for proper percent-encoding — most Ling-Ling
        filenames contain spaces, parentheses, and CJK characters, all of
        which need encoding for Markdown parsers to treat the link
        destination reliably.

        The `#L<start>-L<end>` fragment is only honored by VS Code-family
        editors; Obsidian / `open` will open the file but ignore the line
        range. See README "Lens dual-link" — the Obsidian wikilink remains
        the always-works form.
        """
        if path is None:
            return ""
        try:
            url = Path(path).resolve().as_uri()
        except (ValueError, OSError):
            # Path.as_uri() requires an absolute path; bail if we can't get one.
            return ""
        if start_line and end_line:
            url = f"{url}#L{start_line}-L{end_line}"
        elif start_line:
            url = f"{url}#L{start_line}"
        return url

    @classmethod
    def _physical_source_link(cls, article_title, inst, label="📄 原始檔"):
        """Render the file:/// half of the lens dual-link, or '' if unavailable."""
        path = cls._resolve_original_source_path(article_title)
        if path is None:
            return ""
        source_range = inst.get("original_source_range") or {}
        start_line = source_range.get("start_line")
        end_line = source_range.get("end_line")
        url = cls._file_url_with_range(path, start_line, end_line)
        if not url:
            return ""
        if start_line and end_line:
            return f"[{label} L{start_line}-L{end_line}]({url})"
        return f"[{label}]({url})"

    @staticmethod
    def _is_stitched_path(resolved_path=""):
        return bool(resolved_path and Path(resolved_path).stem.endswith("(Stitched)"))

    @staticmethod
    def _table_cell(value, max_len=80):
        cell = _WS_RE.sub(" ", str(value or "")).strip()
        cell = cell.replace("|", "\\|")
        if len(cell) > max_len:
            cell = cell[: max_len - 1].rstrip() + "…"
        return cell


class _LocationIndex:
    """Precomputed sorted (heading-end-offset, text) lists for fast lookup.

    Built once per article; each `closest_heading(offset)` lookup is then
    O(log n) instead of an O(article-size) re-scan from the start of file
    that the original code did inside a per-instance loop.

    We store heading *end* offsets so that a quote at offset N only sees
    headings whose entire line ends before N — matching the original code's
    behaviour of running the regex over `article_text[:offset]`.
    """

    __slots__ = ("_heading_ends", "_heading_texts", "_part_ends", "_part_texts")

    def __init__(self, article_text: str):
        self._heading_ends: list[int] = []
        self._heading_texts: list[str] = []
        for m in _HEADING_RE.finditer(article_text):
            self._heading_ends.append(m.end())
            self._heading_texts.append(m.group(1).strip())

        self._part_ends: list[int] = []
        self._part_texts: list[str] = []
        for m in _PART_HEADING_RE.finditer(article_text):
            self._part_ends.append(m.end())
            self._part_texts.append(m.group(1).strip())

    def closest_heading(self, offset: int) -> str:
        return self._lookup_before(self._heading_ends, self._heading_texts, offset)

    def closest_part(self, offset: int) -> str:
        return self._lookup_before(self._part_ends, self._part_texts, offset)

    @staticmethod
    def _lookup_before(ends: list[int], texts: list[str], offset: int) -> str:
        if not ends:
            return ""
        idx = bisect_right(ends, offset) - 1
        return texts[idx] if idx >= 0 else ""
