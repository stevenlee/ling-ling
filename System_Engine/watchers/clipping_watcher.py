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
    RAW_CLIPPINGS_DIR, ASSETS_DIR, settings
)
from core.ui import ui
from core.vault_utils import update_wiki_index
from core.parser import dump_markdown_with_metadata

class ClippingWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.splitter = TextSplitter()

    def on_created(self, event):
        if event.is_directory:
            return
            
        if global_busy_state.is_busy():
            return
        
        filepath = Path(event.src_path)
        supported_extensions = ['.md', '.png', '.jpg', '.jpeg']
        if filepath.suffix.lower() not in supported_extensions:
            return
        
        global_busy_state.set_busy(True)
        try:
            ui.set_status(f"正在預處理剪輯：{filepath.name}")
            time.sleep(2)
            
            filepath = Path(event.src_path)
            if not filepath.exists():
                return
                
            ui.set_status(f"正在消化新資料：{filepath.name}")
            self.process_file(filepath)
            ui.success(f"資料消化完畢：{filepath.name}")
        except Exception as e:
            ui.error(f"資料處理失敗：{e}")
        finally:
            ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
            global_busy_state.set_busy(False)
        
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
            synthesis_file = entity_dir / f"{base_title}.md"
            
            # --- Pre-cleanup: Avoid zombie files from previous failed runs ---
            if entity_dir.exists() and not synthesis_file.exists():
                logging.info(f"Clipping: Found incomplete directory for {base_title}. Cleaning up...")
                shutil.rmtree(entity_dir)
            
            # Detect long document
            if len(content) > self.splitter.chunk_size + 1000:
                chunks = self.splitter.split_text(content)
                logging.info(f"Long document detected ({len(content)} chars). Splitting into {len(chunks)} parts.")
                
                master_tags = []
                pending_concepts = ""
                part_summaries = []
                total_output_chars = 0
                
                for i, chunk in enumerate(chunks):
                    ui.set_status(f"正在消化第 {i+1}/{len(chunks)} 部分...")
                    
                    context_hint = f"Part {i+1}/{len(chunks)}."
                    if i > 0 and pending_concepts:
                        context_hint += f" Previously you identified these pending concepts: {pending_concepts}. Please focus on them."
                    
                    if i < len(chunks) - 1:
                        context_hint += " Since more parts follow, PLEASE include a 'pending_concepts' field in your YAML."

                    part_info = {
                        "current": i + 1,
                        "total": len(chunks),
                        "master_tags": master_tags,
                        "context_hint": context_hint
                    }
                    result = self._ingest_to_wiki(chunk, filepath, part_info=part_info)
                    
                    if result:
                        if not master_tags and result.get('tags'):
                            master_tags = result.get('tags')
                        pending_concepts = result.get('pending_concepts', '')
                        
                        total_output_chars += len(result.get('content', ''))
                        first_line = result.get('content', '').strip().split('\n')[0][:100]
                        part_summaries.append(f"- [[{base_title} (Part {i+1})]]: {first_line}")

                # --- Final Synthesis ---
                ui.set_status(f"正在生成實體總結：{base_title}...")
                synthesis_text = self.llm.generate_synthesis(base_title, part_summaries, pending_concepts)
                
                
                final_meta = {
                    "title": base_title,
                    "tags": (master_tags or []) + ["synthesis", "completed"],
                    "status": "#PerfectPitch",
                    "date_completed": datetime.now().strftime("%Y-%m-%d"),
                    "model": self.llm.model,
                    "stats": {
                        "input_chars": len(content),
                        "output_chars": total_output_chars,
                        "parts": len(chunks)
                    }
                }
                
                final_content = f"""# ✨ {base_title} (Synthesis)
---

## 📝 Executive Summary
{synthesis_text}

## 📂 Navigation
{chr(10).join(part_summaries)}

## 🗺️ Knowledge Map
(Tags: {" ".join([f"#{t}" for t in (master_tags or []) if "part-" not in t])})

## 📊 System Metadata
- **Original Content Size**: {len(content)} chars
- **Generated Content Size**: {total_output_chars} chars
- **Total Parts**: {len(chunks)}
- **Model**: {self.llm.model}
- **Status**: #PerfectPitch
"""
                entity_dir.mkdir(parents=True, exist_ok=True)
                synthesis_file.write_text(dump_markdown_with_metadata(final_meta, final_content), encoding='utf-8')
                
                self.rag.add_document(synthesis_file, base_title, final_content, tags=final_meta["tags"])
                update_wiki_index(synthesis_file, base_title)
                
            else:
                self._ingest_to_wiki(content, filepath)
                
            # Archive
            dest = RAW_CLIPPINGS_DIR / filepath.name
            if dest.exists():
                dest = RAW_CLIPPINGS_DIR / f"{filepath.stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{filepath.suffix}"
            shutil.move(str(filepath), str(dest))
            ui.success(f"Clipping complete: [bold]{base_title}[/bold] (Synthesis generated)")
            
        except Exception as e:
            logging.error(f"Error handling markdown {filepath.name}: {e}")

    def _handle_image(self, filepath: Path):
        index_content = INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else ""
        result = process_image(filepath, self.llm, index_content, ASSETS_DIR)
        if result:
            self._ingest_to_wiki(None, filepath, llm_result=result)

    def _ingest_to_wiki(self, raw_content: str, source_filepath: Path, llm_result: dict = None, part_info: dict = None):
        try:
            if not llm_result:
                context_hint = part_info.get('context_hint', '') if part_info else ''
                index_content = INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else ""
                llm_result = self.llm.generate_entity_page(raw_content, source_filepath.name, index_content, context_hint=context_hint)
                if not llm_result:
                    raise ValueError("LLM generation failed.")
            
            base_title = source_filepath.stem.strip().replace("/", "-").replace("\\", "-")
            title = base_title
            
            if part_info:
                title = f"{base_title} (Part {part_info['current']})"
            
            tags = llm_result.get('tags', [])
            if part_info and part_info['master_tags']:
                tags = part_info['master_tags']
            
            page_type = llm_result.get('type', 'entity')
            body_content = llm_result.get('content', '')
            
            # Navigation
            if part_info:
                lang = settings.OUTPUT_LANGUAGE.lower()
                nav_labels = {
                    "chinese": ("← 上一頁", "下一頁 →", "第"),
                    "japanese": ("← 前へ", "次へ →", "第"),
                }.get(next((k for k in ["chinese", "japanese"] if k in lang), "english"), ("← Previous", "Next →", "Part"))
                
                nav = "\n\n---\n"
                if part_info['current'] > 1:
                    nav += f"[[{base_title} (Part {part_info['current']-1})|{nav_labels[0]}]] | "
                nav += f"{nav_labels[2]} {part_info['current']} / {part_info['total']}"
                if part_info['current'] < part_info['total']:
                    nav += f" | [[{base_title} (Part {part_info['current']+1})|{nav_labels[1]}]]"
                body_content += nav

            date_created = datetime.now().strftime("%Y-%m-%d")
            
            wiki_meta = {
                "title": title,
                "type": page_type,
                "date_created": date_created,
                "tags": tags
            }
            wiki_markdown = dump_markdown_with_metadata(wiki_meta, body_content)
            
            if part_info:
                page_folder = PAGES_DIR / base_title
                page_folder.mkdir(parents=True, exist_ok=True)
                page_path = page_folder / f"{title}.md"
            else:
                page_path = PAGES_DIR / f"{title}.md"

            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(wiki_markdown)
                
            self.rag.add_document(page_path, title, wiki_markdown, tags=tags)
            update_wiki_index(page_path, title)
            return llm_result
            
        except Exception as e:
            logging.error(f"Ingestion failed for {source_filepath.name}: {e}")
            return None

    def process_clipping(self, filepath: Path):
        self.process_file(filepath)
