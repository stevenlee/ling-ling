"""
IngestionPipeline — extracted from ClippingWatcher.

Handles the core document processing logic:
  - Single-page wiki ingestion
  - Long-document chunking, part-by-part digestion, stitching, and synthesis
  - Digest formatting and appendix generation
  - Wiki page writing with navigation links
"""
import json
import logging
import re
from pathlib import Path
from datetime import datetime

from services.text_splitter import TextSplitter
from core.config import INDEX_FILE, PAGES_DIR, settings
from core.ui import ui
from core.vault_utils import update_wiki_index
from core.parser import parse_markdown_metadata, dump_markdown_with_metadata, run_markdown_quality_checks
from core.utils import digest_value_to_text


class IngestionPipeline:
    """Orchestrates the conversion of raw content into wiki pages."""

    def __init__(self, llm_client, rag_manager):
        self.llm = llm_client
        self.rag = rag_manager
        self.splitter = TextSplitter()

    # ── Public Entry Points ──────────────────────────────────────────

    def ingest_markdown(self, content: str, source_filepath: Path):
        """Route a markdown document through single-page or long-document pipeline."""
        base_title = source_filepath.stem

        if len(content) > self.splitter.chunk_size + 1000:
            self._ingest_long_document(content, source_filepath, base_title)
        else:
            self.ingest_to_wiki(content, source_filepath)

    def ingest_to_wiki(self, raw_content: str, source_filepath: Path, llm_result: dict = None, part_info: dict = None):
        """Convert raw content (or a pre-existing LLM result) into a wiki page."""
        try:
            if not llm_result:
                context_hint = part_info.get('context_hint', '') if part_info else ''
                index_content = INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else ""
                llm_result = self.llm.generate_entity_page(raw_content, source_filepath.name, index_content, context_hint=context_hint)
                if not llm_result:
                    raise ValueError("LLM generation failed.")

            base_title = source_filepath.stem.strip().replace("/", "-").replace("\\", "-")
            title = f"{base_title} (Synthesis)"

            if part_info:
                title = f"{base_title} (Part {part_info['current']})"

            tags = llm_result.get('tags', [])
            if part_info and part_info['master_tags']:
                tags = part_info['master_tags']

            page_type = llm_result.get('type', 'entity')
            body_content = llm_result.get('content', '')
            body_content, quality_fixes = run_markdown_quality_checks(body_content, strip_frontmatter=True)

            # Enhanced Navigation
            nav = "\n\n---\n## 🔗 知識導航\n"
            if part_info:
                nav += f"*   🔙 **[[{base_title} (Synthesis)|查看全文總結 (Synthesis)]]**\n"
                nav += f"*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**\n"
                nav += f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**\n"

                adj_links = []
                if part_info['current'] > 1:
                    adj_links.append(f"[[{base_title} (Part {part_info['current']-1})|◀ 上一篇]]")
                if part_info['current'] < part_info['total']:
                    adj_links.append(f"[[{base_title} (Part {part_info['current']+1})|下一篇 ▶]]")

                if adj_links:
                    nav += f"*   📑 {' | '.join(adj_links)}\n"
            else:
                # Single-page synthesis navigation
                nav += f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**\n"

            body_content += nav

            date_created = datetime.now().strftime("%Y-%m-%d")

            wiki_meta = {
                "title": title,
                "type": page_type,
                "date_created": date_created,
                "tags": tags,
                "quality_checker": "deterministic-markdown-v1"
            }
            if part_info:
                wiki_meta["part"] = part_info["current"]
                wiki_meta["parts_count"] = part_info["total"]
                wiki_meta["digest_schema"] = "part-digest-v1"
                source_span = part_info.get("source_span") or {}
                wiki_meta.update(source_span)
            if quality_fixes:
                wiki_meta["quality_fixes"] = quality_fixes
            wiki_markdown = dump_markdown_with_metadata(wiki_meta, body_content)

            # Always save in a dedicated entity folder
            page_folder = PAGES_DIR / base_title
            page_folder.mkdir(parents=True, exist_ok=True)
            page_path = page_folder / f"{title}.md"

            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(wiki_markdown)

            if not (part_info and part_info.get("defer_rag")):
                self.rag.add_document(page_path, title, wiki_markdown, tags=tags)
            update_wiki_index(page_path, title)
            llm_result["_page_path"] = str(page_path)
            llm_result["_title"] = title
            llm_result["_tags"] = tags
            return llm_result

        except Exception as e:
            logging.error(f"Ingestion failed for {source_filepath.name}: {e}")
            return None

    # ── Long-Document Pipeline ───────────────────────────────────────

    def _ingest_long_document(self, content: str, source_filepath: Path, base_title: str):
        """Multi-part chunking → per-part digestion → stitching → synthesis."""
        entity_dir = PAGES_DIR / base_title
        synthesis_file = entity_dir / f"{base_title} (Synthesis).md"

        chunk_spans = self.splitter.split_text_with_spans(content)
        chunks = [item["text"] for item in chunk_spans]
        source_spans = [
            self._source_span_for_chunk(content, span, i + 1)
            for i, span in enumerate(chunk_spans)
        ]
        logging.info(f"Long document detected ({len(content)} chars). Splitting into {len(chunks)} parts.")

        master_tags = []
        pending_concepts = ""
        part_digests = []
        part_paths = []
        navigation_items = []
        total_output_chars = 0

        for i, chunk in enumerate(chunks):
            source_span = source_spans[i]
            ui.set_status(f"Distilling Part {i+1} of {len(chunks)}...")

            context_hint = f"Part {i+1}/{len(chunks)}."
            if i > 0 and pending_concepts:
                context_hint += f" Previously you identified these pending concepts: {pending_concepts}. Please focus on them."

            if i < len(chunks) - 1:
                context_hint += " Since more parts follow, PLEASE include a 'pending_concepts' field in your YAML."

            part_info = {
                "current": i + 1,
                "total": len(chunks),
                "master_tags": master_tags,
                "context_hint": context_hint,
                "defer_rag": True,
                "source_span": source_span
            }
            result = self.ingest_to_wiki(chunk, source_filepath, part_info=part_info)

            if result:
                if not master_tags and result.get('tags'):
                    master_tags = result.get('tags')
                pending_concepts = result.get('pending_concepts', '')

                part_content = result.get('content', '')
                total_output_chars += len(part_content)
                digest = self.llm.generate_part_digest(
                    base_title, i + 1, len(chunks),
                    chunk, part_content, pending_concepts
                )
                part_digests.append(digest)
                self._append_part_digest_to_note(result, digest)
                nav_summary = digest_value_to_text(digest.get('thesis')) if isinstance(digest, dict) else ""
                if not nav_summary:
                    nav_summary = part_content.strip().split('\n')[0][:100]
                navigation_items.append(f"- [[{base_title} (Part {i+1})]]: {nav_summary[:140]}")
                if result.get("_page_path"):
                    part_paths.append(Path(result["_page_path"]))

        # --- Faithful stitched article ---
        ui.set_status(f"Stitching: {base_title}...")
        stitched_path = self._write_stitched_article(
            base_title, part_paths, master_tags, len(content), total_output_chars
        )
        if stitched_path:
            navigation_items.append(f"- [[{base_title} (Stitched)]]: 忠實接合版，保留 Part notes 的主要內容")

        # --- Final Synthesis ---
        ui.set_status(f"Synthesizing: {base_title}...")
        synthesis_text = self.llm.generate_synthesis(base_title, part_digests, pending_concepts)
        synthesis_text, synthesis_fixes = run_markdown_quality_checks(synthesis_text, strip_frontmatter=True)

        from core.version import VERSION
        final_meta = {
            "title": f"{base_title} (Synthesis)",
            "tags": (master_tags or []) + ["synthesis", "completed"],
            "status": "#PerfectPitch",
            "version": VERSION,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": len(content),
            "output_chars": total_output_chars,
            "parts_count": len(chunks),
            "part_source_map": source_spans,
            "synthesis_pipeline": "structured-digest-v1",
            "digest_schema": "part-digest-v1",
            "quality_checker": "deterministic-markdown-v1"
        }
        if synthesis_fixes:
            final_meta["quality_fixes"] = synthesis_fixes

        # Synthesis Navigation
        syn_nav = f"\n\n---\n## 🔗 原始溯源\n*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**\n*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**"
        digest_appendix = self.format_digest_appendix(part_digests)

        final_content = f"""# ✨ {base_title} (Synthesis)
---

## 📝 Executive Summary
{synthesis_text}

## 📂 Navigation
{chr(10).join(navigation_items)}{syn_nav}

{digest_appendix}

## 🗺️ Knowledge Map
(Tags: {" ".join([f"#{t}" for t in (master_tags or []) if "part-" not in t])})

## 📊 System Metadata
- **Original Content Size**: {len(content)} chars
- **Generated Content Size**: {total_output_chars} chars
- **Total Parts**: {len(chunks)}
- **Model**: {self.llm.model}
- **Status**: #PerfectPitch
"""
        final_content, final_fixes = run_markdown_quality_checks(final_content)
        if final_fixes:
            final_meta["quality_fixes"] = sorted(set(final_meta.get("quality_fixes", []) + final_fixes))
        entity_dir.mkdir(parents=True, exist_ok=True)
        synthesis_file.write_text(dump_markdown_with_metadata(final_meta, final_content), encoding='utf-8')

        self.rag.add_document(synthesis_file, base_title, final_content, tags=final_meta["tags"])
        update_wiki_index(synthesis_file, base_title)

    # ── Digest Formatting ────────────────────────────────────────────

    def format_digest_appendix(self, part_digests: list) -> str:
        """Format structured part digests into a readable Markdown appendix."""
        if not part_digests:
            return ""

        def as_text(value) -> str:
            return digest_value_to_text(value)

        def clean_list(values):
            if not values:
                return []
            if isinstance(values, str):
                values = [values]
            return [as_text(value) for value in values if as_text(value)]

        def bullet_block(label: str, values) -> list[str]:
            items = clean_list(values)
            if not items:
                return []
            lines = [f"- **{label}**:"]
            lines.extend(f"  - {item}" for item in items)
            return lines

        lines = [
            "## 🧩 Part Digest Appendix",
            "",
            "> 每個 Part 的結構化摘要。這是 Ling Ling 進行總合成前的中間理解，可用來檢查 final synthesis 是否有根據。",
            ""
        ]

        for index, digest in enumerate(part_digests, 1):
            if isinstance(digest, str):
                lines.extend([f"### Part {index}", "", digest.strip(), ""])
                continue
            if not isinstance(digest, dict):
                lines.extend([f"### Part {index}", "", str(digest or "(empty digest)"), ""])
                continue

            part_number = digest.get("part", index)
            title = digest.get("title") or f"Part {part_number}"
            lines.extend([f"### Part {part_number}: {title}", ""])

            thesis = as_text(digest.get("thesis", ""))
            if thesis:
                lines.append(f"- **Thesis**: {thesis}")

            lines.extend(bullet_block("Key Points", digest.get("key_points", [])))
            lines.extend(bullet_block("Evidence", digest.get("evidence", [])))
            lines.extend(bullet_block("Terms", digest.get("terms", [])))
            lines.extend(bullet_block("Open Questions", digest.get("open_questions", [])))

            handoff = as_text(digest.get("handoff", ""))
            if handoff:
                lines.append(f"- **Handoff**: {handoff}")

            lines.append("")

        return "\n".join(lines).strip()

    def _append_part_digest_to_note(self, ingest_result: dict, digest):
        page_path_value = ingest_result.get("_page_path") if isinstance(ingest_result, dict) else None
        if not page_path_value:
            return

        page_path = Path(page_path_value)
        if not page_path.exists():
            return

        appendix = self.format_digest_appendix([digest])
        if not appendix:
            return

        content = page_path.read_text(encoding='utf-8').rstrip()
        if "## 🧩 Part Digest Appendix" in content:
            content = content.split("## 🧩 Part Digest Appendix", 1)[0].rstrip()

        updated = f"{content}\n\n{appendix}\n"
        page_path.write_text(updated, encoding='utf-8')

        title = ingest_result.get("_title") or page_path.stem
        tags = ingest_result.get("_tags") or ingest_result.get("tags", [])
        self.rag.add_document(page_path, title, updated, tags=tags)

    # ── Stitched Article ─────────────────────────────────────────────

    def _write_stitched_article(self, base_title: str, part_paths: list[Path], tags: list[str], input_chars: int, output_chars: int) -> Path | None:
        readable_parts = [path for path in part_paths if path and path.exists()]
        if not readable_parts:
            return None

        stitched_sections = []
        for index, part_path in enumerate(readable_parts, 1):
            part_meta = parse_markdown_metadata(part_path.read_text(encoding="utf-8"))
            body = self._extract_stitchable_body(part_path)
            if not body:
                continue

            source_range = self._format_source_range(part_meta)
            stitched_sections.append(
                f"## Part {index}\n\n"
                f"Source note: [[{part_path.stem}]]\n\n"
                f"{source_range}"
                f"<!-- source: {part_path.name} -->\n\n"
                f"{body}"
            )

        if not stitched_sections:
            return None

        from core.version import VERSION
        metadata = {
            "title": f"{base_title} (Stitched)",
            "type": "stitched_article",
            "tags": sorted(set((tags or []) + ["stitched", "longform"])),
            "status": "#FaithfulStitch",
            "version": VERSION,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "parts_count": len(readable_parts),
            "stitch_pipeline": "part-note-stitch-v1",
            "quality_checker": "deterministic-markdown-v1"
        }

        body = f"""# {base_title} (Stitched)

> 忠實接合版：保留各 Part note 的主要內容，移除每篇的 navigation、metadata 與 digest appendix。這份文件偏向完整閱讀，不等同於洞察型 Synthesis。

## 🔗 Navigation
- [[{base_title} (Synthesis)|查看洞察總結 (Synthesis)]]
- [[{base_title}|查看完整原始檔 (Original)]]

---

{chr(10).join(f'{section}{chr(10)}' for section in stitched_sections)}
"""
        body, quality_fixes = run_markdown_quality_checks(body)
        if quality_fixes:
            metadata["quality_fixes"] = quality_fixes

        stitched_file = PAGES_DIR / base_title / f"{base_title} (Stitched).md"
        stitched_file.parent.mkdir(parents=True, exist_ok=True)
        stitched_markdown = dump_markdown_with_metadata(metadata, body)
        stitched_file.write_text(stitched_markdown, encoding="utf-8")

        self.rag.add_document(stitched_file, f"{base_title} (Stitched)", stitched_markdown, tags=metadata["tags"])
        update_wiki_index(stitched_file, f"{base_title} (Stitched)")
        return stitched_file

    # ── Helpers ───────────────────────────────────────────────────────

    def _source_span_for_chunk(self, source_text: str, chunk_span: dict, part_number: int) -> dict:
        start = int(chunk_span.get("start", 0))
        end = int(chunk_span.get("end", start))
        return {
            "part": part_number,
            "source_start_char": start,
            "source_end_char": end,
            "source_start_line": source_text.count("\n", 0, start) + 1,
            "source_end_line": source_text.count("\n", 0, end) + 1,
        }

    def _format_source_range(self, part_meta: dict) -> str:
        start_line = part_meta.get("source_start_line")
        end_line = part_meta.get("source_end_line")
        start_char = part_meta.get("source_start_char")
        end_char = part_meta.get("source_end_char")
        lines = []
        if start_line and end_line:
            lines.append(f"Original range: lines {start_line}-{end_line}")
        if start_char is not None and end_char is not None:
            lines.append(f"Original chars: {start_char}-{end_char}")
        if not lines:
            return ""
        return "\n".join(lines) + "\n\n"

    def _extract_stitchable_body(self, part_path: Path) -> str:
        content = part_path.read_text(encoding="utf-8")
        content = re.sub(r'^---\s*\n.*?\n---\s*\n?', '', content, count=1, flags=re.DOTALL).strip()

        cut_markers = [
            "\n## 🔗 知識導航",
            "\n## 🧩 Part Digest Appendix",
        ]
        cut_positions = [content.find(marker) for marker in cut_markers if content.find(marker) != -1]
        if cut_positions:
            content = content[:min(cut_positions)].rstrip()

        content = self._demote_headings(content, levels=2)
        content, _ = run_markdown_quality_checks(content)
        return content.strip()

    def _demote_headings(self, markdown: str, levels: int = 1) -> str:
        def replace(match: re.Match) -> str:
            hashes = match.group(1)
            text = match.group(2)
            return f"{'#' * min(len(hashes) + levels, 6)} {text}"

        return re.sub(r'^(#{1,6})\s+(.+)$', replace, markdown, flags=re.MULTILINE)
