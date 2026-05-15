import logging
import json
import re
from datetime import datetime
from pathlib import Path
from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, RAW_CONSOLIDATE_DIR, WIKI_VAULT_DIR, settings
from core.parser import run_markdown_quality_checks, extract_json_array, extract_json_object
from core.ui import ui
from services.text_splitter import TextSplitter


class CounterAgent(BaseAgent):
    """
    LingLens — scans long articles through user-defined conceptual lenses.
    Supports multiple files × multiple concepts.

    Uses a two-pass LLM pipeline per (article, concept) pair:
      1. Extract: scan each chunk for instances of the target concept.
      2. Tally:   deduplicate cross-chunk overlaps and produce a final count.
    """

    def __init__(self, llm, rag=None):
        super().__init__(llm, rag)
        self.splitter = TextSplitter()

    # ── Public entry point ─────────────────────────────────────────────

    def execute(self, task_context: dict) -> str:
        target_titles = task_context.get("target_titles", [])
        user_directive = task_context.get("user_directive", "")
        confidence = task_context.get("confidence", "medium")

        # Parse multiple concepts
        concepts = self._parse_concepts(user_directive)
        if not concepts:
            ui.error("🔎 LingLens: 無法判斷要觀察的概念鏡片")
            return self._error_report("Could not determine what to count. "
                                      "Please include a description after the command, e.g.:\n"
                                      "`@ling-lens [[Article]] Count: appeals to authority`")

        # Resolve all articles
        ui.set_status(f"🔢 正在搜尋文章：{target_titles}")
        articles = self._resolve_articles(target_titles, user_directive)
        if not articles:
            ui.error(f"🔢 找不到任何文章：{target_titles or '(none specified)'}")
            return self._error_report(f"Could not find article content for: {target_titles or '(none specified)'}. "
                                      "Please use `[[WikiLink]]` to reference the article.")

        # Display job summary
        ui.info(f"🔎 LingLens 啟動")
        ui.info(f"   📌 計算目標 ({len(concepts)} 個概念):")
        for c in concepts:
            ui.info(f"      • [bold yellow]{c}[/bold yellow]")
        ui.info(f"   📎 分析文章 ({len(articles)} 篇):")
        for title, _, path in articles:
            path_hint = f" [dim]({path})[/dim]" if path else ""
            ui.info(f"      • [bold green]{title}[/bold green]{path_hint}")
        ui.info(f"   🎯 信心門檻: [green]{confidence}[/green]")

        # ── Run all (article, concept) pairs ──────────────────────────
        # results_matrix[article_title][concept] = tally dict
        results_matrix = {}
        total_jobs = len(articles) * len(concepts)
        job_idx = 0

        for article_title, article_text, resolved_path in articles:
            results_matrix[article_title] = {}
            # Chunk article once, reuse for all concepts
            chunks = self.splitter.split_text(article_text)
            total_parts = len(chunks)
            ui.info(f"\n   📄 [{article_title}] — {len(article_text):,} chars → {total_parts} chunks")

            for concept in concepts:
                job_idx += 1
                ui.info(f"\n   ── 任務 {job_idx}/{total_jobs}: "
                        f"[yellow]{concept}[/yellow] × [green]{article_title}[/green] ──")

                tally = self._run_single_count(concept, chunks, total_parts, confidence, job_idx, total_jobs)
                self._ground_tally_locations(tally, article_text)
                results_matrix[article_title][concept] = tally

                count = tally.get("total_count", 0)
                ui.info(f"   🏁 結果: [bold]{count}[/bold] 個實例 "
                        f"(🟢{tally.get('high_confidence_count',0)} "
                        f"🟡{tally.get('medium_confidence_count',0)} "
                        f"🔴{tally.get('low_confidence_count',0)})")

        # ── Build report ──────────────────────────────────────────────
        ui.set_status("🔢 正在生成報告...")
        is_matrix = len(articles) > 1 or len(concepts) > 1
        if is_matrix:
            report = self._format_matrix_report(concepts, articles, results_matrix)
        else:
            # Single article × single concept — use the classic detailed format
            article_title = articles[0][0]
            resolved_path = articles[0][2]
            concept = concepts[0]
            tally = results_matrix[article_title][concept]
            report = self._format_report(concept, article_title, tally, resolved_path)

        # Build metadata
        grand_total = sum(
            t.get("total_count", 0)
            for art_tallies in results_matrix.values()
            for t in art_tallies.values()
        )
        meta = {
            "target_concepts": concepts,
            "source_articles": [a[0] for a in articles],
            "total_count": grand_total,
            "matrix_mode": is_matrix,
        }
        title_slug = concepts[0] if len(concepts) == 1 else f"{len(concepts)} concepts"
        article_slug = articles[0][0] if len(articles) == 1 else f"{len(articles)} articles"
        self._write_report(
            f"LingLens: {title_slug} in {article_slug}",
            report, "lens_report", meta,
        )
        ui.success(f"🔎 LingLens 完成！{len(concepts)} 概念 × {len(articles)} 篇文章 → 共 {grand_total} 個實例")
        return report

    # ── Single (article, concept) pipeline ─────────────────────────────

    def _run_single_count(self, concept, chunks, total_parts, confidence, job_idx, total_jobs):
        """Run Extract→Tally for one concept across pre-chunked article."""
        all_instances = []

        for i, chunk in enumerate(chunks):
            ui.set_status(f"🔢 [{job_idx}/{total_jobs}] Extract: chunk {i+1}/{total_parts} — 累計 {len(all_instances)}")
            instances = self._extract_from_chunk(concept, chunk, i + 1, total_parts, confidence)
            all_instances.extend(instances)

            chunk_high = sum(1 for x in instances if x.get('confidence') == 'high')
            chunk_med = sum(1 for x in instances if x.get('confidence') == 'medium')
            chunk_low = sum(1 for x in instances if x.get('confidence') == 'low')
            if instances:
                ui.info(f"      ✅ Chunk {i+1}/{total_parts}: +{len(instances)} "
                        f"(🟢{chunk_high} 🟡{chunk_med} 🔴{chunk_low}) | 累計: {len(all_instances)}")
            else:
                ui.info(f"      ⬜ Chunk {i+1}/{total_parts}: 0 | 累計: {len(all_instances)}")

        if not all_instances:
            return self._build_tally_locally(concept, [])

        ui.set_status(f"🔢 [{job_idx}/{total_jobs}] 正在整理找到的 {len(all_instances)} 個線索，合併重複項目")
        tally = self._tally_instances(concept, all_instances, total_parts)
        return tally

    # ── Concept parsing (multi) ────────────────────────────────────────

    def _parse_concepts(self, user_directive: str) -> list[str]:
        """Extract one or more counting targets from the user directive."""
        concepts = []

        # Match all "Count:" / "計算:" lines
        for match in re.finditer(r'(?:Count|計算|算)\s*[:：]\s*(.+)', user_directive, re.IGNORECASE):
            val = match.group(1).strip().rstrip('?？')
            if val:
                concepts.append(val)

        # Match bullet list items (- item) that follow a Count: header
        for match in re.finditer(r'(?:Count|計算|算)\s*[:：]\s*\n((?:\s*[-•]\s+.+\n?)+)', user_directive, re.IGNORECASE):
            block = match.group(1)
            for bullet in re.findall(r'[-•]\s+(.+)', block):
                val = bullet.strip().rstrip('?？')
                if val and val not in concepts:
                    concepts.append(val)

        if concepts:
            return concepts

        # Fallback: treat the remaining text (after removing commands/links) as a single concept
        cleaned = re.sub(r'(?:@ling-lens|@ling-count|/lens|/count)\b', '', user_directive, flags=re.IGNORECASE)
        cleaned = re.sub(r'\[\[.*?\]\]', '', cleaned).strip()
        # Remove confidence line
        cleaned = re.sub(r'(?:Confidence|信心)\s*[:：]\s*\S+', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = cleaned.strip('?？\n\r\t ')
        if len(cleaned) > 3:
            return [cleaned]

        return []

    # ── Article resolution (multi) ─────────────────────────────────────

    def _resolve_articles(self, target_titles: list[str], user_directive: str) -> list[tuple[str, str, str]]:
        """Resolve all target titles. Returns list of (title, text, resolved_path)."""
        results = []
        resolved_titles = set()

        for title in target_titles:
            if title in resolved_titles:
                continue
            text, resolved_path = self._find_in_pages(title)
            if text:
                results.append((title, text, resolved_path))
                resolved_titles.add(title)
                ui.info(f"   📄 已找到: [green]{title}[/green] ({len(text):,} chars)")
            else:
                ui.info(f"   ⚠️  未找到: [dim]{title}[/dim]")

        # Fallback: RAG search if nothing found via filesystem
        if not results and self.rag and user_directive:
            ui.info(f"   🔍 檔案系統未找到任何文章，嘗試 RAG 語意檢索...")
            try:
                rag_results = self.rag.query_similar_notes(user_directive, top_k=1)
                if rag_results:
                    ui.info(f"   📡 RAG 檢索成功")
                    results.append(("(RAG result)", rag_results[0], ""))
            except Exception:
                pass

        return results

    def _find_in_pages(self, title: str) -> tuple[str, str]:
        """Search pages/ for a file matching the title. Returns (text, resolved_path)."""
        title_clean = title.strip()
        if not title_clean:
            return "", ""

        folder = PAGES_DIR / title_clean
        if folder.is_dir():
            for pattern in [
                f"{title_clean} (Stitched).md",
                f"{title_clean} (Synthesis).md",
                f"{title_clean}.md",
            ]:
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
        system_prompt = self._load_prompt("agent_counter")
        if not system_prompt:
            system_prompt = (
                "You are a precise textual analyst. Your job is to scan source text "
                "and identify every instance of a user-defined concept. "
                "Return ONLY a valid JSON array. No markdown, no commentary."
            )

        user_prompt = f"""Target concept: "{concept}"
Minimum confidence: {confidence}

Source text (Part {part}/{total}):
{chunk}

Find every instance where the target concept appears in the source text.
Return a JSON array. Each element must have this exact structure:
{{
  "quote": "exact or near-exact quote from the source (max 120 chars)",
  "reasoning": "brief explanation of why this qualifies",
  "confidence": "high" or "medium" or "low",
  "closest_heading": "the exact text of the closest preceding markdown heading (without the # symbols)"
}}

Rules:
- Be thorough: scan every paragraph.
- Only include genuine instances — do not fabricate quotes.
- If zero instances found, return an empty array: []
- Return ONLY the JSON array, nothing else.
"""
        try:
            raw = self.llm.answer_query(user_prompt, wiki_context="", custom_instruction=system_prompt)
            instances = extract_json_array(raw)
            for inst in instances:
                inst["source_part"] = part
            return instances
        except Exception as e:
            ui.error(f"      ❌ Chunk {part} extraction 失敗: {e}")
            logging.error(f"LingLens extraction failed for chunk {part}: {e}")
            return []

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
        user_prompt = f"""Candidate instances of "{concept}" from a {total_parts}-part article:

{instances_json}

Tasks:
1. Remove exact duplicates (same quote from overlapping chunks).
2. Merge near-duplicates (keep the best quote).
3. Assign final confidence to each unique instance.
4. Number sequentially from 1.
5. Return JSON:
{{
  "concept": "{concept}",
  "total_count": <int>,
  "high_confidence_count": <int>,
  "medium_confidence_count": <int>,
  "low_confidence_count": <int>,
  "instances": [
    {{"id": 1, "quote": "...", "reasoning": "...", "confidence": "high|medium|low", "closest_heading": "..."}}
  ],
  "methodology_note": "Brief description"
}}
Return ONLY the JSON object.
"""
        try:
            raw = self.llm.answer_query(user_prompt, wiki_context="", custom_instruction=system_prompt)
            tally = extract_json_object(raw)
            if tally and "total_count" in tally:
                return tally
        except Exception as e:
            logging.error(f"LingLens tally pass failed: {e}")

        return self._build_tally_locally(concept, all_instances)

    def _build_tally_locally(self, concept, instances):
        seen = set()
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
            "concept": concept, "total_count": len(unique),
            "high_confidence_count": high, "medium_confidence_count": med, "low_confidence_count": low,
            "instances": unique,
            "methodology_note": "Extracted per-chunk, deduplicated by exact quote match.",
        }

    # ── Source grounding ───────────────────────────────────────────────

    def _ground_tally_locations(self, tally, article_text):
        """Attach deterministic source-location hints based on quoted source text."""
        for inst in tally.get("instances", []):
            quote = inst.get("quote", "")
            start = self._find_quote_offset(article_text, quote)
            if start < 0:
                continue
            inst["source_offset"] = start
            heading = self._closest_heading_before(article_text, start)
            part_anchor = self._closest_part_anchor_before(article_text, start)
            if part_anchor:
                inst["source_part_anchor"] = part_anchor
                source_range = self._part_source_range_for_anchor(article_text, part_anchor)
                if source_range:
                    inst["original_source_range"] = source_range
            if heading:
                inst["closest_heading"] = heading
                inst["source_anchor"] = part_anchor or heading

    def _find_quote_offset(self, article_text, quote):
        """Find an exact or normalized quote offset in the original source text."""
        quote = (quote or "").strip().strip('"“”')
        if not quote:
            return -1

        exact = article_text.find(quote)
        if exact >= 0:
            return exact

        normalized_quote = re.sub(r'\s+', ' ', quote)
        if len(normalized_quote) < 12:
            return -1

        # LLM quotes are capped, so a distinctive prefix is usually enough to
        # recover the location without trusting a fabricated heading.
        needle = re.escape(normalized_quote[:80])
        flexible = re.sub(r'\\ ', r'\\s+', needle)
        match = re.search(flexible, article_text, flags=re.DOTALL)
        if match:
            return match.start()
        return -1

    def _closest_heading_before(self, article_text, offset):
        """Return the closest preceding markdown heading text for an offset."""
        closest = ""
        for match in re.finditer(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', article_text[:offset], flags=re.MULTILINE):
            closest = match.group(1).strip()
        return closest

    def _closest_part_anchor_before(self, article_text, offset):
        """Return the closest Stitched article Part heading before an offset."""
        closest = ""
        for match in re.finditer(r'^\s{0,3}##\s+(Part\s+\d+)(?::.*?)?\s*#*\s*$', article_text[:offset], flags=re.MULTILINE):
            closest = match.group(1).strip()
        return closest

    def _part_source_range_for_anchor(self, article_text, part_anchor):
        pattern = rf'^\s{{0,3}}##\s+{re.escape(part_anchor)}(?::.*?)?\s*#*\s*$'
        match = re.search(pattern, article_text, flags=re.MULTILINE)
        if not match:
            return {}

        next_part = re.search(r'^\s{0,3}##\s+Part\s+\d+(?::.*?)?\s*#*\s*$', article_text[match.end():], flags=re.MULTILINE)
        section_end = match.end() + next_part.start() if next_part else len(article_text)
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

    # ── Report: Matrix format (multi-file × multi-concept) ─────────────

    def _format_matrix_report(self, concepts, articles, results_matrix):
        """Build a cross-tabulation report for multiple articles × concepts."""
        lines = ["# 🔎 LingLens — Cross Analysis", ""]

        # ── Summary matrix table ──
        lines.append("## 📊 Summary Matrix")
        lines.append("")
        header = "| Article |" + "|".join(f" {c} " for c in concepts) + "| Total |"
        sep = "|" + "|".join("---" for _ in range(len(concepts) + 2)) + "|"
        lines.extend([header, sep])

        for article_title, _, _ in articles:
            row_counts = []
            row_total = 0
            for concept in concepts:
                tally = results_matrix.get(article_title, {}).get(concept, {})
                count = tally.get("total_count", 0)
                row_counts.append(str(count))
                row_total += count
            lines.append(f"| [[{article_title}]] |" + "|".join(f" {c} " for c in row_counts) + f"| **{row_total}** |")

        # Column totals
        col_totals = []
        grand_total = 0
        for concept in concepts:
            col_sum = sum(
                results_matrix.get(a[0], {}).get(concept, {}).get("total_count", 0)
                for a in articles
            )
            col_totals.append(str(col_sum))
            grand_total += col_sum
        lines.append(f"| **Total** |" + "|".join(f" **{c}** " for c in col_totals) + f"| **{grand_total}** |")
        lines.append("")

        # ── Per-article × per-concept detail sections ──
        for article_title, _, resolved_path in articles:
            reference_title = self._reference_title(article_title, resolved_path)
            lines.append(f"---")
            lines.append(f"## 📄 {article_title}")
            lines.append("")
            for concept in concepts:
                tally = results_matrix.get(article_title, {}).get(concept, {})
                total = tally.get("total_count", 0)
                instances = tally.get("instances", [])
                lines.append(f"### 📌 {concept} — {total} instances")
                lines.append("")
                if not instances:
                    lines.append("> 未發現實例")
                    lines.append("")
                    continue
                lines.append(f"| # | Confidence | Quote | Reasoning | Reference |")
                lines.append(f"|---|------------|-------|-----------|--------|")
                conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}
                for inst in instances:
                    iid = inst.get("id", "?")
                    conf = inst.get("confidence", "medium")
                    emoji = conf_emoji.get(conf, "⚪")
                    quote = self._table_cell(inst.get("quote", ""), 80)
                    reasoning = self._table_cell(inst.get("reasoning", ""), 80)
                    heading = self._instance_anchor(inst)
                    reference = self._reference_cell(article_title, reference_title, resolved_path, heading, inst)
                    lines.append(f"| {iid} | {emoji} {conf} | {quote} | {reasoning} | {reference} |")
                lines.append("")

        # Navigation
        lines.append("---")
        lines.append("## 🔗 Navigation")
        for article_title, _, resolved_path in articles:
            reference_title = self._reference_title(article_title, resolved_path)
            lines.append(f"- [[{reference_title}|查看分析來源]]")
            original_title = self._original_source_title(article_title)
            if original_title != reference_title:
                lines.append(f"- [[{original_title}|查看原始檔]]")

        report = "\n".join(lines)
        report, _ = run_markdown_quality_checks(report)
        return report

    # ── Report: Single article × single concept (classic) ──────────────

    def _format_report(self, concept, article_title, tally, resolved_path=""):
        reference_title = self._reference_title(article_title, resolved_path)
        reference_label = "Analysis source" if self._is_stitched_path(resolved_path) else "Source"
        total = tally.get("total_count", 0)
        high = tally.get("high_confidence_count", 0)
        medium = tally.get("medium_confidence_count", 0)
        low = tally.get("low_confidence_count", 0)
        methodology = tally.get("methodology_note", "")
        instances = tally.get("instances", [])
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}

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
            iid = inst.get("id", "?")
            conf = inst.get("confidence", "medium")
            emoji = conf_emoji.get(conf, "⚪")
            lines.append(f"### Instance {iid} ({emoji} {conf.capitalize()})")
            lines.append(f'> "{inst.get("quote", "(no quote)")}"')
            lines.append("")
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
            if original_title != reference_title:
                range_text = self._original_range_text(inst)
                suffix = f" ({range_text})" if range_text else ""
                lines.append(f"**Original Reference**: {original_link}{suffix}")
            lines.append("")
        lines.extend(["---", "## 🔗 Navigation", f"- [[{reference_title}|查看分析來源]]"])
        if original_title != reference_title:
            lines.append(f"- [[{original_title}|查看原始檔]]")
        report = "\n".join(lines)
        report, _ = run_markdown_quality_checks(report)
        return report

    def _write_empty_report(self, concept, article_title):
        report = (
            f"# 🔎 LingLens: {concept}\n\n"
            f"> Source: [[{article_title}]] | Total instances found: **0**\n\n"
            f"No instances of **{concept}** were identified in the article.\n"
        )
        self._write_report(f"LingLens: {concept} in {article_title}",
                           report, "lens_report",
                           {"target_concept": concept, "source_article": article_title, "total_count": 0})
        return report

    def _error_report(self, message):
        report = f"# ❌ LingLens Error\n\n{message}\n"
        self._write_report("LingLens Error", report, "lens_report")
        return report

    # ── Helpers ────────────────────────────────────────────────────────


    def _source_link(self, article_title, heading="", alias="🔗原文"):
        """Build an Obsidian wikilink to the source article or heading."""
        safe_title = str(article_title).replace("|", "-").strip()
        safe_heading = str(heading or "").replace("|", "-").strip()
        target = f"{safe_title}#{safe_heading}" if safe_heading else safe_title
        return f"[[{target}|{alias}]]"

    def _reference_cell(self, article_title, reference_title, resolved_path, heading, inst):
        """Build a compact table reference with analysis and original links."""
        if heading:
            alias = "🔗分析錨點" if self._is_stitched_path(resolved_path) else "🔗原文錨點"
            analysis_link = self._source_link(reference_title, heading, alias)
        else:
            alias = "🔗分析來源" if self._is_stitched_path(resolved_path) else "🔗原文"
            analysis_link = self._source_link(reference_title, "", alias)

        original_title = self._original_source_title(article_title)
        range_text = self._original_range_text(inst)
        if original_title == reference_title:
            if range_text:
                return f"{analysis_link}<br>{range_text}"
            return analysis_link
        original_link = self._source_link(original_title, "", "🔗原始檔")
        if range_text:
            original_link = f"{original_link} ({range_text})"
        return f"{analysis_link}<br>{original_link}"

    def _instance_anchor(self, inst):
        """Return the best heading anchor recorded for an evidence instance."""
        anchor = inst.get("source_anchor") or inst.get("closest_heading") or ""
        return str(anchor).replace("#", "").strip()

    def _original_range_text(self, inst):
        source_range = inst.get("original_source_range") or {}
        start_line = source_range.get("start_line")
        end_line = source_range.get("end_line")
        if start_line and end_line:
            return f"原文 lines {start_line}-{end_line}"
        return ""

    def _reference_title(self, article_title, resolved_path=""):
        """Use the actual resolved note title for Obsidian references."""
        if resolved_path:
            path = Path(resolved_path)
            if path.suffix.lower() == ".md":
                return path.stem
        return article_title

    def _original_source_title(self, article_title):
        """Return the best available Obsidian title for the original source."""
        direct = PAGES_DIR / f"{article_title}.md"
        if direct.exists():
            return direct.stem

        raw = RAW_CONSOLIDATE_DIR / f"{article_title}.md"
        if raw.exists():
            return raw.stem

        for candidate in RAW_CONSOLIDATE_DIR.glob(f"{article_title}_*.md"):
            return candidate.stem

        return article_title

    def _is_stitched_path(self, resolved_path=""):
        return bool(resolved_path and Path(resolved_path).stem.endswith("(Stitched)"))

    def _table_cell(self, value, max_len=80):
        """Keep generated Markdown table cells on one row."""
        cell = re.sub(r'\s+', ' ', str(value or "")).strip()
        cell = cell.replace("|", "\\|")
        if len(cell) > max_len:
            cell = cell[: max_len - 1].rstrip() + "…"
        return cell
