"""SQLite-backed trace/event store for LLM-driven runs.

The schema is intentionally a little broader than the first writer needs:
critique loops, retrieval explain events, planner decisions, and maintenance
jobs should all be able to read the same run/call/artifact spine later.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from core.config import DATABASE_DIR


_CURRENT_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lingling_trace_run_id", default=None
)
_CURRENT_TRACE_IDS: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "lingling_trace_ids", default=()
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hash_text(value: Any) -> str:
    if isinstance(value, str):
        payload = value
    else:
        try:
            payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            payload = str(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class TraceStore:
    """Append-only-ish trace store with a context-local current run."""

    def __init__(self, db_path: Path | None = None, retention_days: int = 30):
        self.db_path = db_path or (DATABASE_DIR / "llm_trace.sqlite")
        self.retention_days = retention_days
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    parent_run_id TEXT,
                    source_event_id TEXT,
                    command_id TEXT,
                    intent TEXT,
                    agent TEXT,
                    trigger_type TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS llm_calls (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    parent_trace_id TEXT,
                    ts TEXT NOT NULL,
                    stage TEXT,
                    persona TEXT,
                    operation TEXT,
                    template TEXT,
                    provider TEXT,
                    model TEXT,
                    prompt_hash TEXT NOT NULL,
                    response_hash TEXT,
                    prompt_text TEXT NOT NULL,
                    user_msg_json TEXT NOT NULL,
                    response_text TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    latency_ms INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    trace_id TEXT,
                    ts TEXT NOT NULL,
                    path TEXT,
                    artifact_type TEXT,
                    title TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    quality_verdict TEXT,
                    quality_score REAL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(trace_id) REFERENCES llm_calls(trace_id)
                );

                CREATE TABLE IF NOT EXISTS retrieval_events (
                    retrieval_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    trace_id TEXT,
                    ts TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_text TEXT,
                    top_k INTEGER,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    results_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(trace_id) REFERENCES llm_calls(trace_id)
                );

                CREATE INDEX IF NOT EXISTS idx_llm_calls_run_id ON llm_calls(run_id);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_stage ON llm_calls(stage);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_operation ON llm_calls(operation);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
                CREATE INDEX IF NOT EXISTS idx_retrieval_events_run_id ON retrieval_events(run_id);
                """
            )
            # Phase 5C migration: add parent_run_id column to existing DBs.
            # SQLite ALTER TABLE ADD COLUMN is idempotent only via try/except.
            try:
                conn.execute("ALTER TABLE runs ADD COLUMN parent_run_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)"
            )

    @contextlib.contextmanager
    def run(
        self,
        *,
        intent: str | None = None,
        agent: str | None = None,
        trigger_type: str | None = None,
        command_id: str | None = None,
        source_event_id: str | None = None,
        metadata: dict | None = None,
        parent_run_id: str | None = None,
    ) -> Iterator[str]:
        """Open a TraceStore run. Nests automatically: if invoked inside an
        outer `run()` context, `parent_run_id` defaults to the outer
        run_id, creating a parent→child relationship in the `runs` table.

        Phase 5C uses this so PipelineRunner can open a per-step child
        run that inherits the pipeline-level parent — and any nested
        agent calls within the step automatically attribute to the
        child via the existing _CURRENT_RUN_ID ContextVar.
        """
        run_id = f"run_{uuid.uuid4().hex}"
        if parent_run_id is None:
            parent_run_id = _CURRENT_RUN_ID.get()
        started_at = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, parent_run_id, source_event_id, command_id,
                    intent, agent, trigger_type, status, started_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    parent_run_id,
                    source_event_id,
                    command_id,
                    intent,
                    agent,
                    trigger_type,
                    "running",
                    started_at,
                    _json_dumps(metadata or {}),
                ),
            )

        run_token = _CURRENT_RUN_ID.set(run_id)
        trace_token = _CURRENT_TRACE_IDS.set(())
        status = "succeeded"
        error = None
        try:
            yield run_id
        except Exception as e:
            status = "failed"
            error = str(e)
            raise
        finally:
            _CURRENT_TRACE_IDS.reset(trace_token)
            _CURRENT_RUN_ID.reset(run_token)
            # Guard the finalize write: if the body raised, that exception is
            # propagating now, and an unguarded DB error here would replace it
            # — masking the real failure (audit R7-E). Log and swallow instead.
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE runs SET status = ?, ended_at = ?, error = ? WHERE run_id = ?",
                        (status, _utc_now(), error, run_id),
                    )
            except Exception as db_err:
                logging.error(f"TraceStore: failed to finalize run {run_id}: {db_err}")

    def current_run_id(self) -> str | None:
        return _CURRENT_RUN_ID.get()

    def current_trace_ids(self) -> list[str]:
        return list(_CURRENT_TRACE_IDS.get())

    def record_llm_call(
        self,
        *,
        system_prompt: str,
        user_msg: Any,
        response_text: str | None,
        provider: str,
        model: str,
        stage: str | None = None,
        persona: str | None = None,
        operation: str | None = None,
        template: str | None = None,
        parent_trace_id: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        latency_ms: int | None = None,
        status: str = "succeeded",
        error: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        trace_id = f"llm_{uuid.uuid4().hex}"
        run_id = self.current_run_id()
        user_msg_json = _json_dumps(user_msg)
        prompt_hash = _hash_text({"system": system_prompt, "user": user_msg})
        response_hash = _hash_text(response_text or "") if response_text is not None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    trace_id, run_id, parent_trace_id, ts, stage, persona,
                    operation, template, provider, model, prompt_hash,
                    response_hash, prompt_text, user_msg_json, response_text,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms,
                    status, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    run_id,
                    parent_trace_id,
                    _utc_now(),
                    stage,
                    persona,
                    operation,
                    template,
                    provider,
                    model,
                    prompt_hash,
                    response_hash,
                    system_prompt,
                    user_msg_json,
                    response_text,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    latency_ms,
                    status,
                    error,
                    _json_dumps(metadata or {}),
                ),
            )

        if run_id:
            current = _CURRENT_TRACE_IDS.get()
            _CURRENT_TRACE_IDS.set(current + (trace_id,))
        return trace_id

    def record_artifact(
        self,
        *,
        path: str | Path | None,
        artifact_type: str,
        title: str | None = None,
        trace_id: str | None = None,
        metadata: dict | None = None,
        quality_verdict: str | None = None,
        quality_score: float | None = None,
    ) -> str:
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, trace_id, ts, path, artifact_type,
                    title, metadata_json, quality_verdict, quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    self.current_run_id(),
                    trace_id,
                    _utc_now(),
                    str(path) if path is not None else None,
                    artifact_type,
                    title,
                    _json_dumps(metadata or {}),
                    quality_verdict,
                    quality_score,
                ),
            )
        return artifact_id

    def record_retrieval_event(
        self,
        *,
        query_text: str,
        top_k: int,
        options: dict,
        results: list[dict],
        trace_id: str | None = None,
        status: str = "succeeded",
        error: str | None = None,
    ) -> str:
        retrieval_id = f"retrieval_{uuid.uuid4().hex}"
        query_hash = _hash_text(query_text)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO retrieval_events (
                    retrieval_id, run_id, trace_id, ts, query_hash,
                    query_text, top_k, options_json, results_json, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retrieval_id,
                    self.current_run_id(),
                    trace_id,
                    _utc_now(),
                    query_hash,
                    query_text,
                    top_k,
                    _json_dumps(options),
                    _json_dumps(results),
                    status,
                    error,
                ),
            )
        return retrieval_id

    def get_retrieval_events_by_run(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM retrieval_events WHERE run_id = ? ORDER BY ts ASC",
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _since_cutoff(days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def query_artifacts(self, artifact_type: str, since_days: int = 7) -> list[dict]:
        """Fetch artifacts of one type within the window, metadata parsed.

        Powers maintenance analytics (e.g. routing reports over
        `routing_decision` artifacts). Returns newest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_type = ? AND ts >= ? ORDER BY ts DESC",
                (artifact_type, self._since_cutoff(since_days)),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["metadata"] = json.loads(record.get("metadata_json") or "{}")
            except Exception:
                record["metadata"] = {}
            out.append(record)
        return out

    def query_llm_calls(self, stage: str, since_days: int = 7) -> list[dict]:
        """Fetch llm_calls for one stage within the window, metadata parsed.

        Excludes prompt/response bodies — analytics only needs the
        envelope (status, latency, metadata). Returns newest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trace_id, run_id, ts, stage, provider, model, status,
                       error, latency_ms, total_tokens, metadata_json
                FROM llm_calls WHERE stage = ? AND ts >= ? ORDER BY ts DESC
                """,
                (stage, self._since_cutoff(since_days)),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["metadata"] = json.loads(record.get("metadata_json") or "{}")
            except Exception:
                record["metadata"] = {}
            out.append(record)
        return out



    def recently_retrieved_titles(self, since_days: int = 30) -> set[str]:
        """Titles that appeared in retrieval results within the window.

        Used by the facet backfill pump to prioritize pages users actually
        query over pages nobody has asked about.
        """
        titles: set[str] = set()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT results_json FROM retrieval_events WHERE ts >= ?",
                    (self._since_cutoff(since_days),),
                ).fetchall()
            for row in rows:
                try:
                    for item in json.loads(row["results_json"] or "[]"):
                        title = (item or {}).get("title")
                        if title:
                            titles.add(str(title))
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"recently_retrieved_titles failed: {e}")
        return titles

    def recent_query_texts(self, since_days: int = 7) -> list[str]:
        """Distinct retrieval query texts in the window, newest first."""
        texts: list[str] = []
        seen: set[str] = set()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT query_text FROM retrieval_events WHERE ts >= ? ORDER BY ts DESC",
                    (self._since_cutoff(since_days),),
                ).fetchall()
            for row in rows:
                query_text = row["query_text"]
                if not query_text or not str(query_text).strip():
                    continue
                text = str(query_text).strip()
                if text not in seen:
                    seen.add(text)
                    texts.append(text)
        except Exception as e:
            logging.debug(f"recent_query_texts failed: {e}")
        return texts

    def prune_old(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM retrieval_events WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM artifacts WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM llm_calls WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        except Exception as e:
            logging.debug(f"TraceStore prune skipped: {e}")


def usage_to_counts(usage: Any) -> tuple[int | None, int | None, int | None]:
    if usage is None:
        return None, None, None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", prompt)
        completion = usage.get("completion_tokens", completion)
        total = usage.get("total_tokens", total)
    return prompt, completion, total


def elapsed_ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))
