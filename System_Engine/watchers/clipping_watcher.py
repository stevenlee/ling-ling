import time
import json
import logging
import shutil
import re
from pathlib import Path
from datetime import datetime
import watchdog.events

from core.state import global_busy_state
from services.text_splitter import TextSplitter
from services.media_processor import process_image
from core.config import (
    INDEX_FILE, LOG_FILE, EXCALIDRAW_DIR, PAGES_DIR, 
    RAW_CONSOLIDATE_DIR, RAW_ASSETS_DIR, ASSETS_DIR, settings
)
from core.ui import ui
from core.vault_utils import update_wiki_index
from core.parser import dump_markdown_with_metadata, run_markdown_quality_checks

class ClippingWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.splitter = TextSplitter()

    def on_created(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        if not event.is_directory:
            # ONLY handle if the file is being moved INTO the monitored directory
            from core.config import CONSOLIDATE_DIR
            dest_path = Path(event.dest_path)
            if CONSOLIDATE_DIR in dest_path.parents:
                self._handle_event(event, is_move=True)

    def _handle_event(self, event, is_move=False):
        if event.is_directory:
            return
            
        if global_busy_state.is_busy():
            return
        
        filepath = Path(event.dest_path) if is_move else Path(event.src_path)
        if filepath.name.startswith((".", "@")):
            return
            
        supported_extensions = ['.md', '.png', '.jpg', '.jpeg']
        if filepath.suffix.lower() not in supported_extensions:
            return
        
        global_busy_state.set_busy(True)
        try:
            ui.set_status(f"Preparing: {filepath.name}")
            time.sleep(1) # Small buffer for file system stability
            
            if not filepath.exists():
                return
                
            self.process_file(filepath)
            ui.success(f"Successfully Consolidated: {filepath.name}")
        except Exception as e:
            ui.error(f"Consolidation Failed: {e}")
        finally:
            ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
            global_busy_state.set_busy(False)

    def scan_existing(self):
        """Scan for files already in the directory at startup."""
        from core.config import CONSOLIDATE_DIR
        processed = 0
        if CONSOLIDATE_DIR.exists():
            supported_extensions = {'.md', '.png', '.jpg', '.jpeg'}
            for f in sorted(CONSOLIDATE_DIR.iterdir()):
                if (
                    f.is_file()
                    and not f.name.startswith((".", "@"))
                    and f.suffix.lower() in supported_extensions
                ):
                    ui.info(f"Startup scan found: {f.name}")
                    self.process_file(f)
                    if not f.exists():
                        processed += 1
        return processed
        
    def process_file(self, filepath: Path):
        ext = filepath.suffix.lower()
        if ext == '.md':
            self._handle_markdown(filepath)
        elif ext in ['.png', '.jpg', '.jpeg']:
            self._handle_image(filepath)

    def _handle_markdown(self, filepath: Path):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            base_title = filepath.stem
            entity_dir = PAGES_DIR / base_title
            synthesis_file = entity_dir / f"{base_title} (Synthesis).md"
            
            # Detect long document
            if len(content) > self.splitter.chunk_size + 1000:
                chunks = self.splitter.split_text(content)
                logging.info(f"Long document detected ({len(content)} chars). Splitting into {len(chunks)} parts.")
                
                master_tags = []
                pending_concepts = ""
                part_digests = []
                part_paths = []
                navigation_items = []
                total_output_chars = 0
                
                for i, chunk in enumerate(chunks):
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
                        "defer_rag": True
                    }
                    result = self._ingest_to_wiki(chunk, filepath, part_info=part_info)
                    
                    if result:
                        if not master_tags and result.get('tags'):
                            master_tags = result.get('tags')
                        pending_concepts = result.get('pending_concepts', '')
                        
                        part_content = result.get('content', '')
                        total_output_chars += len(part_content)
                        digest = self.llm.generate_part_digest(
                            base_title,
                            i + 1,
                            len(chunks),
                            chunk,
                            part_content,
                            pending_concepts
                        )
                        part_digests.append(digest)
                        self._append_part_digest_to_note(result, digest)
                        nav_summary = self._digest_value_to_text(digest.get('thesis')) if isinstance(digest, dict) else ""
                        if not nav_summary:
                            nav_summary = part_content.strip().split('\n')[0][:100]
                        navigation_items.append(f"- [[{base_title} (Part {i+1})]]: {nav_summary[:140]}")
                        if result.get("_page_path"):
                            part_paths.append(Path(result["_page_path"]))

                # --- Faithful stitched article ---
                ui.set_status(f"Stitching: {base_title}...")
                stitched_path = self._write_stitched_article(
                    base_title,
                    part_paths,
                    master_tags,
                    len(content),
                    total_output_chars
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
                    "synthesis_pipeline": "structured-digest-v1",
                    "digest_schema": "part-digest-v1",
                    "quality_checker": "deterministic-markdown-v1"
                }
                if synthesis_fixes:
                    final_meta["quality_fixes"] = synthesis_fixes
                
                # Synthesis Navigation
                syn_nav = f"\n\n---\n## 🔗 原始溯源\n*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**\n*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**"
                digest_appendix = self._format_digest_appendix(part_digests)
                
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
                
            else:
                self._ingest_to_wiki(content, filepath)
                
            # Archive
            dest = RAW_CONSOLIDATE_DIR / filepath.name
            if dest.exists():
                dest = RAW_CONSOLIDATE_DIR / f"{filepath.stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{filepath.suffix}"
            shutil.move(str(filepath), str(dest))
            ui.success(f"Clipping complete: [bold]{base_title}[/bold] (Synthesis generated)")
            
        except Exception as e:
            logging.error(f"Error handling markdown {filepath.name}: {e}")

    def _handle_image(self, filepath: Path):
        index_content = INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else ""
        result = process_image(filepath, self.llm, index_content, ASSETS_DIR)
        if result:
            ingested = self._ingest_to_wiki(None, filepath, llm_result=result)
            if ingested:
                self._archive_processed_file(filepath, RAW_ASSETS_DIR)

    def _archive_processed_file(self, filepath: Path, archive_dir: Path):
        if not filepath.exists():
            return

        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / filepath.name
        if dest.exists():
            dest = archive_dir / f"{filepath.stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{filepath.suffix}"
        shutil.move(str(filepath), str(dest))

    def _ingest_to_wiki(self, raw_content: str, source_filepath: Path, llm_result: dict = None, part_info: dict = None):
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

    def process_clipping(self, filepath: Path):
        self.process_file(filepath)

    def _format_digest_appendix(self, part_digests: list) -> str:
        if not part_digests:
            return ""

        def as_text(value) -> str:
            return self._digest_value_to_text(value)

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

    def _digest_value_to_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            return "; ".join(self._digest_value_to_text(item) for item in value if self._digest_value_to_text(item))
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    def _append_part_digest_to_note(self, ingest_result: dict, digest):
        page_path_value = ingest_result.get("_page_path") if isinstance(ingest_result, dict) else None
        if not page_path_value:
            return

        page_path = Path(page_path_value)
        if not page_path.exists():
            return

        appendix = self._format_digest_appendix([digest])
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

    def _write_stitched_article(self, base_title: str, part_paths: list[Path], tags: list[str], input_chars: int, output_chars: int) -> Path | None:
        readable_parts = [path for path in part_paths if path and path.exists()]
        if not readable_parts:
            return None

        stitched_sections = []
        for index, part_path in enumerate(readable_parts, 1):
            body = self._extract_stitchable_body(part_path)
            if not body:
                continue

            stitched_sections.append(
                f"## Part {index}: [[{part_path.stem}]]\n\n"
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
