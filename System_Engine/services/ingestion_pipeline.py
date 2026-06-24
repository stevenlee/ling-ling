"""Ingestion pipeline — turns raw markdown into wiki pages.

Two flows:
  - **Single-page**: short doc → one LLM call → one wiki note.
  - **Long-document**: chunk → per-part LLM → stitched + synthesis.

Originally extracted from ClippingWatcher.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from core.config import (
    FACET_INDEX_ENABLED,
    FACET_MAX_PER_DOC,
    FROM_LLM_DIR,
    INDEX_FILE,
    PAGES_DIR,
    PROFILES_DIR,
    PROFILES_PENDING_DIR,
    SYNTHESIS_CRITIQUE_ENABLED,
    SYNTHESIS_CRITIQUE_MAX_RETRIES,
    THOUGHTFUL_EMIT_SUMMARY,
    THOUGHTFUL_USE_LLM_FOR_INGEST,
    TEMPLATES_DIR,
    USE_THOUGHTFUL_SPLITTER,
    settings,
    SCRIPTURE_DIR,
)
from core.parser import (
    dump_markdown_with_metadata,
    parse_markdown_metadata,
    strip_body_frontmatter,
    run_markdown_quality_checks,
)
from core.ui import ui
from core.utils import digest_value_to_text
from core.vault_utils import update_wiki_index
from services.profile_manager import ProfileManager
from services.text_splitter import TextSplitter


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_FRONTMATTER_RE = re.compile(r'^---\s*\n.*?\n---\s*\n?', re.DOTALL)
_PART_DIGEST_HEADER = "## 🧩 Part Digest Appendix"
# Auto-attached learning-artifact section (visual_router). Prefix shared by all
# emitted sections so re-runs can strip and regenerate them idempotently.
_ARTIFACT_HEADER = "## 🖼️ 學習輔助"
_CRITIQUE_HEADER = "## 🔍 Quality Critique"
# Verdicts come from Operations/critique.md ("keep, revise, or reject").
# The model is allowed to use either English or zh-translated equivalents, and
# often wraps the keyword in prose ("應修正 (revise)" — observed live on
# gemma), so allow a short gap after the colon and take the first keyword on
# the line. A negated revise ("不需修正") counts as keep.
_VERDICT_RE = re.compile(
    r'(?im)^\**\s*Overall\s+Verdict\**\s*[:：][^\n]{0,40}?(keep|revise|reject|保留|修訂|修正|重做|拒絕)',
)
_VERDICT_NEGATION_RE = re.compile(r'(不需|不必|無需|无需|毋須|毋须)\s*$')
_VERDICT_NORMALISE = {
    "keep": "keep", "保留": "keep",
    "revise": "revise", "修訂": "revise", "修正": "revise",
    "reject": "reject", "重做": "reject", "拒絕": "reject",
}


class IngestionPipeline:
    """Orchestrates raw content → wiki pages."""

    def __init__(self, llm_client, rag_manager):
        self.llm = llm_client
        self.rag = rag_manager
        # Splitter selection is Scripture-overridable (settings.*), not just env.
        # ThoughtfulSplitter's `split_text_with_spans` returns dicts with
        # extra `section_path` / `boundary_type` fields; TextSplitter's
        # returns the lean `{text, start, end}` shape. Both work with the
        # downstream `chunk_spans[i].get(...)` reads below.
        if settings.USE_THOUGHTFUL_SPLITTER:
            from services.thoughtful_splitter import ThoughtfulSplitter
            self.splitter = ThoughtfulSplitter(
                default_use_llm=settings.THOUGHTFUL_USE_LLM_FOR_INGEST,
                default_emit_summary=THOUGHTFUL_EMIT_SUMMARY,
                llm=self.llm,  # Phase 4 topic-shift detector reaches the LLM through this
            )
        else:
            self.splitter = TextSplitter()

    def load_profiles(self) -> ProfileManager:
        """Build the profile registry, migrating legacy DocType.md once.

        Constructed fresh per ingest run so vault edits take effect
        immediately; the scan reads a handful of small files.
        """
        pm = ProfileManager(PROFILES_DIR, pending_dir=PROFILES_PENDING_DIR)
        if pm.is_empty():
            pm.migrate_from_doctype(SCRIPTURE_DIR / "DocType.md")
        return pm

    # ── Public entry points ──────────────────────────────────────────

    def ingest_markdown(self, content: str, source_filepath: Path):
        # Source pre-passes (in-memory, never edits the source file):
        #   0c strip_boilerplate — drop Gutenberg license/TOC.
        #   0b normalize_structure — promote plain-text chapter cues to markdown
        #      headings, but only for docs that lack markdown structure.
        from services.source_prep import strip_boilerplate, normalize_structure
        content, _stripped = strip_boilerplate(content)
        content, _normed = normalize_structure(content)
        if _stripped or _normed:
            logging.info(
                f"Source prep on {source_filepath.name}: strip={_stripped} normalize={_normed}"
            )
        meta = parse_markdown_metadata(content)
        doc_config = self._resolve_routing(meta, content, source_filepath)

        if len(content) > self.splitter.chunk_size + 1000:
            self._ingest_long_document(content, source_filepath, source_filepath.stem, doc_config=doc_config)
        else:
            result = self.ingest_to_wiki(content, source_filepath, doc_config=doc_config)
            self._index_short_doc_facets(content, result)

    # ── Facet index (summary-as-pointer retrieval) ───────────────────

    @staticmethod
    def _facets_from_digest(digest) -> list[str]:
        """Extract facet sentences (thesis + key points) from a part digest.

        Facets are the retrieval pointers for the facet index — short,
        clean, query-shaped sentences. Non-dict digests (fallback strings,
        mocks) yield nothing.
        """
        if not isinstance(digest, dict):
            return []
        facets: list[str] = []
        thesis = digest_value_to_text(digest.get("thesis"))
        if thesis:
            facets.append(thesis)
        key_points = digest.get("key_points") or []
        if isinstance(key_points, str):
            key_points = [key_points]
        if isinstance(key_points, (list, tuple)):
            for point in key_points:
                text = digest_value_to_text(point)
                if text:
                    facets.append(text)
        # Dedup (preserving order), drop fragments, cap per document.
        seen = set()
        out = []
        for f in facets:
            f = f.strip()
            if len(f) < 8 or f in seen:
                continue
            seen.add(f)
            out.append(f)
        return out[:FACET_MAX_PER_DOC]

    def _index_digest_facets(self, page_path_value, title, digest, tags=None) -> None:
        """Phase A: register a page's digest facets as retrieval pointers."""
        if not FACET_INDEX_ENABLED or not page_path_value or not title:
            return
        facets = self._facets_from_digest(digest)
        if not facets:
            return
        try:
            self.rag.add_facets(Path(page_path_value), title, facets, tags=tags)
        except Exception as e:
            logging.warning(f"Facet indexing failed for {title}: {e}")

    def _index_short_doc_facets(self, raw_content: str, ingest_result: dict | None) -> None:
        """Phase B: short docs have no part digests, so spend one light LLM
        call to produce one — then index its facets. Fail-soft throughout."""
        if not FACET_INDEX_ENABLED or not isinstance(ingest_result, dict):
            return
        page_path = ingest_result.get("_page_path")
        title = ingest_result.get("_title")
        if not page_path or not title:
            return
        try:
            digest = self.llm.generate_part_digest(
                title, 1, 1, raw_content, ingest_result.get("content", ""), "",
            )
        except Exception as e:
            logging.warning(f"Short-doc digest for facets failed for {title}: {e}")
            return
        self._index_digest_facets(page_path, title, digest, tags=ingest_result.get("_tags"))

    # ── Profile routing ──────────────────────────────────────────────

    def _resolve_routing(self, meta: dict, content: str, source_filepath: Path) -> dict:
        """Resolve synthesis persona/template via the profile registry.

        Resolution layers, highest priority first:
          1. Explicit frontmatter overrides (`synthesis_persona`,
             `synthesis_template`, or a `profile` name).
          2. A registered profile matching `document_type`/`type`, else the
             LLM's closed-choice pick among registered profiles.
          3. The `default` profile; Scripture settings as the last resort.

        Unknown document kinds trigger a pending-review bundle (never
        activated silently) and fall back to layer 3 for this run.
        """
        synthesis_persona = meta.get("synthesis_persona")
        synthesis_template = meta.get("synthesis_template")

        pm = self.load_profiles()
        profile = None
        layer = "frontmatter_override"
        pending_queued = False
        doc_type = meta.get("document_type") or meta.get("type")
        doc_type = doc_type.lower().strip() if isinstance(doc_type, str) else None

        if not (synthesis_persona and synthesis_template):
            # Layer 1b: explicit profile name in frontmatter.
            profile = pm.get(meta.get("profile")) or pm.get(doc_type)
            layer = "frontmatter_profile"

            # Layer 2: closed-choice LLM selection among registered profiles.
            if profile is None:
                content_prefix = self._classification_prefix(content)
                choice = self.llm.select_profile(
                    source_filepath.name, content_prefix, pm.selection_options()
                )
                if isinstance(choice, str) and choice != "none":
                    profile = pm.get(choice)
                    layer = "llm_selection"

                # No fit: draft a new bundle for review, then fall through to
                # the default profile for this run (quality over immediacy).
                if profile is None:
                    pending_queued = self._queue_new_profile(
                        pm, doc_type, source_filepath, content_prefix
                    )

            # Layer 3: the default profile.
            if profile is None:
                profile = pm.get("default")
                layer = "default_profile" if profile else "settings_fallback"
            if profile is not None:
                synthesis_persona = synthesis_persona or profile.persona
                synthesis_template = synthesis_template or profile.template

        doc_config = {
            "ingest_persona": meta.get("ingest_persona") or "translator",
            "ingest_template": meta.get("ingest_template") or "translation-rpt",
            "synthesis_persona": synthesis_persona or settings.AGENT_ROLE or "none",
            "synthesis_template": synthesis_template or settings.USE_TEMPLATE or "wiki-note",
            "doc_type": doc_type or (profile.name if profile else "default"),
            "profile": profile.name if profile else None,
            "operations": list(profile.operations) if profile else [],
        }
        self._record_routing_decision(
            source_filepath, doc_config, layer=layer, pending_queued=pending_queued
        )
        return doc_config

    def _record_routing_decision(
        self,
        source_filepath: Path,
        doc_config: dict,
        *,
        layer: str,
        pending_queued: bool,
    ) -> None:
        """Persist the routing outcome as a `routing_decision` artifact.

        Layers: frontmatter_override / frontmatter_profile / llm_selection /
        default_profile / settings_fallback. The routing health report
        aggregates these to surface fallback rates and unused profiles.
        """
        if not hasattr(self.llm, "trace_store"):
            return
        try:
            self.llm.trace_store.record_artifact(
                path=source_filepath,
                artifact_type="routing_decision",
                title=source_filepath.name,
                metadata={
                    "layer": layer,
                    "profile": doc_config.get("profile"),
                    "doc_type": doc_config.get("doc_type"),
                    "synthesis_persona": doc_config.get("synthesis_persona"),
                    "synthesis_template": doc_config.get("synthesis_template"),
                    "fellback_to_default": layer in ("default_profile", "settings_fallback"),
                    "pending_queued": pending_queued,
                },
            )
        except Exception as e:
            logging.debug(f"Routing decision trace write failed: {e}")

    @staticmethod
    def _template_stamp(template_name: str | None) -> dict:
        """Page-frontmatter stamp recording which template (and version)
        generated the page. Versions come from the template's own
        frontmatter `version:` key; unversioned templates stamp name only.
        The template audit task compares these stamps against current
        template versions to find pages rendered with outdated layouts."""
        if not template_name or template_name == "none":
            return {}
        stamp = {"template": template_name}
        try:
            name = template_name if template_name.endswith(".md") else f"{template_name}.md"
            template_file = TEMPLATES_DIR / name
            if template_file.exists():
                meta = parse_markdown_metadata(template_file.read_text(encoding="utf-8"))
                version = meta.get("version")
                if version is not None:
                    stamp["template_version"] = version
        except Exception as e:
            logging.debug(f"Template stamp failed for {template_name}: {e}")
        return stamp

    @staticmethod
    def _classification_prefix(content: str) -> str:
        """First 500 chars of the body, with any frontmatter stripped."""
        clean_content = content
        if content.startswith("---"):
            match = _FRONTMATTER_RE.match(content)
            if match:
                clean_content = content[match.end():]
        return clean_content[:500]

    def _queue_new_profile(
        self,
        pm: ProfileManager,
        doc_type: str | None,
        source_filepath: Path,
        content_prefix: str,
    ) -> bool:
        """Draft persona/template/profile for an unrecognized category into
        _pending/. Fail-soft: routing falls back to `default` regardless.
        Returns True when a new bundle was queued."""
        try:
            category = doc_type or self.llm.classify_document(
                source_filepath.name, content_prefix
            )
            if not isinstance(category, str):
                return False
            category = re.sub(r'[^a-z0-9\-]', '', category.lower().strip())
            if not category or pm.get(category) or pm.has_pending(category):
                return False

            gen = self.llm.generate_persona_and_template(category)
            if not isinstance(gen, dict) or "Mock" in type(gen).__name__:
                return False
            persona_name = gen.get("persona_name")
            persona_content = gen.get("persona_content")
            template_name = gen.get("template_name")
            template_content = gen.get("template_content")
            if not all(
                isinstance(v, str) and v
                for v in (persona_name, persona_content, template_name, template_content)
            ):
                return False

            persona_name = re.sub(r'[^a-zA-Z0-9\-]', '', persona_name.replace(".md", ""))
            template_name = re.sub(r'[^a-zA-Z0-9\-]', '', template_name.replace(".md", ""))
            pm.queue_pending(
                profile_name=category,
                persona_name=persona_name,
                persona_content=persona_content,
                template_name=template_name,
                template_content=template_content,
                description=f"Auto-generated for {category}",
                notify_dir=FROM_LLM_DIR,
            )
            ui.info(f"🧾 新類型「{category}」的 Profile 草稿已送審 (fromLingLing)")
            return True
        except Exception as e:
            logging.warning(f"Profile draft generation failed: {e}")
            return False

    def ingest_to_wiki(
        self,
        raw_content: str,
        source_filepath: Path,
        llm_result: dict | None = None,
        part_info: dict | None = None,
        doc_config: dict | None = None,
    ):
        """Convert raw content into one wiki page.

        `part_info` flags this as a long-document part; when set, RAG indexing
        and wiki-index rebuild can be deferred to the driver so we don't
        rebuild the entire index N times for an N-part document.
        """
        template_used = None
        try:
            if not llm_result:
                context_hint = (part_info or {}).get("context_hint", "")
                index_content = (part_info or {}).get("index_content")
                if index_content is None:
                    index_content = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""

                # Resolve dynamic persona/template:
                if part_info:
                    persona = part_info.get("ingest_persona", "translator")
                    template = part_info.get("ingest_template", "translation-rpt")
                else:
                    persona = (doc_config or {}).get("synthesis_persona") or settings.AGENT_ROLE or "none"
                    template = (doc_config or {}).get("synthesis_template") or settings.USE_TEMPLATE or "wiki-note"
                template_used = template

                llm_result = self.llm.generate_entity_page(
                    raw_content,
                    source_filepath.name,
                    index_content,
                    context_hint=context_hint,
                    persona=persona,
                    forced_template=template,
                )
                if not llm_result:
                    raise ValueError("LLM generation failed.")

            base_title = source_filepath.stem.strip().replace("/", "-").replace("\\", "-")
            # Naming convention (NOT a bug — audit A1, deliberately kept): a
            # short doc's single page is the canonical "(Synthesis)" page for
            # that stem. Resolvers depend on this — load_sources
            # (builtin_adapters) looks up `{title} (Synthesis).md` with no bare
            # `{title}.md` fallback, ReadingIndex (vault_utils) links by this
            # name, and users may have `[[X (Synthesis)]]` wikilinks. Renaming
            # short docs to `{stem}` would break all three for cosmetic gain, so
            # the suffix stays. A given stem is either short (one Synthesis page,
            # no Parts) or long (Parts + a real Synthesis), never both.
            title = f"{base_title} (Part {part_info['current']})" if part_info else f"{base_title} (Synthesis)"

            tags = (part_info or {}).get("master_tags") or llm_result.get("tags", [])
            page_type = llm_result.get("type", "entity")

            body, quality_fixes = run_markdown_quality_checks(
                llm_result.get("content", ""),
                strip_frontmatter=True,
            )
            body += self._build_navigation(base_title, part_info)

            wiki_meta = self._build_part_metadata(title, page_type, tags, part_info, quality_fixes)
            wiki_meta.update(self._template_stamp(template_used))
            self._attach_trace_metadata(wiki_meta)
            wiki_markdown = dump_markdown_with_metadata(wiki_meta, body)

            page_folder = PAGES_DIR / base_title
            page_folder.mkdir(parents=True, exist_ok=True)
            page_path = page_folder / f"{title}.md"
            page_path.write_text(wiki_markdown, encoding="utf-8")
            self._record_artifact(page_path, page_type, title, wiki_meta)

            if not (part_info and part_info.get("defer_rag")):
                self.rag.add_document(
                    page_path, title, wiki_markdown, tags=tags,
                    section_path=(part_info or {}).get("section_path") or None,
                )

            # Long-doc parts pass `defer_index=True` so we only rebuild the
            # wiki index once at the end of the run, not per part.
            if not (part_info and part_info.get("defer_index")):
                update_wiki_index(page_path, title, sync_reading_index=True)

            llm_result["_page_path"] = str(page_path)
            llm_result["_title"] = title
            llm_result["_tags"] = tags
            return llm_result

        except Exception as e:
            logging.error(f"Ingestion failed for {source_filepath.name}: {e}")
            return None

    # ── Single-page helpers ──────────────────────────────────────────

    @staticmethod
    def _build_navigation(base_title: str, part_info: dict | None) -> str:
        lines = ["\n\n---\n## 🔗 知識導航"]
        if part_info:
            lines.append(f"*   🔙 **[[{base_title} (Synthesis)|查看全文總結 (Synthesis)]]**")
            lines.append(f"*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**")
            lines.append(f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**")

            adj_links = []
            current = part_info["current"]
            total = part_info["total"]
            if current > 1:
                adj_links.append(f"[[{base_title} (Part {current - 1})|◀ 上一篇]]")
            if current < total:
                adj_links.append(f"[[{base_title} (Part {current + 1})|下一篇 ▶]]")
            if adj_links:
                lines.append(f"*   📑 {' | '.join(adj_links)}")
        else:
            lines.append(f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _build_part_metadata(
        title: str,
        page_type: str,
        tags: list,
        part_info: dict | None,
        quality_fixes: list,
    ) -> dict:
        meta = {
            "title": title,
            "type": page_type,
            "date_created": datetime.now().strftime("%Y-%m-%d"),
            "tags": tags,
            "quality_checker": "deterministic-markdown-v1",
        }
        if part_info:
            meta["part"] = part_info["current"]
            meta["parts_count"] = part_info["total"]
            meta["digest_schema"] = "part-digest-v1"
            meta.update(part_info.get("source_span") or {})
            # ThoughtfulSplitter metadata: only present when USE_THOUGHTFUL_SPLITTER=true.
            section_path = part_info.get("section_path")
            if section_path:
                meta["section_path"] = list(section_path)
            boundary_type = part_info.get("boundary_type")
            if boundary_type:
                meta["boundary_type"] = boundary_type
        if quality_fixes:
            meta["quality_fixes"] = quality_fixes
        return meta

    # ── Long-document pipeline ──────────────────────────────────────

    def _ingest_long_document(self, content: str, source_filepath: Path, base_title: str, doc_config: dict | None = None):
        chunk_spans = self.splitter.split_text_with_spans(content)
        chunks = [s["text"] for s in chunk_spans]
        source_spans = [self._source_span_for_chunk(content, span, i + 1) for i, span in enumerate(chunk_spans)]
        logging.info(f"Long document detected ({len(content)} chars). Splitting into {len(chunks)} parts.")

        # Read the wiki index ONCE for the whole run; previously each part
        # re-read it from disk.
        index_content = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""

        # `chunk_spans` carries section_path/boundary_type only when the
        # ThoughtfulSplitter is in use; under the legacy splitter the extra
        # keys simply aren't present and `_process_parts` falls back to "".
        part_state = self._process_parts(
            chunks, source_spans, source_filepath, base_title, index_content,
            chunk_metas=chunk_spans, doc_config=doc_config,
        )

        ui.set_status(f"Stitching: {base_title}...")
        stitched_path = self._write_stitched_article(
            base_title,
            part_state["part_paths"],
            part_state["master_tags"],
            len(content),
            part_state["total_output_chars"],
        )
        if stitched_path:
            part_state["navigation_items"].append(
                f"- [[{base_title} (Stitched)]]: 忠實接合版，保留 Part notes 的主要內容"
            )

        ui.set_status(f"Synthesizing: {base_title}...")
        synthesis_file = self._write_synthesis(
            base_title=base_title,
            content=content,
            chunks=chunks,
            source_spans=source_spans,
            part_state=part_state,
            doc_config=doc_config,
        )

        # Single index rebuild at the very end of the long-doc run, covering
        # every part + stitched + synthesis we just wrote.
        update_wiki_index(synthesis_file, base_title, sync_reading_index=True)

    def _process_parts(
        self,
        chunks: list[str],
        source_spans: list[dict],
        source_filepath: Path,
        base_title: str,
        index_content: str,
        chunk_metas: list[dict] | None = None,
        doc_config: dict | None = None,
    ) -> dict:
        master_tags: list = []
        pending_concepts = ""
        part_digests: list = []
        part_paths: list[Path] = []
        navigation_items: list[str] = []
        total_output_chars = 0
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            # B1 resume: if this part's note is already complete (digest appendix
            # + persisted resume state), skip the LLM work and rebuild its state
            # from frontmatter, keeping the pending_concepts chain intact.
            part_path = PAGES_DIR / base_title / f"{base_title} (Part {i + 1}).md"
            resumed = self._resume_part(part_path)
            if resumed is not None:
                ui.set_status(f"Resuming: Part {i + 1}/{total} already distilled")
                if not master_tags and resumed["tags"]:
                    master_tags = resumed["tags"]
                pending_concepts = resumed["pending_concepts"]
                if resumed["part_digest"]:
                    part_digests.append(resumed["part_digest"])
                    nav = digest_value_to_text(resumed["part_digest"].get("thesis")) or ""
                    navigation_items.append(f"- [[{base_title} (Part {i + 1})]]: {nav[:140]}")
                part_paths.append(part_path)
                continue

            ui.set_status(f"Distilling Part {i + 1} of {total}...")

            context_hint = f"Part {i + 1}/{total}."
            if i > 0 and pending_concepts:
                context_hint += (
                    f" Previously you identified these pending concepts: {pending_concepts}. Please focus on them."
                )
            if i < total - 1:
                context_hint += " Since more parts follow, PLEASE include a 'pending_concepts' field in your YAML."

            chunk_meta = chunk_metas[i] if chunk_metas else {}
            part_info = {
                "current": i + 1,
                "total": total,
                "master_tags": master_tags,
                "context_hint": context_hint,
                "defer_rag": True,
                "defer_index": True,
                "source_span": source_spans[i],
                "index_content": index_content,
                # Optional metadata from ThoughtfulSplitter (empty under legacy splitter):
                "section_path": chunk_meta.get("section_path") or [],
                "boundary_type": chunk_meta.get("boundary_type") or "",
                # Configurable ingest persona and template:
                "ingest_persona": (doc_config or {}).get("ingest_persona", "translator"),
                "ingest_template": (doc_config or {}).get("ingest_template", "translation-rpt"),
            }
            result = self.ingest_to_wiki(chunk, source_filepath, part_info=part_info)
            if not result:
                continue

            if not master_tags and result.get("tags"):
                master_tags = result["tags"]
            pending_concepts = result.get("pending_concepts", "")

            part_content = result.get("content", "")
            total_output_chars += len(part_content)
            digest = self.llm.generate_part_digest(
                base_title, i + 1, total, chunk, part_content, pending_concepts,
            )
            part_digests.append(digest)
            self._append_part_digest_to_note(
                result, digest, section_path=part_info.get("section_path"),
                part_content=part_content, pending_concepts=pending_concepts,
            )
            self._index_digest_facets(
                result.get("_page_path"), result.get("_title"), digest,
                tags=master_tags,
            )

            nav_summary = digest_value_to_text(digest.get("thesis")) if isinstance(digest, dict) else ""
            if not nav_summary:
                nav_summary = part_content.strip().split("\n")[0][:100]
            navigation_items.append(f"- [[{base_title} (Part {i + 1})]]: {nav_summary[:140]}")

            if result.get("_page_path"):
                part_paths.append(Path(result["_page_path"]))

        return {
            "master_tags": master_tags,
            "pending_concepts": pending_concepts,
            "part_digests": part_digests,
            "part_paths": part_paths,
            "navigation_items": navigation_items,
            "total_output_chars": total_output_chars,
        }

    def _write_synthesis(
        self,
        *,
        base_title: str,
        content: str,
        chunks: list[str],
        source_spans: list[dict],
        part_state: dict,
        doc_config: dict | None = None,
    ) -> Path:
        from core.version import BUILD_DATE

        syn_persona = (doc_config or {}).get("synthesis_persona", "none")
        syn_template = (doc_config or {}).get("synthesis_template") or settings.USE_TEMPLATE or "wiki-note"

        outcome = self._synthesize_with_critique_retry(
            base_title, part_state, syn_template, syn_persona
        )
        synthesis_text = outcome["text"]
        synthesis_fixes = outcome["fixes"]
        critique_section = outcome["section"]
        critique_verdict = outcome["verdict"]

        digest_appendix = self.format_digest_appendix(part_state["part_digests"])
        master_tags = part_state["master_tags"]
        nav_block = "\n".join(part_state["navigation_items"])
        syn_nav = (
            f"\n\n---\n## 🔗 原始溯源\n"
            f"*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**\n"
            f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**"
        )
        tag_line = " ".join(f"#{t}" for t in master_tags if "part-" not in t)

        # Phase 6 auto-attach: a learning artifact for the summary (gated by
        # Scripture's `visual_router`; "" and zero LLM calls when off → byte-identical).
        from services.learning_artifacts import maybe_artifact_section
        artifact_section = maybe_artifact_section(self.llm, synthesis_text)

        final_content = (
            f"# ✨ {base_title} (Synthesis)\n"
            f"---\n\n"
            f"## 📝 Executive Summary\n{synthesis_text}\n\n"
            f"{artifact_section}"
            f"## 📂 Navigation\n{nav_block}{syn_nav}\n\n"
            f"{digest_appendix}\n\n"
            f"{critique_section}"
            f"## 🗺️ Knowledge Map\n(Tags: {tag_line})\n\n"
            f"## 📊 System Metadata\n"
            f"- **Original Content Size**: {len(content)} chars\n"
            f"- **Generated Content Size**: {part_state['total_output_chars']} chars\n"
            f"- **Total Parts**: {len(chunks)}\n"
            f"- **Model**: {self.llm.model}\n"
            f"- **Status**: #PerfectPitch\n"
        )
        final_content, final_fixes = run_markdown_quality_checks(final_content)

        final_meta = {
            "title": f"{base_title} (Synthesis)",
            "tags": (master_tags or []) + ["synthesis", "completed"],
            "status": "#PerfectPitch",
            "engine_build": BUILD_DATE,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": len(content),
            "output_chars": part_state["total_output_chars"],
            "parts_count": len(chunks),
            "part_source_map": source_spans,
            "synthesis_pipeline": "structured-digest-v1",
            "digest_schema": "part-digest-v1",
            "quality_checker": "deterministic-markdown-v1",
        }
        final_meta.update(self._template_stamp(syn_template))
        combined_fixes = self._dedupe_quality_fixes(
            (synthesis_fixes or []) + (final_fixes or [])
        )
        if combined_fixes:
            final_meta["quality_fixes"] = combined_fixes
        if critique_verdict:
            final_meta["quality_verdict"] = critique_verdict
        if SYNTHESIS_CRITIQUE_ENABLED:
            final_meta["critique_attempts"] = outcome["attempts"]
        if len(outcome["verdict_history"]) > 1:
            final_meta["quality_verdict_history"] = outcome["verdict_history"]
        self._attach_trace_metadata(final_meta)

        entity_dir = PAGES_DIR / base_title
        entity_dir.mkdir(parents=True, exist_ok=True)
        synthesis_file = entity_dir / f"{base_title} (Synthesis).md"
        synthesis_file.write_text(dump_markdown_with_metadata(final_meta, final_content), encoding="utf-8")
        self._record_artifact(
            synthesis_file,
            "synthesis",
            f"{base_title} (Synthesis)",
            final_meta,
            quality_verdict=critique_verdict,
        )

        self.rag.add_document(synthesis_file, base_title, final_content, tags=final_meta["tags"])
        return synthesis_file

    # ── Critique post-step ───────────────────────────────────────────

    _VERDICT_RANK = {"keep": 2, "revise": 1, "reject": 0, None: -1}

    def _synthesize_with_critique_retry(
        self,
        base_title: str,
        part_state: dict,
        syn_template,
        syn_persona,
    ) -> dict:
        """Generate the synthesis, then act on the critique verdict.

        An explicit revise/reject verdict triggers up to
        SYNTHESIS_CRITIQUE_MAX_RETRIES regenerations with the critique
        findings fed back. A retry is adopted only when its verdict ranks
        strictly higher (keep > revise > reject > unparseable); an
        unparseable first verdict never triggers a retry. Worst case adds
        one synthesis + one critique call per retry (local model).

        Returns {"text", "fixes", "section", "verdict", "attempts",
        "verdict_history"}.
        """

        def attempt(feedback: str | None) -> dict:
            # Pass critique_feedback only when set, so doubles of the LLM
            # client that predate the kwarg keep working on the normal path.
            extra = {"critique_feedback": feedback} if feedback is not None else {}
            text = self.llm.generate_synthesis(
                base_title,
                part_state["part_digests"],
                part_state["pending_concepts"],
                template=syn_template,
                persona=syn_persona,
                **extra,
            )
            text, fixes = run_markdown_quality_checks(text, strip_frontmatter=True)
            # Critique runs against the same digests the synthesis was
            # generated from — so any drift away from the sources surfaces.
            section, verdict = self._run_synthesis_critique(
                base_title, text, part_state["part_digests"]
            )
            return {"text": text, "fixes": fixes, "section": section, "verdict": verdict}

        current = attempt(None)
        attempts = 1
        history = [current["verdict"]]

        retries_left = SYNTHESIS_CRITIQUE_MAX_RETRIES
        while current["verdict"] in ("revise", "reject") and retries_left > 0:
            retries_left -= 1
            feedback = current["section"].removeprefix(_CRITIQUE_HEADER).strip()
            retry = attempt(feedback)
            attempts += 1
            history.append(retry["verdict"])
            if self._VERDICT_RANK[retry["verdict"]] > self._VERDICT_RANK[current["verdict"]]:
                current = retry
            else:
                logging.info(
                    f"Critique retry for {base_title} did not improve "
                    f"({history[-2]} → {history[-1]}); keeping the original synthesis."
                )

        current["attempts"] = attempts
        current["verdict_history"] = history
        return current

    def _run_synthesis_critique(
        self,
        base_title: str,
        synthesis_text: str,
        part_digests: list,
    ) -> tuple[str, str | None]:
        """Critique the synthesis against its part digests. Fail-soft.

        Returns (body_section, verdict). `body_section` is the empty string
        when critique is disabled or fails, so the caller can splice it in
        unconditionally. `verdict` is one of "keep" / "revise" / "reject"
        if parseable, else None.
        """
        if not SYNTHESIS_CRITIQUE_ENABLED:
            return "", None
        if not part_digests or not synthesis_text.strip():
            return "", None

        sources = "\n\n".join(
            self.llm.format_digest_for_prompt(d) for d in part_digests
        )
        try:
            critique = self.llm.critique_text(
                candidate=synthesis_text,
                sources=sources,
                focus="Source-grounding, specificity preservation, and contradiction surfacing.",
            )
        except Exception as e:
            logging.warning(f"Critique failed for {base_title}: {e}")
            return "", None

        if not critique or not critique.strip() or critique.startswith("Critique failed"):
            return "", None

        verdict = self._parse_verdict(critique)
        section = f"{_CRITIQUE_HEADER}\n\n{critique.strip()}\n\n"
        return section, verdict

    @staticmethod
    def _parse_verdict(critique: str) -> str | None:
        m = _VERDICT_RE.search(critique)
        if not m:
            return None
        verdict = _VERDICT_NORMALISE.get(m.group(1).strip().lower())
        if verdict == "revise" and _VERDICT_NEGATION_RE.search(critique[: m.start(1)]):
            return "keep"
        return verdict

    @staticmethod
    def _dedupe_quality_fixes(fixes: list) -> list:
        """Deduplicate structured/string quality fixes while preserving order."""
        seen = set()
        out = []
        for fix in fixes:
            key = (
                tuple(sorted(fix.items()))
                if isinstance(fix, dict)
                else ("scalar", str(fix))
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(fix)
        return out

    # ── Digest formatting ────────────────────────────────────────────

    def format_digest_appendix(self, part_digests: list) -> str:
        if not part_digests:
            return ""

        lines = [
            _PART_DIGEST_HEADER,
            "",
            "> 每個 Part 的結構化摘要。這是 Ling Ling 進行總合成前的中間理解，可用來檢查 final synthesis 是否有根據。",
            "",
        ]

        for index, digest in enumerate(part_digests, 1):
            lines.extend(self._format_one_digest(index, digest))

        return "\n".join(lines).strip()

    @staticmethod
    def _format_one_digest(index: int, digest) -> list[str]:
        if isinstance(digest, str):
            return [f"### Part {index}", "", digest.strip(), ""]
        if not isinstance(digest, dict):
            return [f"### Part {index}", "", str(digest or "(empty digest)"), ""]

        def clean_list(values):
            if not values:
                return []
            if isinstance(values, str):
                values = [values]
            return [t for v in values if (t := digest_value_to_text(v))]

        def bullet_block(label: str, values) -> list[str]:
            items = clean_list(values)
            if not items:
                return []
            block = [f"- **{label}**:"]
            block.extend(f"  - {item}" for item in items)
            return block

        part_number = digest.get("part", index)
        title = digest.get("title") or f"Part {part_number}"
        out = [f"### Part {part_number}: {title}", ""]

        thesis = digest_value_to_text(digest.get("thesis", ""))
        if thesis:
            out.append(f"- **Thesis**: {thesis}")

        out.extend(bullet_block("Key Points", digest.get("key_points", [])))
        out.extend(bullet_block("Evidence", digest.get("evidence", [])))
        out.extend(bullet_block("Terms", digest.get("terms", [])))
        out.extend(bullet_block("Open Questions", digest.get("open_questions", [])))

        handoff = digest_value_to_text(digest.get("handoff", ""))
        if handoff:
            out.append(f"- **Handoff**: {handoff}")

        out.append("")
        return out

    @staticmethod
    def _resume_part(part_path: Path) -> dict | None:
        """For B1 resume: if a part note is already complete — it has the digest
        appendix AND the persisted resume state (`part_digest` in frontmatter) —
        return its reconstructed state so `_process_parts` can skip it. Returns
        None when missing, or finalized by a pre-B1 build (no resume state) so it
        gets re-distilled rather than silently dropped."""
        if not part_path.exists():
            return None
        try:
            text = part_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if _PART_DIGEST_HEADER not in text or "part_digest:" not in text:
            return None
        meta = parse_markdown_metadata(text)
        if "part_digest" not in meta:
            return None
        return {
            "tags": meta.get("tags") or [],
            "pending_concepts": meta.get("pending_concepts", "") or "",
            "part_digest": meta.get("part_digest"),
        }

    def _append_part_digest_to_note(
        self, ingest_result: dict, digest, section_path: list | None = None,
        part_content: str | None = None, pending_concepts: str = "",
    ) -> None:
        page_path_value = ingest_result.get("_page_path") if isinstance(ingest_result, dict) else None
        if not page_path_value:
            return

        page_path = Path(page_path_value)
        if not page_path.exists():
            return

        appendix = self.format_digest_appendix([digest])
        # Per-part learning artifact (top-2 visuals), gated by visual_router;
        # "" and zero LLM calls when off → byte-identical default behaviour.
        artifact_section = ""
        if part_content:
            from services.learning_artifacts import maybe_artifact_section
            artifact_section = maybe_artifact_section(self.llm, part_content)

        content = page_path.read_text(encoding="utf-8").rstrip()
        # Strip any previously-appended auto sections (whichever comes first) so
        # re-ingesting the same part regenerates rather than duplicates them.
        cut = len(content)
        for marker in (_ARTIFACT_HEADER, _PART_DIGEST_HEADER):
            i = content.find(marker)
            if i != -1:
                cut = min(cut, i)
        content = content[:cut].rstrip()

        # Inline key-point highlighting (== ==): a deterministic, non-destructive
        # wrap of verbatim spans the digest call already chose. No extra LLM call.
        highlighted = 0
        if settings.HIGHLIGHT_ENABLED and isinstance(digest, dict):
            content, highlighted = self._apply_highlights(
                content, digest.get("highlights"), settings.HIGHLIGHT_MAX,
            )

        tail = f"{artifact_section}{appendix}".strip()
        body_full = f"{content}\n\n{tail}\n" if tail else f"{content}\n"

        # B1 resume state: persist the carry-forward pending_concepts and the
        # structured digest into the note's frontmatter, so an interrupted run
        # can resume from disk (skip-existing) without re-calling the LLM.
        meta = parse_markdown_metadata(body_full)
        body_only, _ = strip_body_frontmatter(body_full)
        meta["pending_concepts"] = pending_concepts or ""
        if isinstance(digest, dict):
            meta["part_digest"] = digest
        updated = dump_markdown_with_metadata(meta, body_only)
        page_path.write_text(updated, encoding="utf-8")

        title = ingest_result.get("_title") or page_path.stem
        tags = ingest_result.get("_tags") or ingest_result.get("tags", [])
        self.rag.add_document(page_path, title, updated, tags=tags, section_path=section_path or None)

    @staticmethod
    def _apply_highlights(text: str, highlights, max_spans: int = 5) -> tuple[str, int]:
        """Wrap up to ``max_spans`` verbatim spans in Obsidian ``== ==`` markers.

        Deterministic and non-destructive by design:
        - a span not found verbatim in the body is skipped, never paraphrased in
          (so LLM drift drops the highlight rather than corrupting the note);
        - the YAML frontmatter is never touched;
        - a span already wrapped is left alone, so re-ingesting a part is
          idempotent rather than producing ``====…====``.

        Returns the (possibly) updated text and the number of spans applied.
        """
        if not highlights or max_spans <= 0:
            return text, 0

        # Confine marking to the body — never inside the YAML frontmatter.
        body_start = 0
        if text.startswith("---"):
            fence = text.find("\n---", 3)
            if fence != -1:
                nl = text.find("\n", fence + 1)
                body_start = nl + 1 if nl != -1 else len(text)
        head, body = text[:body_start], text[body_start:]

        applied = 0
        for raw in highlights:
            if applied >= max_spans:
                break
            phrase = digest_value_to_text(raw).strip()
            if len(phrase) < 4 or "==" in phrase:
                continue
            idx = body.find(phrase)
            if idx == -1:
                continue
            # Skip if the span already abuts a marker (idempotent re-ingest).
            if body[max(0, idx - 2):idx] == "==":
                continue
            body = f"{body[:idx]}=={phrase}=={body[idx + len(phrase):]}"
            applied += 1

        return head + body, applied

    # ── Stitched article ────────────────────────────────────────────

    def _write_stitched_article(
        self,
        base_title: str,
        part_paths: list[Path],
        tags: list[str],
        input_chars: int,
        output_chars: int,
    ) -> Path | None:
        readable_parts = [p for p in part_paths if p and p.exists()]
        if not readable_parts:
            return None

        sections: list[str] = []
        for index, part_path in enumerate(readable_parts, 1):
            # Read the file once and reuse it for both metadata + body extraction.
            content = part_path.read_text(encoding="utf-8")
            part_meta = parse_markdown_metadata(content)
            body = self._extract_stitchable_body(content)
            if not body:
                continue

            source_range = self._format_source_range(part_meta)
            sections.append(
                f"## Part {index}\n\n"
                f"Source note: [[{part_path.stem}]]\n\n"
                f"{source_range}"
                f"<!-- source: {part_path.name} -->\n\n"
                f"{body}"
            )

        if not sections:
            return None

        from core.version import BUILD_DATE
        metadata = {
            "title": f"{base_title} (Stitched)",
            "type": "stitched_article",
            "tags": sorted(set((tags or []) + ["stitched", "longform"])),
            "status": "#FaithfulStitch",
            "engine_build": BUILD_DATE,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "parts_count": len(readable_parts),
            "stitch_pipeline": "part-note-stitch-v1",
            "quality_checker": "deterministic-markdown-v1",
        }

        body = (
            f"# {base_title} (Stitched)\n\n"
            "> 忠實接合版：保留各 Part note 的主要內容，移除每篇的 navigation、metadata 與 digest appendix。"
            "這份文件偏向完整閱讀，不等同於洞察型 Synthesis。\n\n"
            "## 🔗 Navigation\n"
            f"- [[{base_title} (Synthesis)|查看洞察總結 (Synthesis)]]\n"
            f"- [[{base_title}|查看完整原始檔 (Original)]]\n\n"
            "---\n\n"
            + "\n".join(f"{section}\n" for section in sections)
        )
        body, quality_fixes = run_markdown_quality_checks(body)
        if quality_fixes:
            metadata["quality_fixes"] = quality_fixes
        self._attach_trace_metadata(metadata)

        stitched_file = PAGES_DIR / base_title / f"{base_title} (Stitched).md"
        stitched_file.parent.mkdir(parents=True, exist_ok=True)
        stitched_markdown = dump_markdown_with_metadata(metadata, body)
        stitched_file.write_text(stitched_markdown, encoding="utf-8")
        self._record_artifact(stitched_file, "stitched_article", metadata["title"], metadata)

        self.rag.add_document(stitched_file, f"{base_title} (Stitched)", stitched_markdown, tags=metadata["tags"])
        return stitched_file

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _source_span_for_chunk(source_text: str, chunk_span: dict, part_number: int) -> dict:
        start = int(chunk_span.get("start", 0))
        end = int(chunk_span.get("end", start))
        return {
            "part": part_number,
            "source_start_char": start,
            "source_end_char": end,
            "source_start_line": source_text.count("\n", 0, start) + 1,
            "source_end_line": source_text.count("\n", 0, end) + 1,
        }

    @staticmethod
    def _format_source_range(part_meta: dict) -> str:
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

    def _extract_stitchable_body(self, content_or_path) -> str:
        """Strip frontmatter, navigation, and digest appendix from a part note."""
        if isinstance(content_or_path, Path):
            content = content_or_path.read_text(encoding="utf-8")
        else:
            content = content_or_path

        content = _FRONTMATTER_RE.sub("", content, count=1).strip()

        cut_markers = ("\n## 🔗 知識導航", "\n" + _PART_DIGEST_HEADER)
        positions = [pos for marker in cut_markers if (pos := content.find(marker)) != -1]
        if positions:
            content = content[: min(positions)].rstrip()

        content = self._demote_headings(content, levels=2)
        content, _ = run_markdown_quality_checks(content)
        return content.strip()

    def _attach_trace_metadata(self, metadata: dict) -> None:
        if hasattr(self.llm, "current_trace_ids"):
            trace_ids = self.llm.current_trace_ids()
            if not (isinstance(trace_ids, list) and all(isinstance(t, str) for t in trace_ids)):
                trace_ids = []
            if trace_ids:
                metadata.setdefault("trace_ids", trace_ids)
        if hasattr(self.llm, "current_run_id"):
            run_id = self.llm.current_run_id()
            if not isinstance(run_id, str):
                run_id = None
            if run_id:
                metadata.setdefault("run_id", run_id)

    def _record_artifact(
        self,
        path: Path,
        artifact_type: str,
        title: str,
        metadata: dict,
        quality_verdict: str | None = None,
    ) -> None:
        if not hasattr(self.llm, "trace_store"):
            return
        trace_ids = metadata.get("trace_ids") or []
        try:
            self.llm.trace_store.record_artifact(
                path=path,
                artifact_type=artifact_type,
                title=title,
                trace_id=trace_ids[-1] if trace_ids else None,
                metadata=metadata,
                quality_verdict=quality_verdict or metadata.get("quality_verdict"),
                quality_score=metadata.get("quality_score"),
            )
        except Exception as e:
            logging.debug(f"Artifact trace write failed: {e}")

    @staticmethod
    def _demote_headings(markdown: str, levels: int = 1) -> str:
        def replace(match: re.Match) -> str:
            hashes = match.group(1)
            text = match.group(2)
            return f"{'#' * min(len(hashes) + levels, 6)} {text}"

        return _HEADING_RE.sub(replace, markdown)
