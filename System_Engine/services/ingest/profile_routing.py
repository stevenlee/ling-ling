"""ProfileRouter — persona/template resolution for a document (P2d).

Moved from IngestionPipeline (_resolve_routing / _record_routing_decision /
_classification_prefix / _queue_new_profile). Resolution layers, highest
priority first:

  1. Explicit frontmatter overrides (`synthesis_persona`,
     `synthesis_template`, or a `profile` name).
  2. A registered profile matching `document_type`/`type`, else the
     LLM's closed-choice pick among registered profiles.
  3. The `default` profile; Scripture settings as the last resort.

Unknown document kinds trigger a pending-review bundle (never activated
silently) and fall back to layer 3 for this run.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.config import FROM_LLM_DIR, settings
from core.ui import ui
from services.profile_manager import ProfileManager

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


class ProfileRouter:
    def __init__(self, llm):
        self.llm = llm

    def resolve(self, pm: ProfileManager, meta: dict, content: str, source_filepath: Path) -> dict:
        """Resolve synthesis persona/template via the profile registry."""
        synthesis_persona = meta.get("synthesis_persona")
        synthesis_template = meta.get("synthesis_template")

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
                content_prefix = self.classification_prefix(content)
                choice = self.llm.select_profile(
                    source_filepath.name, content_prefix, pm.selection_options()
                )
                if isinstance(choice, str) and choice != "none":
                    profile = pm.get(choice)
                    layer = "llm_selection"

                # No fit: draft a new bundle for review, then fall through to
                # the default profile for this run (quality over immediacy).
                if profile is None:
                    pending_queued = self.queue_new_profile(
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
        self.record_routing_decision(
            source_filepath, doc_config, layer=layer, pending_queued=pending_queued
        )
        return doc_config

    def record_routing_decision(
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
    def classification_prefix(content: str) -> str:
        """First 500 chars of the body, with any frontmatter stripped."""
        clean_content = content
        if content.startswith("---"):
            match = _FRONTMATTER_RE.match(content)
            if match:
                clean_content = content[match.end() :]
        return clean_content[:500]

    def queue_new_profile(
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
            category = doc_type or self.llm.classify_document(source_filepath.name, content_prefix)
            if not isinstance(category, str):
                return False
            category = re.sub(r"[^a-z0-9\-]", "", category.lower().strip())
            if not category or pm.get(category) or pm.has_pending(category):
                return False

            gen = self.llm.generate_persona_and_template(category)
            if not isinstance(gen, dict) or "Mock" in type(gen).__name__:
                return False
            persona_name = gen.get("persona_name")
            persona_content = gen.get("persona_content")
            template_name = gen.get("template_name")
            template_content = gen.get("template_content")
            if not (
                isinstance(persona_name, str)
                and persona_name
                and isinstance(persona_content, str)
                and persona_content
                and isinstance(template_name, str)
                and template_name
                and isinstance(template_content, str)
                and template_content
            ):
                return False

            persona_name = re.sub(r"[^a-zA-Z0-9\-]", "", persona_name.replace(".md", ""))
            template_name = re.sub(r"[^a-zA-Z0-9\-]", "", template_name.replace(".md", ""))
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
