"""CommandDispatcher — intent routing + execution for user prompts (P2c).

Extracted from watchers/prompt_watcher.py, which had grown into a command
dispatcher wearing a watcher's hat. The watcher now only watches (enqueue,
settle, drain under the busy lock) and hands each prompt's text here.

Dispatch surface, in resolution order:
  1. KB management (zip/unzip/reset)           -> maintenance.kb_manager
  2. Brain ops (dream/consolidate/decay/...)    -> maintenance functions
  3. repair_tags                                -> interactive tag repair
  4. research                                   -> ResearchPipeline
  5. any registered agent intent                -> AgentRegistry
  6. fallback                                   -> RAG-grounded Q&A chat
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from agents.registry import AgentRegistry
from core.config import (
    COMMAND_PREFIX,
    FROM_LLM_DIR,
    INDEX_FILE,
    LOAD_SOURCES_MAX_CHARS_PER_SOURCE,
    settings,
)
from services.builtin_adapters import _resolve_source_paths

# Declarative intent routing table.
# Order matters: longer prefixes (e.g. "patrol-tags") must appear before shorter
# ones (e.g. "patrol") to prevent false matches.
# Each entry: (filename_triggers, slash_triggers, intent_key)
INTENT_ROUTES = [
    (["visualize"], ["visualize"], "visualize"),
    (["merge"], ["merge"], "merge"),
    (["lens", "count"], ["lens", "count"], "lens"),
    (["patrol-tags"], ["patrol-tags"], "patrol_tags"),
    (["repair-tags"], ["repair-tags"], "repair_tags"),
    (["patrol"], ["patrol"], "patrol"),
    (["repair-db"], ["repair-db"], "linter"),
    (["insight"], ["insight"], "insight"),
    # Coder track — review packed source code (工程師帽). MUST precede "review":
    # substring routing means the longer trigger is tried first (same rule as
    # patrol-tags before patrol). Dispatches to CodeReviewAgent.
    (["code-review"], ["code-review"], "code-review"),
    # Coder track — map a packed system's architecture (module map + flows +
    # state diagrams). Dispatches to ArchitectAgent.
    (["architect"], ["architect"], "architect"),
    # Publishing track — turn a note's Synthesis into a learning-first blog
    # review/report (報導者／書評人). Dispatches to ReviewAgent.
    (["review"], ["review"], "review"),
    # Publish track step 1 (ling-ling push): transform Blog/ → kafu/content/.
    # Build + deploy stay on the kafu side (`make publish`). Dispatches BlogAgent.
    (["blog"], ["blog"], "blog"),
    (["profiles", "profile"], ["profiles", "profile"], "profiles"),
    # "recalled" before "recall": longer trigger first, else @ling-recalled
    # would false-match the recall (Q&A) route. Fires a spaced-review reinforce.
    (["recalled"], ["recalled"], "recalled"),
    (["recall"], ["recall"], "recall"),
    (["tensions", "tension"], ["tensions", "tension"], "tensions"),
    (["improve", "improvements"], ["improve"], "improve"),
    (["cortex"], ["cortex"], "cortex"),
    # Brain ops — fire a maintenance/cognition pass on demand (TUI or Obsidian).
    # They run the SAME functions the scheduler/daydream pump use, under the
    # busy lock the worker already holds. No agent class; dispatched directly.
    (["resynthesize", "re-synthesize"], ["resynthesize", "re-synthesize"], "resynthesize"),
    (["consolidate"], ["consolidate"], "consolidate"),
    (["dream"], ["dream"], "dream"),
    (["decay"], ["decay"], "decay"),
    (["ledger"], ["ledger"], "ledger"),
    (["assess", "checkup"], ["assess", "checkup"], "assess"),
    # Spaced-review card on demand (小老師出題考你). Daily auto-push runs via the
    # scheduler; this is the manual "give me a card now" trigger.
    (["quiz"], ["quiz"], "quiz"),
    (["plan"], ["plan"], "plan"),
    (["do"], ["do"], "do"),
    (["zip"], ["zip"], "kb_zip"),
    (["unzip"], ["unzip"], "kb_unzip"),
    (["reset"], ["reset"], "kb_reset"),
    (["research"], ["research"], "research"),
]

# Intents dispatched directly to a maintenance/cognition function (no agent).
_BRAIN_OPS = {
    "dream",
    "consolidate",
    "decay",
    "ledger",
    "assess",
    "resynthesize",
    "quiz",
    "recalled",
}

_KB_OPS = {"kb_zip", "kb_unzip", "kb_reset"}


def detect_intent(lower_name: str, lower_query: str) -> str | None:
    """Walk the INTENT_ROUTES table and return the first matching intent key."""
    for filename_triggers, slash_triggers, intent_key in INTENT_ROUTES:
        for trigger in filename_triggers:
            if f"{COMMAND_PREFIX}{trigger}" in lower_name:
                return intent_key
        for trigger in slash_triggers:
            if f"/{trigger}" in lower_query:
                return intent_key
    return None


def detect_planner_flags(lower_query: str) -> dict:
    """Phase 6A flags for opt-in planner preview on high-frequency intents."""
    return {
        "planner_mode": ("planner-mode" in lower_query or "/planner" in lower_query),
        "execute_plan": ("/execute" in lower_query or "/execution" in lower_query),
    }


def load_linked_sources(target_entities: list[str]) -> list[str]:
    """Load explicitly linked vault sources for default Q&A prompts."""
    loaded_sources = []
    max_chars = LOAD_SOURCES_MAX_CHARS_PER_SOURCE
    target_titles = [t.split("|")[0].strip() for t in target_entities]
    for title in target_titles:
        resolved = _resolve_source_paths(title)
        if not resolved:
            continue

        text = "\n\n".join(path.read_text(encoding="utf-8") for path, _ in resolved)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n\n<!-- truncated by PromptWatcher default Q&A -->"
        loaded_sources.append(f"## Source: {title}\n\n{text}")
    return loaded_sources


class CommandDispatcher:
    def __init__(self, llm_client, rag_manager, registry: AgentRegistry | None = None):
        self.llm = llm_client
        self.rag = rag_manager
        self.registry = registry or AgentRegistry(self.llm, self.rag)
        # Owned here (P3): one ResearchPipeline per dispatcher keeps the
        # per-source politeness throttle state alive ACROSS research commands
        # (per-dispatch construction reset the timers).
        from services.research_pipeline import ResearchPipeline

        self.research = ResearchPipeline(self.llm)

    # ── Entry point ────────────────────────────────────────────────────

    def dispatch(self, query_content: str, filepath: Path) -> None:
        """Route one prompt's text to its handler. Caller owns file lifecycle
        (read/archive) and the busy lock."""
        target_entities = re.findall(r"\[\[(.*?)\]\]", query_content)
        lower_query = query_content.lower()
        lower_name = filepath.name.lower()

        intent_key = detect_intent(lower_name, lower_query)

        run_context = (
            self.llm.trace_run(
                intent=intent_key or "chat",
                agent=intent_key,
                trigger_type="prompt_file",
                command_id=filepath.name,
                source_event_id=str(filepath),
                metadata={"target_titles": target_entities},
            )
            if hasattr(self.llm, "trace_run")
            else contextlib.nullcontext()
        )
        with run_context:
            if intent_key in _KB_OPS:
                self._handle_kb_op(intent_key, target_entities)
            # Brain ops — run a cognition/maintenance pass directly (no agent),
            # reusing the busy lock the worker already holds.
            elif intent_key in _BRAIN_OPS:
                res = self._run_brain_op(intent_key, target_entities)
                output_path = (
                    FROM_LLM_DIR / f"✅sys-admin-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
                )
                output_path.write_text(
                    f'---\ntitle: "{intent_key} 報告"\ntype: report_admin\n---\n\n{res}',
                    encoding="utf-8",
                )
            elif intent_key == "repair_tags":
                from maintenance.repair_tags import repair_tags_interactively

                repair_tags_interactively(filepath)
            elif intent_key == "research":
                self._handle_research(query_content, filepath)
            elif intent_key:
                self._handle_agent(intent_key, query_content, lower_query, target_entities)
            else:
                self._handle_chat(query_content, lower_query, target_entities, filepath)

    # ── Handlers ───────────────────────────────────────────────────────

    def _handle_kb_op(self, intent_key: str, target_entities: list[str]) -> None:
        from maintenance.kb_manager import KBManager

        manager = KBManager(self.rag)
        if intent_key == "kb_zip":
            res = f"✅ Backup successful: {manager.zip_kb().name}"
        elif intent_key == "kb_reset":
            res = manager.reset_kb()
        else:
            res = manager.unzip_kb(target_entities[0] if target_entities else None)

        output_path = FROM_LLM_DIR / f"✅sys-admin-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
        output_path.write_text(
            f'---\ntitle: "管理報告"\ntype: report_admin\n---\n\n{res}', encoding="utf-8"
        )

    def _handle_research(self, query_content: str, filepath: Path) -> None:
        rp = self.research
        # We use query_content (original case) but strip the command prefix.
        # Since query_content could contain the trigger anywhere, we remove it.
        # (?!-) so we don't partially strip a @ling-research-done style marker.
        instruction = re.sub(f"(?i){COMMAND_PREFIX}research(?!-)", "", query_content).strip()
        if not instruction:
            instruction = "General topic"

        res = rp.prepare_and_run(instruction, query_content)

        short_topic = filepath.stem[6:] if filepath.stem.startswith("@ling-") else filepath.stem
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        output_path = FROM_LLM_DIR / f"💌re-{short_topic}-{timestamp}.md"
        output_path.write_text(
            f'---\ntitle: "re: {filepath.stem}"\ntype: research\n---\n\n{res}', encoding="utf-8"
        )

    def _handle_agent(
        self, intent_key: str, query_content: str, lower_query: str, target_entities: list[str]
    ) -> None:
        agent = self.registry.get_agent(intent_key)
        if not agent:
            logging.warning(f"No agent found for intent: {intent_key}")
            return

        context = {
            "target_titles": [t.split("|")[0].strip() for t in target_entities],
            "user_directive": query_content,
            "strategy_id": "recency",
            "is_full_report": "/full" in lower_query,
            # Discriminator for agents shared across intents — e.g.
            # LinterAgent serves both "patrol" (full garden report)
            # and "linter" (@ling-repair-db, focused DB repair).
            "intent_key": intent_key,
        }

        template_match = re.search(r"/template[:\s]+([\w-]+)", lower_query)
        if template_match:
            context["forced_template"] = template_match.group(1)

        # Specialized context for InsightAgent
        if intent_key == "insight":
            context.update(detect_planner_flags(lower_query))
            for s_id in getattr(agent, "strategies", {}).keys():
                # `/tag` and `/tags` are documented shortcuts for the
                # tag-cluster strategy (its skill name is "tag-cluster",
                # not "tags" — that's its `method:` field).
                if f"/{s_id}" in lower_query or (s_id == "tag-cluster" and "/tag" in lower_query):
                    context["strategy_id"] = s_id
                    break
        # Specialized context for LingLens/CounterAgent
        elif intent_key == "lens":
            confidence = "medium"
            conf_match = re.search(r"(?:confidence|信心)\s*[:：]\s*(high|medium|low)", lower_query)
            if conf_match:
                confidence = conf_match.group(1)
            context["confidence"] = confidence

        agent.execute(context)

    def _handle_chat(
        self, query_content: str, lower_query: str, target_entities: list[str], filepath: Path
    ) -> None:
        loaded_sources = load_linked_sources(target_entities)
        relevant = self.rag.query_similar_notes(query_content, top_k=settings.SEARCH_DEPTH)
        context_parts = []
        if loaded_sources:
            context_parts.extend(loaded_sources)
        if relevant:
            context_parts.extend(relevant)

        context = (
            "\n---\n".join(context_parts)
            if context_parts
            else (INDEX_FILE.read_text("utf-8") if INDEX_FILE.exists() else "")
        )

        forced_template = None
        template_match = re.search(r"/template[:\s]+([\w-]+)", lower_query)
        if template_match:
            forced_template = template_match.group(1)

        res = self.llm.answer_query(query_content, context, forced_template=forced_template)

        trace_ids = self.llm.current_trace_ids() if hasattr(self.llm, "current_trace_ids") else []
        run_id = self.llm.current_run_id() if hasattr(self.llm, "current_run_id") else None

        short_topic = filepath.stem[6:] if filepath.stem.startswith("@ling-") else filepath.stem
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        output_path = FROM_LLM_DIR / f"💌re-{short_topic}-{timestamp}.md"

        if forced_template:
            # Template path: the model emits its own YAML frontmatter
            # + body, so write it through verbatim rather than wrapping
            # it in the chat-reply envelope. Yields a clean
            # template-shaped document.
            output_path = FROM_LLM_DIR / f"📄{forced_template}-{filepath.stem}.md"
            body = res if res.endswith("\n") else f"{res}\n"
            output_path.write_text(body, encoding="utf-8")
            artifact_type = "report"
            artifact_title = f"{forced_template}: {filepath.stem}"
        else:
            trace_meta = ""
            if run_id or trace_ids:
                trace_meta = f"run_id: {run_id or ''}\ntrace_ids: {trace_ids}\n"
            full_content = (
                f'---\ntitle: "re: {filepath.stem}"\ntype: chat\n{trace_meta}---\n\n'
                f"> {query_content.strip()}\n\n{res}\n"
            )
            output_path.write_text(full_content, encoding="utf-8")
            artifact_type = "chat"
            artifact_title = f"re: {filepath.stem}"

        if hasattr(self.llm, "trace_store"):
            self.llm.trace_store.record_artifact(
                path=output_path,
                artifact_type=artifact_type,
                title=artifact_title,
                trace_id=trace_ids[-1] if trace_ids else None,
                metadata={"run_id": run_id, "trace_ids": trace_ids},
            )

    # ── Brain ops ──────────────────────────────────────────────────────

    def _run_brain_op(self, intent_key: str, target_entities: list[str]) -> str:
        """Run a brain-op maintenance/cognition function directly (no agent) and
        return a one-line human summary for the admin report. Reuses the exact
        functions the scheduler/daydream pump call; the worker already holds the
        busy lock, so these run under the same contention discipline."""
        if intent_key == "resynthesize":
            return self._resynthesize(target_entities)

        # Spaced-review (Phase 2): a card on demand, or a reinforce on recall.
        # Both read/write only Cortex/*.md via cortex_store — no agent needed.
        if intent_key == "quiz":
            from maintenance.spaced_review import run_spaced_review

            result = run_spaced_review(self.llm, self.rag, occasion="Manual")
            return f"[{result.status}] {result.summary}"
        if intent_key == "recalled":
            from maintenance.spaced_review import run_recalled_report

            result = run_recalled_report(target_entities)
            return f"[{result.status}] {result.summary}"

        trace_store = getattr(self.llm, "trace_store", None)
        result_obj: object
        if intent_key == "dream":
            from maintenance.daily_insight import run_daily_insight

            result_obj = run_daily_insight(self.llm, self.rag, occasion="Manual")
        elif intent_key == "consolidate":
            from maintenance.cortex_consolidation import run_consolidation

            result_obj = run_consolidation(self.llm, self.rag)
        elif intent_key == "decay":
            from maintenance.cortex_decay_pass import run_decay_pass

            result_obj = run_decay_pass(self.llm, self.rag)
        elif intent_key == "ledger":
            from maintenance.cortex_ledger import run_ledger_pass

            result_obj = run_ledger_pass(self.llm, self.rag)
        elif intent_key == "assess":
            if trace_store is None:
                return "skipped：沒有 trace store，無法體檢。"
            from maintenance.self_assessment import run_self_assessment

            result_obj = run_self_assessment(trace_store)
        else:
            return f"未知的大腦指令：{intent_key}"

        status = getattr(result_obj, "status", "done")
        # run_* results expose either .message or .summary — accept both.
        message = (
            getattr(result_obj, "message", None)
            or getattr(result_obj, "summary", "")
            or str(result_obj)
        )
        return f"[{status}] {message}"

    def _resynthesize(self, target_entities: list[str]) -> str:
        """Re-queue an already-ingested document for synthesis by copying its
        archived source back into Consolidate/ (ClippingWatcher picks it up).
        Sidecar images are restored too so `images/<title>/` links resolve."""
        from core.config import CONSOLIDATE_DIR, RAW_CONSOLIDATE_DIR

        titles = [t.split("|")[0].strip() for t in target_entities]
        if not titles:
            return "skipped：請以 [[標題]] 指定要重新 synthesis 的文件。"
        done, missing = [], []
        for title in titles:
            src = RAW_CONSOLIDATE_DIR / f"{title}.md"
            if not src.exists():
                missing.append(title)
                continue
            CONSOLIDATE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(CONSOLIDATE_DIR / src.name))
            sidecar = RAW_CONSOLIDATE_DIR / "images" / title
            if sidecar.is_dir():
                dest = CONSOLIDATE_DIR / "images" / title
                if not dest.exists():
                    shutil.copytree(str(sidecar), str(dest))
            done.append(title)
        parts = []
        if done:
            parts.append(f"已重新投入 Consolidate（將重跑 synthesis）：{', '.join(done)}")
        if missing:
            parts.append(f"找不到原始檔（raw/consolidate/）：{', '.join(missing)}")
        return "；".join(parts) or "無動作"
