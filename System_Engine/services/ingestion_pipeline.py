"""Ingestion pipeline — turns raw markdown into wiki pages.

Two flows:
  - **Single-page**: short doc → one LLM call → one wiki note.
  - **Long-document**: chunk → per-part LLM → stitched + synthesis.

Originally extracted from ClippingWatcher.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core.config import (
    FACET_MAX_PER_DOC,
    INGEST_ARTIFACT_BACKUP_DIR,
    INGEST_ARTIFACT_MAX_LAG_PARTS,
    INGEST_ARTIFACT_MAX_ATTEMPTS,
    INGEST_ARTIFACT_PENDING_DIR,
    INGEST_ARTIFACT_QUARANTINE_HOURS,
    INGEST_ARTIFACT_WORKERS,
    INGEST_FAILURE_STATE_FILE,
    INDEX_FILE,
    PAGES_DIR,
    PROFILES_DIR,
    PROFILES_PENDING_DIR,
    SYNTHESIS_CRITIQUE_ENABLED,
    SYNTHESIS_CRITIQUE_MAX_RETRIES,
    THOUGHTFUL_EMIT_SUMMARY,
    TEMPLATES_DIR,
    settings,
    SCRIPTURE_DIR,
)
from core.parser import (
    check_translation_number_fidelity,
    demote_body_h1,
    dump_markdown_with_metadata,
    parse_markdown_metadata,
    run_markdown_quality_checks,
    strip_body_frontmatter,
)
from core.markdown_doc import MarkdownDocument
from core.ui import ui
from core.utils import digest_value_to_text
from core.vault_utils import sanitize_filename, update_wiki_index
from services.ingest.critique_loop import SynthesisCritiqueLoop, parse_verdict
from services.ingest.artifact_pipeline import (
    ArtifactJobDispatcher,
    apply_artifact_section,
    artifact_section_from_page,
    artifact_slot_status,
    content_hash as artifact_content_hash,
    core_content_for_artifact,
    begin_artifact_attempt,
    defer_artifact_attempt,
    prepare_artifact_slot,
)
from services.ingest.digest_format import PART_DIGEST_HEADER as _PART_DIGEST_HEADER
from services.ingest.digest_format import format_digest_appendix as _format_digest_appendix
from services.ingest.digest_format import format_one_digest as _format_one_digest_fn
from services.ingest.part_state import PartState
from services.ingest.atomic_io import atomic_write_text
from services.ingest.entity_quality import assess_entity_body
from services.ingest.failure_ledger import IngestFailureLedger
from services.ingest.profile_routing import ProfileRouter
from services.ingest.result import DocumentIngestResult, IngestResult
from services.profile_manager import ProfileManager
from services.text_splitter import TextSplitter


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
# Auto-attached learning-artifact section (visual_router). Prefix shared by all
# emitted sections so re-runs can strip and regenerate them idempotently.
_ARTIFACT_HEADER = "## 🖼️ 學習輔助"


class IngestionPipeline:
    """Orchestrates raw content → wiki pages."""

    def __init__(self, llm_client, rag_manager):
        self.llm = llm_client
        self.rag = rag_manager
        # LLM calls may overlap across the core and enrichment pipelines, but
        # vault mutations remain short, serialized commit sections.
        self._commit_lock = threading.Lock()
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

    @property
    def _commit_guard(self) -> threading.Lock:
        """Lazy compatibility for tests/legacy callers constructed via __new__."""
        lock = getattr(self, "_commit_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._commit_lock = lock
        return lock

    # ── Public entry points ──────────────────────────────────────────

    def ingest_markdown(self, content: str, source_filepath: Path):
        # Source pre-passes (in-memory, never edits the source file):
        #   0c strip_boilerplate — drop Gutenberg license/TOC.
        #   0d flatten_linenumber_tables — OCR page-line-number tables → prose.
        #   0b normalize_structure — promote plain-text chapter cues to markdown
        #      headings, but only for docs that lack markdown structure.
        from services.source_prep import (
            flatten_linenumber_tables,
            normalize_structure,
            strip_boilerplate,
        )

        content, _stripped = strip_boilerplate(content)
        content, _flattened = flatten_linenumber_tables(content)
        content, _normed = normalize_structure(content)
        if _stripped or _flattened or _normed:
            logging.info(
                f"Source prep on {source_filepath.name}: strip={_stripped} "
                f"flatten={_flattened} normalize={_normed}"
            )
        meta = parse_markdown_metadata(content)
        doc_config = self._resolve_routing(meta, content, source_filepath)

        if len(content) > self.splitter.chunk_size + 1000:
            return self._ingest_long_document(
                content, source_filepath, source_filepath.stem, doc_config=doc_config
            )
        else:
            result = self.ingest_to_wiki(content, source_filepath, doc_config=doc_config)
            if not result:
                logging.warning(
                    f"Short-doc ingest failed for {source_filepath.name}: "
                    f"stage={result.stage} kind={result.error_kind} — {result.detail}"
                )
            return result

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
        # A degraded digest (fallback path) is scraped prose, not curated
        # facets — indexing it poisons retrieval with scaffolding lines.
        if digest.get("degraded"):
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

    # ── Profile routing ──────────────────────────────────────────────

    def _resolve_routing(self, meta: dict, content: str, source_filepath: Path) -> dict:
        """Resolve synthesis persona/template (see services.ingest.profile_routing)."""
        return ProfileRouter(self.llm).resolve(self.load_profiles(), meta, content, source_filepath)

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

    def ingest_to_wiki(
        self,
        raw_content: str,
        source_filepath: Path,
        llm_result: dict | None = None,
        part_info: dict | None = None,
        doc_config: dict | None = None,
    ) -> IngestResult:
        """Convert raw content into one wiki page. Returns an IngestResult —
        falsy on failure, with `stage`/`error_kind` saying what broke where
        (the old contract was a bare None for every failure mode).

        `part_info` flags this as a long-document part; when set, RAG indexing
        and wiki-index rebuild can be deferred to the driver so we don't
        rebuild the entire index N times for an N-part document.
        """
        template_used = None
        stage = "llm"
        try:
            if not llm_result:
                context_hint = (part_info or {}).get("context_hint", "")
                index_content = (part_info or {}).get("index_content")
                if index_content is None:
                    index_content = (
                        INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""
                    )

                # Resolve dynamic persona/template:
                if part_info:
                    persona = part_info.get("ingest_persona", "translator")
                    template = part_info.get("ingest_template", "translation-rpt")
                else:
                    persona = (
                        (doc_config or {}).get("synthesis_persona") or settings.AGENT_ROLE or "none"
                    )
                    template = (
                        (doc_config or {}).get("synthesis_template")
                        or settings.USE_TEMPLATE
                        or "wiki-note"
                    )
                template_used = template

                llm_result = self.llm.generate_entity_page(
                    raw_content,
                    source_filepath.name,
                    index_content,
                    context_hint=context_hint,
                    persona=persona,
                    forced_template=template,
                    # Long-document retries are charged and persisted by the
                    # per-Part failure ledger below. Keeping this call to one
                    # content attempt prevents an invisible 2x multiplier.
                    content_attempts=1 if part_info else 2,
                )
                if not llm_result:
                    raw_failure = getattr(self.llm, "last_entity_failure", None)
                    failure = raw_failure if isinstance(raw_failure, dict) else {}
                    result = IngestResult.failure("llm", ValueError("LLM generation failed."))
                    result.error_kind = str(failure.get("kind") or result.error_kind)
                    result.issues = list(failure.get("issues") or [])
                    result.transient = bool(failure.get("transient"))
                    return result

            parse_status = llm_result.get("_parse_status")
            if parse_status == "invalid":
                result = IngestResult.failure(
                    "parse", ValueError("Entity response did not satisfy the YAML contract.")
                )
                result.issues = list(llm_result.get("_parse_issues") or [])
                return result
            entity_quality = assess_entity_body(llm_result.get("content", ""))
            if entity_quality.hard_issues or entity_quality.suspect_issues:
                result = IngestResult.failure(
                    "entity_quality", ValueError("Generated entity body failed publication checks.")
                )
                result.issues = [
                    *entity_quality.hard_issues,
                    *entity_quality.suspect_issues,
                ]
                return result

            # Sanitize math + path separators out of the stem: it becomes the
            # page folder, the title, every Part/Synthesis filename and the RAG
            # metadata title, so a LaTeX-laden source name (數學分析原理：$\mathcal{L}^2$…)
            # must not leak `$ \ /` downstream and split the vault into phantom dirs.
            base_title = sanitize_filename(source_filepath.stem)
            # Naming convention (NOT a bug — audit A1, deliberately kept): a
            # short doc's single page is the canonical "(Synthesis)" page for
            # that stem. Resolvers depend on this — load_sources
            # (builtin_adapters) looks up `{title} (Synthesis).md` with no bare
            # `{title}.md` fallback, ReadingIndex (vault_utils) links by this
            # name, and users may have `[[X (Synthesis)]]` wikilinks. Renaming
            # short docs to `{stem}` would break all three for cosmetic gain, so
            # the suffix stays. A given stem is either short (one Synthesis page,
            # no Parts) or long (Parts + a real Synthesis), never both.
            title = (
                f"{base_title} (Part {part_info['current']})"
                if part_info
                else f"{base_title} (Synthesis)"
            )

            tags = (part_info or {}).get("master_tags") or llm_result.get("tags", [])
            page_type = llm_result.get("type", "entity")

            stage = "quality"
            body, quality_fixes = run_markdown_quality_checks(
                llm_result.get("content", ""),
                strip_frontmatter=True,
            )
            # DocQuality P4: warning_* entries are observations, not applied
            # fixes — route them to quality_warnings. Translations addition-
            # ally get the number-fidelity diff against the whole source
            # document (catches 1180-days-for-180 style digit corruption).
            quality_fixes, quality_warnings = self._split_quality_warnings(quality_fixes)
            if page_type == "translation":
                fidelity_source = (part_info or {}).get("fidelity_source") or raw_content
                quality_warnings.extend(check_translation_number_fidelity(body, fidelity_source))
            body += self._build_navigation(base_title, part_info)

            wiki_meta = self._build_part_metadata(title, page_type, tags, part_info, quality_fixes)
            if quality_warnings:
                wiki_meta["quality_warnings"] = quality_warnings
                wiki_meta["status"] = "#NeedsReview"
            wiki_meta.update(self._template_stamp(template_used))
            self._attach_trace_metadata(wiki_meta)
            wiki_markdown = dump_markdown_with_metadata(self._frontmatter_meta(wiki_meta), body)

            stage = "write"
            page_folder = PAGES_DIR / base_title
            page_folder.mkdir(parents=True, exist_ok=True)
            page_path = page_folder / f"{title}.md"
            if not (part_info and part_info.get("defer_commit")):
                atomic_write_text(page_path, wiki_markdown)
                self._record_artifact(page_path, page_type, title, wiki_meta)

            stage = "rag_index"
            if not (part_info and (part_info.get("defer_rag") or part_info.get("defer_commit"))):
                self.rag.add_document(
                    page_path,
                    title,
                    wiki_markdown,
                    tags=tags,
                    section_path=(part_info or {}).get("section_path") or None,
                )

            # Long-doc parts pass `defer_index=True` so we only rebuild the
            # wiki index once at the end of the run, not per part.
            stage = "wiki_index"
            if not (part_info and (part_info.get("defer_index") or part_info.get("defer_commit"))):
                update_wiki_index(page_path, title, sync_reading_index=True)

            return IngestResult(
                ok=True,
                page_path=page_path,
                title=title,
                tags=tags,
                content=llm_result.get("content", ""),
                pending_concepts=llm_result.get("pending_concepts", "") or "",
                part_digest=(
                    llm_result.get("part_digest")
                    if isinstance(llm_result.get("part_digest"), dict)
                    else None
                ),
                page_type=page_type,
                rendered_markdown=wiki_markdown,
                wiki_meta=wiki_meta,
            )

        except Exception as e:
            logging.error(f"Ingestion failed for {source_filepath.name} at stage '{stage}': {e}")
            return IngestResult.failure(stage, e)

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

    def _ingest_long_document(
        self, content: str, source_filepath: Path, base_title: str, doc_config: dict | None = None
    ) -> DocumentIngestResult:
        run_started = time.perf_counter()
        stage_ms: dict[str, int] = {}
        split_started = time.perf_counter()
        chunk_spans = self.splitter.split_text_with_spans(content)
        stage_ms["split"] = self._elapsed_ms(split_started)
        chunks = [s["text"] for s in chunk_spans]
        source_spans = [
            self._source_span_for_chunk(content, span, i + 1) for i, span in enumerate(chunk_spans)
        ]
        logging.info(
            f"Long document detected ({len(content)} chars). Splitting into {len(chunks)} parts."
        )

        # Read the wiki index ONCE for the whole run; previously each part
        # re-read it from disk.
        index_content = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""

        # `chunk_spans` carries section_path/boundary_type only when the
        # ThoughtfulSplitter is in use; under the legacy splitter the extra
        # keys simply aren't present and `_process_parts` falls back to "".
        distill_started = time.perf_counter()
        part_state = self._process_parts(
            chunks,
            source_spans,
            source_filepath,
            base_title,
            index_content,
            chunk_metas=chunk_spans,
            doc_config=doc_config,
        )
        stage_ms["distill_parts"] = self._elapsed_ms(distill_started)

        if part_state.failed_parts or len(part_state.completed_parts) != len(chunks):
            result = DocumentIngestResult(
                ok=False,
                status="partial",
                stage="distill_parts",
                expected_parts=len(chunks),
                completed_parts=part_state.completed_parts,
                failed_parts=part_state.failed_parts,
                archivable=False,
                detail="One or more parts did not reach the digest commit point.",
                metrics=self._ingest_metrics(stage_ms, part_state, run_started),
            )
            self._record_ingestion_run(source_filepath, base_title, result)
            return result

        ui.set_status(f"Stitching: {base_title}...")
        stitch_started = time.perf_counter()
        try:
            stitched_path = self._write_stitched_article(
                base_title,
                part_state.part_paths,
                part_state.master_tags,
                len(content),
                part_state.total_output_chars,
            )
        except Exception as e:
            stage_ms["stitch"] = self._elapsed_ms(stitch_started)
            result = DocumentIngestResult(
                ok=False,
                status="failed",
                stage="stitch",
                expected_parts=len(chunks),
                completed_parts=part_state.completed_parts,
                failed_parts=part_state.failed_parts,
                archivable=False,
                detail=str(e),
                metrics=self._ingest_metrics(stage_ms, part_state, run_started),
            )
            self._record_ingestion_run(source_filepath, base_title, result)
            return result
        stage_ms["stitch"] = self._elapsed_ms(stitch_started)
        if not stitched_path:
            result = DocumentIngestResult(
                ok=False,
                status="failed",
                stage="stitch",
                expected_parts=len(chunks),
                completed_parts=part_state.completed_parts,
                archivable=False,
                detail="Stitched article was not written.",
                metrics=self._ingest_metrics(stage_ms, part_state, run_started),
            )
            self._record_ingestion_run(source_filepath, base_title, result)
            return result
        if stitched_path:
            part_state.navigation_items.append(
                f"- [[{base_title} (Stitched)]]: 忠實接合版，保留 Part notes 的主要內容"
            )

        ui.set_status(f"Synthesizing: {base_title}...")
        synthesis_started = time.perf_counter()
        try:
            synthesis_file = self._write_synthesis(
                base_title=base_title,
                content=content,
                chunks=chunks,
                source_spans=source_spans,
                part_state=part_state,
                doc_config=doc_config,
            )
        except Exception as e:
            stage_ms["synthesize"] = self._elapsed_ms(synthesis_started)
            result = DocumentIngestResult(
                ok=False,
                status="failed",
                stage="synthesize",
                expected_parts=len(chunks),
                completed_parts=part_state.completed_parts,
                archivable=False,
                detail=str(e),
                metrics=self._ingest_metrics(stage_ms, part_state, run_started),
            )
            self._record_ingestion_run(source_filepath, base_title, result)
            return result
        stage_ms["synthesize"] = self._elapsed_ms(synthesis_started)

        # Single index rebuild at the very end of the long-doc run, covering
        # every part + stitched + synthesis we just wrote.
        index_started = time.perf_counter()
        try:
            update_wiki_index(synthesis_file, base_title, sync_reading_index=True)
        except Exception as e:
            stage_ms["wiki_index"] = self._elapsed_ms(index_started)
            result = DocumentIngestResult(
                ok=False,
                status="failed",
                stage="wiki_index",
                expected_parts=len(chunks),
                completed_parts=part_state.completed_parts,
                failed_parts=part_state.failed_parts,
                synthesis_path=synthesis_file,
                archivable=False,
                detail=str(e),
                metrics=self._ingest_metrics(stage_ms, part_state, run_started),
            )
            self._record_ingestion_run(source_filepath, base_title, result)
            return result
        stage_ms["wiki_index"] = self._elapsed_ms(index_started)

        result = DocumentIngestResult(
            ok=True,
            status="complete",
            stage="done",
            expected_parts=len(chunks),
            completed_parts=part_state.completed_parts,
            synthesis_path=synthesis_file,
            archivable=True,
            metrics=self._ingest_metrics(stage_ms, part_state, run_started),
        )
        self._record_ingestion_run(source_filepath, base_title, result)
        return result

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))

    def _ingest_metrics(
        self, stage_ms: dict[str, int], part_state: PartState, run_started: float
    ) -> dict:
        return {
            "total_ms": self._elapsed_ms(run_started),
            "stage_ms": dict(stage_ms),
            "completed_parts": len(part_state.completed_parts),
            "resumed_parts": list(part_state.resumed_parts),
            "resumed_part_count": len(part_state.resumed_parts),
            "failed_parts": len(part_state.failed_parts),
            "degraded_parts": len(part_state.degraded_parts),
            "part_metrics": list(part_state.part_metrics),
            "artifact_metrics": list(part_state.artifact_metrics),
        }

    def _record_ingestion_run(
        self, source_path: Path, title: str, result: DocumentIngestResult
    ) -> None:
        metadata = {
            "status": result.status,
            "stage": result.stage,
            "archivable": result.archivable,
            "expected_parts": result.expected_parts,
            "completed_parts": list(result.completed_parts),
            "failed_parts": list(result.failed_parts),
            "metrics": result.metrics,
        }
        self._attach_trace_metadata(metadata)
        self._record_artifact(source_path, "ingestion_run", title, metadata)

    def _report_part_timing(
        self,
        source_path: Path,
        base_title: str,
        total_parts: int,
        metric: dict,
        *,
        status: str,
        stage: str,
    ) -> None:
        """Display and persist one Part's timing immediately.

        The document-level ingestion_run remains the aggregate. This separate
        artifact survives an interrupted long run and makes per-Part latency
        queryable without parsing terminal output.
        """
        metadata = {**metric, "status": status, "stage": stage, "total_parts": total_parts}
        self._attach_trace_metadata(metadata)
        self._record_artifact(
            source_path,
            "ingestion_part_timing",
            f"{base_title} (Part {metric['part']})",
            metadata,
        )

        total_seconds = metric.get("total_ms", 0) / 1000
        digest_mode = "inline" if metric.get("inline_digest") else "fallback"
        detail = (
            f"entity={metric.get('entity_ms', 0) / 1000:.1f}s, "
            f"digest={metric.get('digest_ms', 0) / 1000:.1f}s ({digest_mode}), "
            f"commit={metric.get('commit_ms', 0) / 1000:.1f}s"
        )
        message = f"Part {metric['part']}/{total_parts} {status} in {total_seconds:.1f}s [{detail}]"
        if status == "complete":
            ui.info(message)
        else:
            ui.warning(f"{message}; stage={stage}")

    def _process_parts(
        self,
        chunks: list[str],
        source_spans: list[dict],
        source_filepath: Path,
        base_title: str,
        index_content: str,
        chunk_metas: list[dict] | None = None,
        doc_config: dict | None = None,
    ) -> PartState:
        state = PartState()
        failure_ledger = IngestFailureLedger(path=INGEST_FAILURE_STATE_FILE)
        artifact_dispatcher = ArtifactJobDispatcher(INGEST_ARTIFACT_WORKERS)
        total = len(chunks)
        full_source = "\n".join(chunks)

        for i, chunk in enumerate(chunks):
            part_number = i + 1

            def report_artifact_wait(info: dict, next_part: int = part_number) -> None:
                ui.set_status(
                    f"Waiting for {info.get('label', 'learning aids')} "
                    f"({info.get('elapsed_seconds', 0):.0f}s); "
                    f"Part {next_part}/{total} is next..."
                )

            # Core work has higher priority, but not absolute priority: keep a
            # bounded lead so artifact jobs are admitted and make progress
            # throughout the document instead of draining only at the end.
            artifact_dispatcher.enforce_max_inflight(
                INGEST_ARTIFACT_MAX_LAG_PARTS,
                on_wait=report_artifact_wait,
            )
            part_started = time.perf_counter()
            # B1 resume: if this part's note is already complete (digest appendix
            # + persisted resume state), skip the LLM work and rebuild its state
            # from frontmatter, keeping the pending_concepts chain intact.
            part_path = PAGES_DIR / base_title / f"{base_title} (Part {part_number}).md"
            resumed = self._resume_part(part_path, chunk)
            if resumed is not None:
                if not state.master_tags and resumed["tags"]:
                    state.master_tags = resumed["tags"]
                state.pending_concepts = resumed["pending_concepts"]
                if resumed["part_digest"]:
                    state.part_digests.append(resumed["part_digest"])
                    nav = digest_value_to_text(resumed["part_digest"].get("thesis")) or ""
                    state.navigation_items.append(
                        f"- [[{base_title} (Part {part_number})]]: {nav[:140]}"
                    )
                state.part_paths.append(part_path)
                state.completed_parts.append(part_number)
                state.resumed_parts.append(part_number)
                try:
                    self._prepare_and_queue_resumed_artifact(
                        artifact_dispatcher,
                        part_path,
                        chunk,
                        base_title,
                        part_number,
                    )
                except Exception as exc:
                    logging.warning(
                        "Part %s artifact resume preparation failed: %s",
                        part_number,
                        exc,
                    )
                    state.artifact_metrics.append(
                        {
                            "part": part_number,
                            "status": "failed",
                            "stage": "resume_prepare",
                            "detail": str(exc),
                        }
                    )
                continue

            model = str(getattr(self.llm, "model", "unknown"))
            try:
                allowed, failure_key, _content_hash = failure_ledger.begin(
                    source_filepath, part_number, chunk, model
                )
            except OSError as exc:
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": "failure_ledger",
                        "error_kind": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
                continue
            if not allowed:
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": "quarantine",
                        "error_kind": "entity_quarantined",
                        "detail": "Deterministic entity failures exhausted the retry budget.",
                    }
                )
                continue

            ui.set_status(f"Distilling Part {part_number} of {total}...")

            context_hint = f"Part {part_number}/{total}."
            if i > 0 and state.pending_concepts:
                context_hint += f" Previously you identified these pending concepts: {state.pending_concepts}. Please focus on them."
            if i < total - 1:
                context_hint += " Since more parts follow, PLEASE include a 'pending_concepts' field in your YAML."
            context_hint += (
                " Include a 'part_digest' mapping in the same YAML header with: "
                "part, title, thesis, key_points, evidence, terms, open_questions, "
                "handoff, and highlights. thesis must be a non-empty string and "
                "key_points a non-empty list. This digest describes only this part. "
                "For LaTeX or any backslash inside YAML, prefer single-quoted scalars; "
                "inside double quotes, escape every backslash as \\\\."
            )

            chunk_meta = chunk_metas[i] if chunk_metas else {}
            part_info = {
                "current": part_number,
                "total": total,
                "master_tags": state.master_tags,
                "context_hint": context_hint,
                "defer_rag": True,
                "defer_index": True,
                "defer_commit": True,
                "source_span": source_spans[i],
                "index_content": index_content,
                # Number fidelity compares against the WHOLE document, not
                # just this chunk: translators legitimately carry context
                # across chunk boundaries (「承接前文」 recaps, 「第 18 編」
                # from the running statute) — only numbers absent from the
                # entire source are corruption.
                "fidelity_source": full_source,
                # Optional metadata from ThoughtfulSplitter (empty under legacy splitter):
                "section_path": chunk_meta.get("section_path") or [],
                "boundary_type": chunk_meta.get("boundary_type") or "",
                # Configurable ingest persona and template:
                "ingest_persona": (doc_config or {}).get("ingest_persona", "translator"),
                "ingest_template": (doc_config or {}).get("ingest_template", "translation-rpt"),
            }
            entity_started = time.perf_counter()
            result = self.ingest_to_wiki(chunk, source_filepath, part_info=part_info)
            if self._should_reroll_entity(result):
                failure_ledger.fail(
                    failure_key,
                    stage=result.stage,
                    detail=", ".join(result.issues) or (result.detail or "entity failure"),
                )
                try:
                    retry_allowed, failure_key, _content_hash = failure_ledger.begin(
                        source_filepath, part_number, chunk, model
                    )
                except OSError as exc:
                    retry_allowed = False
                    logging.warning(
                        "Part %s/%s could not persist its reroll budget: %s",
                        part_number,
                        total,
                        exc,
                    )
                if retry_allowed:
                    issues = ", ".join(result.issues) or result.detail or result.error_kind
                    ui.warning(
                        f"Part {part_number}/{total} draft rejected ({issues}); "
                        "rerolling once before moving on."
                    )
                    retry_info = dict(part_info)
                    retry_info["context_hint"] = (
                        f"{context_hint} The previous draft was rejected by the publication "
                        f"gate for: {issues}. Produce a fresh corrected document; close every "
                        "Markdown fence and do not leak YAML or reasoning into the body."
                    )
                    result = self.ingest_to_wiki(chunk, source_filepath, part_info=retry_info)
            entity_ms = self._elapsed_ms(entity_started)
            if not result:
                # The typed result finally says WHICH part died and why — a
                # silently dropped Part used to be indistinguishable from a skip.
                logging.warning(
                    f"Part {part_number}/{total} of '{base_title}' failed at stage "
                    f"{result.stage!r} ({result.error_kind}); skipping. {result.detail}"
                )
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": result.stage,
                        "error_kind": result.error_kind,
                        "detail": result.detail,
                    }
                )
                if result.transient:
                    failure_ledger.outage(failure_key)
                else:
                    failure_ledger.fail(
                        failure_key,
                        stage=result.stage,
                        detail=", ".join(result.issues) or (result.detail or "entity failure"),
                    )
                metric = {
                    "part": part_number,
                    "resumed": False,
                    "entity_ms": entity_ms,
                    "digest_ms": 0,
                    "commit_ms": 0,
                    "total_ms": self._elapsed_ms(part_started),
                }
                state.part_metrics.append(metric)
                self._report_part_timing(
                    source_filepath,
                    base_title,
                    total,
                    metric,
                    status="failed",
                    stage=result.stage,
                )
                continue

            failure_ledger.succeed(failure_key)

            if not state.master_tags and result.tags:
                state.master_tags = result.tags
            state.pending_concepts = result.pending_concepts

            part_content = result.content
            state.total_output_chars += len(part_content)
            digest_started = time.perf_counter()
            digest = self._normalize_inline_part_digest(result.part_digest, part_number)
            inline_digest = digest is not None
            try:
                if digest is None:
                    digest = self.llm.generate_part_digest(
                        base_title,
                        part_number,
                        total,
                        chunk,
                        part_content,
                        state.pending_concepts,
                    )
            except Exception as e:
                digest_ms = self._elapsed_ms(digest_started)
                logging.warning(f"Part {part_number}/{total} digest failed: {e}")
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": "digest",
                        "error_kind": type(e).__name__,
                        "detail": str(e),
                    }
                )
                metric = {
                    "part": part_number,
                    "resumed": False,
                    "entity_ms": entity_ms,
                    "digest_ms": digest_ms,
                    "commit_ms": 0,
                    "inline_digest": False,
                    "total_ms": self._elapsed_ms(part_started),
                }
                state.part_metrics.append(metric)
                self._report_part_timing(
                    source_filepath,
                    base_title,
                    total,
                    metric,
                    status="failed",
                    stage="digest",
                )
                continue
            digest_ms = self._elapsed_ms(digest_started)
            if not isinstance(digest, dict):
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": "digest",
                        "error_kind": "invalid_digest",
                        "detail": "Digest result was not a mapping.",
                    }
                )
                metric = {
                    "part": part_number,
                    "resumed": False,
                    "entity_ms": entity_ms,
                    "digest_ms": digest_ms,
                    "commit_ms": 0,
                    "inline_digest": False,
                    "total_ms": self._elapsed_ms(part_started),
                }
                state.part_metrics.append(metric)
                self._report_part_timing(
                    source_filepath,
                    base_title,
                    total,
                    metric,
                    status="failed",
                    stage="digest",
                )
                continue
            state.part_digests.append(digest)
            commit_started = time.perf_counter()
            commit_error = ""
            try:
                committed = self._append_part_digest_to_note(
                    result,
                    digest,
                    section_path=part_info.get("section_path"),
                    part_content=part_content,
                    pending_concepts=state.pending_concepts,
                    chunk=chunk,
                )
            except Exception as e:
                committed = False
                commit_error = str(e)
            commit_ms = self._elapsed_ms(commit_started)
            if not committed:
                state.part_digests.pop()
                state.failed_parts.append(
                    {
                        "part": part_number,
                        "stage": "commit",
                        "error_kind": "part_commit_failed",
                        "detail": commit_error
                        or "Part note and digest were not durably committed.",
                    }
                )
                metric = {
                    "part": part_number,
                    "resumed": False,
                    "entity_ms": entity_ms,
                    "digest_ms": digest_ms,
                    "commit_ms": commit_ms,
                    "inline_digest": inline_digest,
                    "total_ms": self._elapsed_ms(part_started),
                }
                state.part_metrics.append(metric)
                self._report_part_timing(
                    source_filepath,
                    base_title,
                    total,
                    metric,
                    status="failed",
                    stage="commit",
                )
                continue
            state.completed_parts.append(part_number)
            if digest.get("degraded"):
                state.degraded_parts.append(part_number)
            metric = {
                "part": part_number,
                "resumed": False,
                "entity_ms": entity_ms,
                "digest_ms": digest_ms,
                "commit_ms": commit_ms,
                "inline_digest": inline_digest,
                "total_ms": self._elapsed_ms(part_started),
            }
            state.part_metrics.append(metric)
            self._report_part_timing(
                source_filepath,
                base_title,
                total,
                metric,
                status="complete",
                stage="commit",
            )

            nav_summary = (
                digest_value_to_text(digest.get("thesis")) if isinstance(digest, dict) else ""
            )
            if not nav_summary:
                nav_summary = part_content.strip().split("\n")[0][:100]
            state.navigation_items.append(
                f"- [[{base_title} (Part {part_number})]]: {nav_summary[:140]}"
            )

            if result.page_path:
                state.part_paths.append(result.page_path)

                self._queue_artifact_if_pending(
                    artifact_dispatcher,
                    result.page_path,
                    chunk,
                    part_content,
                    base_title,
                    part_number,
                )

        state.artifact_metrics.extend(artifact_dispatcher.wait())
        artifact_dispatcher.shutdown()
        return state

    @staticmethod
    def _should_reroll_entity(result: IngestResult) -> bool:
        """One immediate retry for deterministic publication-contract failures.

        Transport outages keep their existing outage semantics. A generic LLM
        error without structured quality issues is not safe to reinterpret as
        content poison, so only parser/publication evidence opens this path.
        """
        if result is None or result or result.transient:
            return False
        return bool(result.issues) and result.stage in {"llm", "parse", "entity_quality"}

    @staticmethod
    def _normalize_inline_part_digest(digest, part_number: int) -> dict | None:
        """Validate the digest embedded in the entity-page response.

        Invalid or incomplete inline data is deliberately rejected so the
        established standalone digest call remains the compatibility fallback.
        """
        if not isinstance(digest, dict):
            return None
        thesis = digest_value_to_text(digest.get("thesis")).strip()
        key_points = digest.get("key_points")
        if not thesis or not isinstance(key_points, list):
            return None
        cleaned_points = [digest_value_to_text(item).strip() for item in key_points]
        cleaned_points = [item for item in cleaned_points if item]
        if not cleaned_points:
            return None
        normalized = dict(digest)
        normalized["part"] = part_number
        normalized["title"] = (
            digest_value_to_text(digest.get("title")).strip() or f"Part {part_number}"
        )
        normalized["thesis"] = thesis
        normalized["key_points"] = cleaned_points
        for key in ("evidence", "terms", "open_questions", "highlights"):
            value = normalized.get(key)
            normalized[key] = value if isinstance(value, list) else []
        normalized["handoff"] = digest_value_to_text(normalized.get("handoff")).strip()
        return normalized

    def _write_synthesis(
        self,
        *,
        base_title: str,
        content: str,
        chunks: list[str],
        source_spans: list[dict],
        part_state: PartState,
        doc_config: dict | None = None,
    ) -> Path:
        from core.version import BUILD_DATE

        syn_persona = (doc_config or {}).get("synthesis_persona", "none")
        syn_template = (
            (doc_config or {}).get("synthesis_template") or settings.USE_TEMPLATE or "wiki-note"
        )

        outcome = self._synthesize_with_critique_retry(
            base_title, part_state, syn_template, syn_persona
        )
        synthesis_text = outcome["text"]
        synthesis_fixes = outcome["fixes"]
        # The synthesis body sits under the shell's page-title H1, so any H1 the
        # model authored (against the template) inverts the hierarchy — demote
        # it to H2 (DocQuality P5.1). The shell's own title is added below.
        synthesis_text, h1_fixes = demote_body_h1(synthesis_text)
        synthesis_fixes = (synthesis_fixes or []) + h1_fixes
        critique_section = outcome["section"]
        critique_verdict = outcome["verdict"]

        # Verdict → publication status. Only a parsed "keep" earns
        # #PerfectPitch; an explicit revise/reject — or a critique that ran
        # but carried no parseable verdict — ships as #NeedsReview so a
        # known-suspect synthesis stops masquerading as clean. When critique
        # is disabled (no section) there is no signal either way.
        if critique_verdict in ("revise", "reject") or (
            critique_section and critique_verdict is None
        ):
            status_tag = "#NeedsReview"
        else:
            status_tag = "#PerfectPitch"
        effective_verdict = critique_verdict or ("unparseable" if critique_section else None)

        # Retry budget is spent by this point: a still-standing revise/reject
        # must be visible where the reader starts, not buried in the critique
        # appendix (the cloud_act failure mode: body said 1180 天, the
        # appendix said that was wrong, nothing connected them).
        warning_block = ""
        if status_tag == "#NeedsReview":
            verdict_key = effective_verdict or "unparseable"
            verdict_zh = {
                "revise": "修訂 (revise)",
                "reject": "拒絕 (reject)",
                "unparseable": "無法解析判定",
            }.get(verdict_key, verdict_key)
            findings = [
                line.strip()
                for line in critique_section.splitlines()
                if "[critical]" in line or "[major]" in line
            ][:3]
            warning_lines = [
                f"> [!warning] 🔔 品質警示（critique 判定：{verdict_zh}）",
                "> 品質審查發現未解決的缺陷，本文可能含有事實錯誤；"
                "請對照文末「🔍 Quality Critique」逐項核對後再引用。",
            ]
            warning_lines.extend(f"> {finding}" for finding in findings)
            warning_block = "\n".join(warning_lines) + "\n\n"

        digest_appendix = self.format_digest_appendix(part_state.part_digests)
        master_tags = part_state.master_tags
        nav_block = "\n".join(part_state.navigation_items)
        syn_nav = (
            f"\n\n---\n## 🔗 原始溯源\n"
            f"*   📚 **[[{base_title} (Stitched)|查看忠實接合版 (Stitched)]]**\n"
            f"*   📄 **[[{base_title}|查看完整原始檔 (Original)]]**"
        )
        tag_line = " ".join(f"#{t}" for t in master_tags if "part-" not in t)
        synthesis_file = PAGES_DIR / base_title / f"{base_title} (Synthesis).md"

        final_content = (
            f"# ✨ {base_title} (Synthesis)\n"
            f"---\n\n"
            f"{warning_block}"
            f"## 📝 Executive Summary\n{synthesis_text}\n\n"
            f"## 📂 Navigation\n{nav_block}{syn_nav}\n\n"
            f"{digest_appendix}\n\n"
            f"{critique_section}"
            f"## 🗺️ Knowledge Map\n(Tags: {tag_line})\n\n"
            f"## 📊 System Metadata\n"
            f"- **Original Content Size**: {len(content)} chars\n"
            f"- **Generated Content Size**: {part_state.total_output_chars} chars\n"
            f"- **Total Parts**: {len(chunks)}\n"
            f"- **Model**: {self.llm.model}\n"
            f"- **Status**: {status_tag}\n"
        )
        final_content, final_fixes = run_markdown_quality_checks(final_content)

        final_meta = {
            "title": f"{base_title} (Synthesis)",
            "type": "synthesis",
            "tags": master_tags or [],
            "status": status_tag,
            "engine_build": BUILD_DATE,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": len(content),
            "output_chars": part_state.total_output_chars,
            "parts_count": len(chunks),
            "part_source_map": source_spans,
            "synthesis_pipeline": "structured-digest-v1",
            "digest_schema": "part-digest-v1",
            "quality_checker": "deterministic-markdown-v1",
        }
        final_meta.update(self._template_stamp(syn_template))
        combined_fixes = self._dedupe_quality_fixes((synthesis_fixes or []) + (final_fixes or []))
        combined_fixes, syn_warnings = self._split_quality_warnings(combined_fixes)
        if combined_fixes:
            final_meta["quality_fixes"] = combined_fixes
        if syn_warnings:
            final_meta["quality_warnings"] = syn_warnings
        if effective_verdict:
            final_meta["quality_verdict"] = effective_verdict
        if SYNTHESIS_CRITIQUE_ENABLED:
            final_meta["critique_attempts"] = outcome["attempts"]
        if len(outcome["verdict_history"]) > 1:
            final_meta["quality_verdict_history"] = outcome["verdict_history"]
        self._attach_trace_metadata(final_meta)

        entity_dir = PAGES_DIR / base_title
        entity_dir.mkdir(parents=True, exist_ok=True)
        rendered = dump_markdown_with_metadata(self._frontmatter_meta(final_meta), final_content)
        existing = synthesis_file.read_text(encoding="utf-8") if synthesis_file.exists() else ""
        basis_hash = artifact_content_hash(synthesis_text)
        prepared = prepare_artifact_slot(
            rendered,
            existing,
            basis_hash=basis_hash,
            enabled=bool(settings.VISUAL_ROUTER_ENABLED),
        )
        with self._commit_guard:
            atomic_write_text(synthesis_file, prepared.text)
        self._record_artifact(
            synthesis_file,
            "synthesis",
            f"{base_title} (Synthesis)",
            final_meta,
            quality_verdict=effective_verdict,
        )

        self.rag.add_document(synthesis_file, base_title, final_content, tags=final_meta["tags"])
        if prepared.should_generate:
            dispatcher = ArtifactJobDispatcher(INGEST_ARTIFACT_WORKERS)
            dispatcher.submit(
                self._run_artifact_job,
                synthesis_file,
                basis_hash,
                synthesis_text,
                base_title,
                "Synthesis",
            )
            part_state.artifact_metrics.extend(dispatcher.wait())
            dispatcher.shutdown()
        return synthesis_file

    # ── Critique post-step (logic: services/ingest/critique_loop.py) ──

    def _synthesize_with_critique_retry(
        self,
        base_title: str,
        part_state,
        syn_template,
        syn_persona,
    ) -> dict:
        """Delegate to SynthesisCritiqueLoop; accepts PartState or the legacy
        dict shape (tests pass dicts). Flags are read HERE so monkeypatching
        this module's SYNTHESIS_CRITIQUE_* keeps working."""
        if isinstance(part_state, PartState):
            digests, pending = part_state.part_digests, part_state.pending_concepts
        else:
            digests = part_state["part_digests"]
            pending = part_state["pending_concepts"]
        return SynthesisCritiqueLoop(self.llm).run(
            base_title,
            part_digests=digests,
            pending_concepts=pending,
            template=syn_template,
            persona=syn_persona,
            enabled=SYNTHESIS_CRITIQUE_ENABLED,
            max_retries=SYNTHESIS_CRITIQUE_MAX_RETRIES,
        )

    def _run_synthesis_critique(
        self,
        base_title: str,
        synthesis_text: str,
        part_digests: list,
    ) -> tuple[str, str | None]:
        return SynthesisCritiqueLoop(self.llm).critique_once(
            base_title, synthesis_text, part_digests, enabled=SYNTHESIS_CRITIQUE_ENABLED
        )

    _parse_verdict = staticmethod(parse_verdict)

    @staticmethod
    def _dedupe_quality_fixes(fixes: list) -> list:
        """Deduplicate structured/string quality fixes while preserving order."""
        seen = set()
        out = []
        for fix in fixes:
            key = tuple(sorted(fix.items())) if isinstance(fix, dict) else ("scalar", str(fix))
            if key in seen:
                continue
            seen.add(key)
            out.append(fix)
        return out

    @staticmethod
    def _split_quality_warnings(fixes: list) -> tuple[list, list]:
        """Separate `warning_*` observations (nothing was changed) from
        applied fixes, so frontmatter doesn't present them as repairs."""

        def is_warning(f) -> bool:
            return isinstance(f, dict) and str(f.get("type", "")).startswith("warning_")

        return [f for f in fixes if not is_warning(f)], [f for f in fixes if is_warning(f)]

    # ── Digest formatting (logic: services/ingest/digest_format.py) ──

    format_digest_appendix = staticmethod(_format_digest_appendix)
    _format_one_digest = staticmethod(_format_one_digest_fn)

    @staticmethod
    def _chunk_fingerprint(chunk: str) -> str:
        """Short content hash of a raw chunk — the resume validity key. Exact
        match by design: any difference in the chunk text (e.g. a chunking-config
        change that shifts Part boundaries) flips the hash, so resume re-distills
        rather than reusing a note whose text no longer matches this Part."""
        return hashlib.sha256((chunk or "").encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _resume_part(cls, part_path: Path, chunk: str | None = None) -> dict | None:
        """For B1 resume: if a part note is already complete — it has the digest
        appendix AND the persisted resume state (`part_digest` in frontmatter) —
        return its reconstructed state so `_process_parts` can skip it. Returns
        None when missing, or finalized by a pre-B1 build (no resume state) so it
        gets re-distilled rather than silently dropped.

        Guarded by `part_chunk_hash`: when the note carries a fingerprint and the
        current chunk's fingerprint differs (the source was re-chunked under a
        different config), the note is stale → return None so it re-distills.
        Notes written before fingerprinting existed carry no hash and resume as
        before (no forced mass re-distillation of the existing corpus)."""
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
        ingest_status = meta.get("ingest_status")
        if ingest_status and ingest_status != "complete":
            return None
        if not ingest_status:
            # Lazy migration: old pages do not trigger a 219-Part re-bill, but
            # known poison shapes fail the current publication gate and are
            # regenerated. New writes always carry the explicit status.
            body, _ = strip_body_frontmatter(text)
            legacy_quality = assess_entity_body(body)
            if legacy_quality.hard_issues or legacy_quality.suspect_issues:
                return None
        stored_hash = meta.get("part_chunk_hash")
        if stored_hash and chunk is not None and stored_hash != cls._chunk_fingerprint(chunk):
            return None  # chunk text changed (re-chunked) → note is stale
        return {
            "tags": meta.get("tags") or [],
            "pending_concepts": meta.get("pending_concepts", "") or "",
            "part_digest": meta.get("part_digest"),
        }

    def _prepare_and_queue_resumed_artifact(
        self,
        dispatcher: ArtifactJobDispatcher,
        page_path: Path,
        chunk: str,
        base_title: str,
        part_number: int,
    ) -> None:
        if not settings.VISUAL_ROUTER_ENABLED:
            return
        basis_hash = artifact_content_hash(chunk)
        with self._commit_guard:
            current = page_path.read_text(encoding="utf-8")
            prepared = prepare_artifact_slot(
                current,
                current,
                basis_hash=basis_hash,
                enabled=True,
            )
            if prepared.text != current:
                self._backup_artifact_patch(page_path, current)
                atomic_write_text(page_path, prepared.text)
        if prepared.should_generate or artifact_slot_status(prepared.text, basis_hash) == "pending":
            self._queue_artifact_if_pending(
                dispatcher,
                page_path,
                chunk,
                core_content_for_artifact(prepared.text),
                base_title,
                part_number,
            )

    def _queue_artifact_if_pending(
        self,
        dispatcher: ArtifactJobDispatcher,
        page_path: Path,
        chunk: str,
        part_content: str,
        base_title: str,
        part_number: int,
    ) -> None:
        basis_hash = artifact_content_hash(chunk)
        try:
            current = page_path.read_text(encoding="utf-8")
        except OSError:
            return
        if artifact_slot_status(current, basis_hash) != "pending":
            return
        dispatcher.submit(
            self._run_artifact_job,
            page_path,
            basis_hash,
            part_content,
            base_title,
            part_number,
            wait_until_running=True,
            job_label=f"Part {part_number} learning aids",
        )

    def _run_artifact_job(
        self,
        page_path: Path,
        basis_hash: str,
        part_content: str,
        base_title: str,
        part_number: int | str,
    ) -> dict:
        from services.learning_artifacts import ArtifactSectionOutcome, maybe_artifact_section

        started = time.perf_counter()
        with self._commit_guard:
            admission = begin_artifact_attempt(
                page_path,
                basis_hash=basis_hash,
                max_attempts=INGEST_ARTIFACT_MAX_ATTEMPTS,
                quarantine_hours=INGEST_ARTIFACT_QUARANTINE_HOURS,
            )
        if not admission.allowed:
            metric: dict = {
                "part": part_number,
                "status": admission.status,
                "generate_ms": 0,
                "apply_ms": 0,
                "total_ms": self._elapsed_ms(started),
                "detail": f"artifact attempt {admission.status}",
                "artifact_types": [],
                "attempt": admission.attempts,
            }
            self._record_artifact(
                page_path,
                "ingestion_artifact_timing",
                f"{base_title} (Part {part_number}) learning aids",
                metric,
            )
            return metric
        generate_started = time.perf_counter()
        raw_outcome = maybe_artifact_section(
            self.llm,
            part_content,
            limit=2,
            exclude_types={"flowchart", "concept_map"},
            return_outcome=True,
        )
        outcome = (
            raw_outcome
            if isinstance(raw_outcome, ArtifactSectionOutcome)
            else ArtifactSectionOutcome(
                "complete" if str(raw_outcome or "").strip() else "not_applicable",
                section=str(raw_outcome or ""),
            )
        )
        section = outcome.section
        generate_ms = self._elapsed_ms(generate_started)
        if outcome.status == "deferred":
            with self._commit_guard:
                defer_artifact_attempt(
                    page_path,
                    basis_hash=basis_hash,
                    detail=outcome.detail,
                    max_attempts=INGEST_ARTIFACT_MAX_ATTEMPTS,
                    quarantine_hours=INGEST_ARTIFACT_QUARANTINE_HOURS,
                    transient=outcome.transient,
                )
            metric = {
                "part": part_number,
                "status": "deferred",
                "generate_ms": generate_ms,
                "apply_ms": 0,
                "total_ms": self._elapsed_ms(started),
                "detail": outcome.detail,
                "artifact_types": list(outcome.artifact_types),
                "attempt": admission.attempts,
                "transient": outcome.transient,
            }
            self._record_artifact(
                page_path,
                "ingestion_artifact_timing",
                f"{base_title} (Part {part_number}) learning aids",
                metric,
            )
            ui.warning(f"Part {part_number} learning aids deferred: {outcome.detail}")
            return metric
        apply_started = time.perf_counter()
        with self._commit_guard:
            patch = apply_artifact_section(
                page_path,
                section,
                basis_hash=basis_hash,
                backup_dir=INGEST_ARTIFACT_BACKUP_DIR,
            )
        apply_ms = self._elapsed_ms(apply_started)
        if patch.status == "conflict" and section:
            pending_path = self._save_pending_artifact(
                base_title, part_number, basis_hash, section, patch.detail
            )
            detail = f"{patch.detail}; pending={pending_path}"
        else:
            detail = patch.detail
        status = (
            "complete"
            if patch.status == "applied" and section
            else "skipped"
            if patch.status == "applied"
            else patch.status
        )
        total_ms = self._elapsed_ms(started)
        metric = {
            "part": part_number,
            "status": status,
            "generate_ms": generate_ms,
            "apply_ms": apply_ms,
            "total_ms": total_ms,
            "detail": detail,
            "artifact_types": list(outcome.artifact_types),
        }
        self._record_artifact(
            page_path,
            "ingestion_artifact_timing",
            f"{base_title} (Part {part_number}) learning aids",
            metric,
        )
        if status == "complete":
            ui.info(f"Part {part_number} learning aids ready in {total_ms / 1000:.1f}s")
        elif status == "conflict":
            ui.warning(f"Part {part_number} learning aids preserved for review: {detail}")
        return metric

    @staticmethod
    def _backup_artifact_patch(page_path: Path, current: str) -> Path:
        backup = (
            INGEST_ARTIFACT_BACKUP_DIR
            / artifact_content_hash(str(page_path))[:16]
            / f"{time.time_ns()}.md"
        )
        atomic_write_text(backup, current)
        return backup

    @staticmethod
    def _save_pending_artifact(
        base_title: str,
        part_number: int | str,
        basis_hash: str,
        section: str,
        detail: str,
    ) -> Path:
        safe_title = sanitize_filename(base_title)
        path = (
            INGEST_ARTIFACT_PENDING_DIR
            / safe_title
            / f"Part-{part_number}-{basis_hash[:12]}-{time.time_ns()}.md"
        )
        payload = dump_markdown_with_metadata(
            {
                "title": f"{safe_title} Part {part_number} learning-aid conflict",
                "basis_sha256": basis_hash,
                "reason": detail,
            },
            f"{section.strip()}\n",
        )
        atomic_write_text(path, payload)
        return path

    def _append_part_digest_to_note(
        self,
        ingest_result: IngestResult,
        digest,
        section_path: list | None = None,
        part_content: str | None = None,
        pending_concepts: str = "",
        chunk: str | None = None,
    ) -> bool:
        page_path = ingest_result.page_path if ingest_result else None
        if not page_path:
            return False

        appendix = self.format_digest_appendix([digest])
        if ingest_result.rendered_markdown:
            content = ingest_result.rendered_markdown.rstrip()
        elif page_path.exists():
            content = page_path.read_text(encoding="utf-8").rstrip()
        else:
            return False

        # Inline key-point highlighting (== ==): a deterministic, non-destructive
        # wrap of verbatim spans the digest call already chose. No extra LLM call.
        highlighted = 0
        if settings.HIGHLIGHT_ENABLED and isinstance(digest, dict):
            content, highlighted = self._apply_highlights(
                content,
                digest.get("highlights"),
                settings.HIGHLIGHT_MAX,
            )

        existing = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        basis_hash = artifact_content_hash(chunk or part_content or "")
        prepared = prepare_artifact_slot(
            content,
            existing,
            basis_hash=basis_hash,
            enabled=bool(settings.VISUAL_ROUTER_ENABLED),
        )
        body_full = f"{prepared.text.rstrip()}\n\n{appendix}\n"

        # B1 resume state: persist the carry-forward pending_concepts and the
        # structured digest into the note's frontmatter, so an interrupted run
        # can resume from disk (skip-existing) without re-calling the LLM.
        doc = MarkdownDocument.from_text(body_full, path=page_path)
        doc.meta["pending_concepts"] = pending_concepts or ""
        doc.meta["ingest_status"] = "pending_index"
        if isinstance(digest, dict):
            doc.meta["part_digest"] = digest
            if digest.get("degraded"):
                doc.meta["digest_degraded"] = True
        if chunk is not None:
            doc.meta["part_chunk_hash"] = self._chunk_fingerprint(chunk)
        title = ingest_result.title or page_path.stem
        tags = ingest_result.tags
        with self._commit_guard:
            updated = doc.to_text()
            atomic_write_text(page_path, updated)
            self.rag.add_document(
                page_path, title, updated, tags=tags, section_path=section_path or None
            )
            doc.meta["ingest_status"] = "complete"
            updated = doc.to_text()
            atomic_write_text(page_path, updated)
            self._record_artifact(
                page_path,
                ingest_result.page_type,
                title,
                {**ingest_result.wiki_meta, "ingest_status": "complete"},
            )
        return True

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
            if body[max(0, idx - 2) : idx] == "==":
                continue
            body = f"{body[:idx]}=={phrase}=={body[idx + len(phrase) :]}"
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
        glossary_rows: list[tuple[str, str, str]] = []
        for index, part_path in enumerate(readable_parts, 1):
            # Read the file once and reuse it for both metadata + body extraction.
            content = part_path.read_text(encoding="utf-8")
            part_meta = parse_markdown_metadata(content)
            body = self._extract_stitchable_body(content)
            # Pull each part's glossary out — six near-identical per-part tables
            # merge into one deduped table at the foot of the stitched doc
            # (DocQuality P5.4).
            body, rows = self._split_glossary_section(body)
            glossary_rows.extend(rows)
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
            "tags": sorted(set(tags or [])),
            "status": "#FaithfulStitch",
            "engine_build": BUILD_DATE,
            "date_completed": datetime.now().strftime("%Y-%m-%d"),
            "model": self.llm.model,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "parts_count": len(readable_parts),
            "stitch_pipeline": "part-note-stitch-v2",
            "quality_checker": "deterministic-markdown-v1",
        }

        body = (
            f"# {base_title} (Stitched)\n\n"
            "> 忠實接合版：保留各 Part note 的主要內容，移除每篇的 navigation、metadata 與 digest appendix。"
            "這份文件偏向完整閱讀，不等同於洞察型 Synthesis。\n\n"
            "## 🔗 Navigation\n"
            f"- [[{base_title} (Synthesis)|查看洞察總結 (Synthesis)]]\n"
            f"- [[{base_title}|查看完整原始檔 (Original)]]\n\n"
            "---\n\n" + "\n".join(f"{section}\n" for section in sections)
        )
        merged_glossary = self._merge_glossary(glossary_rows)
        if merged_glossary:
            body += f"\n---\n\n{merged_glossary}"
        body, quality_fixes = run_markdown_quality_checks(body)
        quality_fixes, quality_warnings = self._split_quality_warnings(quality_fixes)
        if quality_fixes:
            metadata["quality_fixes"] = quality_fixes
        if quality_warnings:
            metadata["quality_warnings"] = quality_warnings
        self._attach_trace_metadata(metadata)

        stitched_file = PAGES_DIR / base_title / f"{base_title} (Stitched).md"
        stitched_file.parent.mkdir(parents=True, exist_ok=True)
        stitched_markdown = dump_markdown_with_metadata(self._frontmatter_meta(metadata), body)
        stitched_file.write_text(stitched_markdown, encoding="utf-8")
        self._record_artifact(stitched_file, "stitched_article", metadata["title"], metadata)

        self.rag.add_document(
            stitched_file, f"{base_title} (Stitched)", stitched_markdown, tags=metadata["tags"]
        )
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

    # Glossary section heading (any `#` depth): 詞彙/術語/Glossary/Key Terms.
    _GLOSSARY_HEADING_RE = re.compile(
        r"^#{1,6}\s+.*(?:詞彙|術語|glossary|key terms).*$", re.IGNORECASE
    )
    _TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
    _TABLE_SEP_CELL_RE = re.compile(r"^\s*:?-{2,}:?\s*$")

    @classmethod
    def _split_glossary_section(cls, body: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Remove a glossary section (heading + its markdown table) from a part
        body and return ``(body_without_glossary, data_rows)``.

        Rows are ``(term, translation, note)`` triples; header and separator
        rows are dropped. A part with no glossary returns the body unchanged and
        an empty list."""
        lines = body.split("\n")
        out: list[str] = []
        rows: list[tuple[str, str, str]] = []
        i = 0
        n = len(lines)
        while i < n:
            if cls._GLOSSARY_HEADING_RE.match(lines[i].strip()):
                # Skip the heading and any blank lines, then consume the table.
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                k = j
                table: list[str] = []
                while k < n and cls._TABLE_ROW_RE.match(lines[k]):
                    table.append(lines[k])
                    k += 1
                if table:
                    rows.extend(cls._parse_glossary_rows(table))
                    # Drop the heading + table; resume after the table.
                    i = k
                    # Also swallow a single trailing blank so we don't leave a gap.
                    if i < n and not lines[i].strip():
                        i += 1
                    continue
            out.append(lines[i])
            i += 1
        return "\n".join(out).strip(), rows

    @classmethod
    def _parse_glossary_rows(cls, table: list[str]) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for line in table:
            m = cls._TABLE_ROW_RE.match(line)
            if not m:
                continue
            cells = [c.strip() for c in m.group(1).split("|")]
            if all(cls._TABLE_SEP_CELL_RE.match(c) or c == "" for c in cells):
                continue  # separator row
            term = cells[0]
            # Header row: first cell is a column label, not a term.
            if re.search(r"英文|術語|term", term, re.IGNORECASE) and len(term) < 12:
                continue
            translation = cells[1] if len(cells) > 1 else ""
            note = " ".join(cells[2:]).strip() if len(cells) > 2 else ""
            if term:
                rows.append((term, translation, note))
        return rows

    @staticmethod
    def _merge_glossary(rows: list[tuple[str, str, str]]) -> str:
        """One deduped glossary table, keyed by the (normalized) term. First
        occurrence wins; the richest note across duplicates is kept."""
        if not rows:
            return ""
        merged: dict[str, tuple[str, str, str]] = {}
        for term, translation, note in rows:
            key = re.sub(r"[*`\s]", "", term).lower()
            if not key:
                continue
            if key not in merged:
                merged[key] = (term, translation, note)
            elif len(note) > len(merged[key][2]):
                # Keep the first term/translation but upgrade to a fuller note.
                kept = merged[key]
                merged[key] = (kept[0], kept[1], note)
        lines = [
            "## 📖 詞彙與關鍵術語（全篇合併）",
            "",
            "| 英文術語 | 繁體中文翻譯 | 說明 |",
            "| :--- | :--- | :--- |",
        ]
        for term, translation, note in merged.values():
            lines.append(f"| {term} | {translation} | {note} |")
        return "\n".join(lines)

    def _extract_stitchable_body(self, content_or_path) -> str:
        """Strip frontmatter, navigation, and digest appendix from a part note,
        while preserving inline learning aids on either side of navigation."""
        if isinstance(content_or_path, Path):
            content = content_or_path.read_text(encoding="utf-8")
        else:
            content = content_or_path

        content = _FRONTMATTER_RE.sub("", content, count=1).strip()

        artifacts = artifact_section_from_page(content)

        cut_markers = (
            "\n## 🔗 知識導航",
            "\n" + _PART_DIGEST_HEADER,
            "\n" + _ARTIFACT_HEADER,
            "\n<!-- lingling:learning-aids:start",
        )
        positions = [pos for marker in cut_markers if (pos := content.find(marker)) != -1]
        if positions:
            content = content[: min(positions)].rstrip()

        if artifacts:
            content = f"{content}\n\n{artifacts}"

        content = self._demote_headings(content, levels=2)
        content, _ = run_markdown_quality_checks(content)
        return content.strip()

    @staticmethod
    def _frontmatter_meta(metadata: dict) -> dict:
        """The on-disk frontmatter view: drop ``trace_ids`` (DocQuality P5.3).

        The full per-run trace_id list is a 40+ line block of opaque hashes that
        bloats every note a reader opens. It is fully recoverable from the trace
        store (SQLite, indexed by ``run_id``) via the ``run_id`` we keep, so the
        serialized frontmatter carries only the anchor. The in-memory ``metadata``
        is untouched, so ``_record_artifact`` still links the artifact's trace."""
        return {k: v for k, v in metadata.items() if k != "trace_ids"}

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
