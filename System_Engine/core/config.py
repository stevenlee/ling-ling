import logging
import os
import re
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# ─── Static config ────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "vllm").lower()
COMMAND_PREFIX = "@ling-"

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "5000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "500"))

# ─── Thoughtful Splitter (P4 wiring) ──────────────────────────────────
# These env values are the deployment-time DEFAULTS. `use_thoughtful_splitter`
# and `thoughtful_use_llm` are also bound in DynamicSettings, so Scripture.md
# overrides them at runtime (config-in-Scripture convention). The remaining
# flags below stay env-only. See DesignDoc/ThoughtfulSplitter_implementation_plan.md §9.1.

USE_THOUGHTFUL_SPLITTER = os.getenv("USE_THOUGHTFUL_SPLITTER", "false").lower() == "true"
THOUGHTFUL_USE_LLM_FOR_INGEST = os.getenv("THOUGHTFUL_USE_LLM_FOR_INGEST", "true").lower() == "true"
THOUGHTFUL_USE_LLM_FOR_COUNTER = (
    os.getenv("THOUGHTFUL_USE_LLM_FOR_COUNTER", "false").lower() == "true"
)
THOUGHTFUL_EMIT_SUMMARY = os.getenv("THOUGHTFUL_EMIT_SUMMARY", "false").lower() == "true"
THOUGHTFUL_CACHE_DIR = os.getenv("THOUGHTFUL_CACHE_DIR") or None

# Long-doc synthesis runs a Critique pass against the part digests to surface
# source-grounding defects and a keep/revise/reject verdict. Adds one LLM call
# per long-doc ingestion; flip to "false" to disable on cost-constrained runs.
SYNTHESIS_CRITIQUE_ENABLED = os.getenv("SYNTHESIS_CRITIQUE_ENABLED", "true").lower() == "true"
# When the critique verdict is revise/reject, regenerate the synthesis (with
# the findings as feedback) up to this many times. Worst case adds one
# synthesis + one critique call per retry. 0 disables the retry loop.
SYNTHESIS_CRITIQUE_MAX_RETRIES = max(0, int(os.getenv("SYNTHESIS_CRITIQUE_MAX_RETRIES", "1")))
# LingLens quote verification: a report whose deterministically grounded
# quote ratio falls below this gets quality_verdict "revise" instead of
# "keep". Grounding is exact/near-exact substring match — translated or
# heavily paraphrased quotes legitimately miss, hence the lenient default.
LENS_QUOTE_MIN_GROUNDED_RATIO = float(os.getenv("LENS_QUOTE_MIN_GROUNDED_RATIO", "0.8"))
RAG_EXPLAIN_ENABLED = os.getenv("RAG_EXPLAIN_ENABLED", "false").lower() == "true"
MAINTENANCE_SCHEDULER_ENABLED = os.getenv("MAINTENANCE_SCHEDULER_ENABLED", "true").lower() == "true"
MAINTENANCE_POLL_SECONDS = int(os.getenv("MAINTENANCE_POLL_SECONDS", "300"))
RETRIEVAL_BENCH_MIN_PASS_RATE = float(os.getenv("RETRIEVAL_BENCH_MIN_PASS_RATE", "0.8"))
LOAD_SOURCES_MAX_CHARS_PER_SOURCE = int(os.getenv("LOAD_SOURCES_MAX_CHARS_PER_SOURCE", "20000"))

# Phase 0.3.1 — Source Digest Layer
# DIGEST_SOURCES_BUDGET_PER_SOURCE: target output size (chars) for each per-source digest.
# DIGEST_SOURCES_MAX_SOURCE_CHARS:  max raw text sent to LLM for digesting one source.
DIGEST_SOURCES_BUDGET_PER_SOURCE = int(os.getenv("DIGEST_SOURCES_BUDGET_PER_SOURCE", "6000"))
DIGEST_SOURCES_MAX_SOURCE_CHARS = int(os.getenv("DIGEST_SOURCES_MAX_SOURCE_CHARS", "30000"))


# ─── Embedding Configuration ──────────────────────────────────────────
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL") or None
EMBEDDING_CACHE_ENABLED = os.getenv("EMBEDDING_CACHE_ENABLED", "true").lower() == "true"
# Per-input char cap before embedding, to stay under the model's context window
# (Ollama 400s past it). 0 = auto by model: nomic-embed-text has a short context
# (~1200 chars safe), long-context models like bge-m3 get a generous cap. Set
# explicitly to override. NOTE: too small a cap silently embeds only the head of
# each chunk — the bug that crippled vector retrieval under nomic+CHUNK_SIZE=5000.
EMBEDDING_MAX_CHARS = int(os.getenv("EMBEDDING_MAX_CHARS", "0"))

# ─── Reranker Configuration ───────────────────────────────────────────
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_MODEL = os.getenv("RERANKER_MODEL") or "BAAI/bge-reranker-v2-m3"
RERANKER_MULTIPLIER = int(os.getenv("RERANKER_MULTIPLIER", "5"))

# ─── Hybrid Retrieval (BM25 + RRF) ────────────────────────────────────
HYBRID_RETRIEVAL_ENABLED = os.getenv("HYBRID_RETRIEVAL_ENABLED", "false").lower() == "true"
BM25_MULTIPLIER = int(os.getenv("BM25_MULTIPLIER", "3"))

# ─── Cross-lingual retrieval (query translation → candidate net) ──────
# NOTE: the PRIMARY cross-lingual lever is the multilingual reranker
# (RERANKER_ENABLED + bge-reranker-v2-m3) — verified to rank a zh query's
# English doc at #1, taking the bench 0.867→0.933 with NO translation. This
# query-translation path is a secondary RECALL hedge: it only helps when the
# foreign-language doc never reaches the candidate pool at all (so the reranker
# can't rank it). Default off. When on, translate the query into the other
# corpus languages, retrieve per variant, RRF-fuse, rerank against the ORIGINAL
# query. Additive, index-untouched, one extra LLM call per query.
CROSS_LINGUAL_ENABLED = os.getenv("CROSS_LINGUAL_ENABLED", "false").lower() == "true"
# Languages the corpus holds; a query is translated into each of these EXCEPT
# its own detected language. Comma-separated ISO-ish codes.
CROSS_LINGUAL_TARGET_LANGS = [
    s.strip() for s in os.getenv("CROSS_LINGUAL_TARGET_LANGS", "en,zh").split(",") if s.strip()
]

# ─── Per-document cap (anti-flood diversity) ──────────────────────────
# A single high-volume document's many chunks can flood the top results and
# bury the genuinely-relevant doc just below the cut (verified: a NIST query
# returned 9/10 SpaceX chunks, NIST itself at rank 11). Cap how many chunks
# from the SAME source document survive into the final top-k. 0 disables.
# Applied on the non-MMR path (MMR is its own diversity mechanism).
RETRIEVAL_MAX_PER_DOC = int(os.getenv("RETRIEVAL_MAX_PER_DOC", "2"))

# ─── Facet Index (summary-as-pointer retrieval) ───────────────────────
# Part digests (thesis/key_points) are embedded as "facet" entries that
# point back to their source page. A facet hit is dereferenced to the real
# chunk before reranking — facets are retrieval pointers, never content.
FACET_INDEX_ENABLED = os.getenv("FACET_INDEX_ENABLED", "true").lower() == "true"
FACET_MAX_PER_DOC = int(os.getenv("FACET_MAX_PER_DOC", "8"))

# ─── Facet Backfill (idle, low-priority) ──────────────────────────────
# Pages indexed before the facet index existed get their facets backfilled
# one page at a time whenever the system is idle. Strictly lower priority
# than user work: the busy lock arbitrates, steps are small, and the pump
# yields to fresh files in toLingLing/ or Consolidate/.
FACET_BACKFILL_ENABLED = os.getenv("FACET_BACKFILL_ENABLED", "true").lower() == "true"
FACET_BACKFILL_GRACE_SECONDS = int(os.getenv("FACET_BACKFILL_GRACE_SECONDS", "180"))
FACET_BACKFILL_STEP_GAP_SECONDS = int(os.getenv("FACET_BACKFILL_STEP_GAP_SECONDS", "30"))
FACET_BACKFILL_DAILY_BUDGET = int(os.getenv("FACET_BACKFILL_DAILY_BUDGET", "1000"))
FACET_BACKFILL_MAX_ATTEMPTS = int(os.getenv("FACET_BACKFILL_MAX_ATTEMPTS", "3"))
FACET_BACKFILL_MIN_BYTES = int(os.getenv("FACET_BACKFILL_MIN_BYTES", "400"))

# Long-document entity contract. A deterministic poison Part receives one
# bounded content reroll per run; repeated runs are capped by the persistent
# ledger. Changing model, source content, or contract version creates a fresh
# budget because it is a materially different generation attempt.
INGEST_ENTITY_CONTRACT_VERSION = os.getenv("INGEST_ENTITY_CONTRACT_VERSION", "3")
INGEST_ENTITY_MAX_ATTEMPTS = max(1, int(os.getenv("INGEST_ENTITY_MAX_ATTEMPTS", "3")))
INGEST_ENTITY_QUARANTINE_HOURS = max(1, int(os.getenv("INGEST_ENTITY_QUARANTINE_HOURS", "24")))
INGEST_ARTIFACT_WORKERS = max(1, int(os.getenv("INGEST_ARTIFACT_WORKERS", "1")))
INGEST_ARTIFACT_MAX_LAG_PARTS = max(1, int(os.getenv("INGEST_ARTIFACT_MAX_LAG_PARTS", "2")))
INGEST_ARTIFACT_MAX_ATTEMPTS = max(1, int(os.getenv("INGEST_ARTIFACT_MAX_ATTEMPTS", "2")))
INGEST_ARTIFACT_QUARANTINE_HOURS = max(1, int(os.getenv("INGEST_ARTIFACT_QUARANTINE_HOURS", "24")))

# ─── Daydream (daytime makeup + spontaneous reflection, idle, low-priority) ──
# Night belongs to the scheduler's deep sleep (1–5am dreaming window). If that
# window is busy the day's cognition is otherwise lost. The DaydreamPump runs
# DURING THE DAY whenever idle, lowest priority of all idle callbacks, one
# small bite per step. Its behavioural knobs (enabled / spontaneous / per-day
# budgets) are Scripture-driven and hot-reloadable — see DynamicSettings below,
# alongside dreaming_from/to. Only the state-file path lives here (infra).

# ─── Cortex Memory Phase 1 ────────────────────────────────────────────
INSIGHT_SIGNALS_ENABLED = os.getenv("INSIGHT_SIGNALS_ENABLED", "true").lower() == "true"
INSIGHT_REFUTE_ENABLED = os.getenv("INSIGHT_REFUTE_ENABLED", "true").lower() == "true"

# ─── Cortex Memory Phase 2 (nightly consolidation) ────────────────────
# Insights with healthy Phase-1 signals are distilled into atomic claims
# and consolidated into Cortex/ pages during the dreaming window. Merging
# only happens on a bidirectional-entailment verdict; everything else
# links. Quotas bound the nightly LLM spend.
CORTEX_CONSOLIDATION_ENABLED = os.getenv("CORTEX_CONSOLIDATION_ENABLED", "true").lower() == "true"
CORTEX_MAX_INSIGHTS_PER_NIGHT = int(os.getenv("CORTEX_MAX_INSIGHTS_PER_NIGHT", "10"))
CORTEX_CONSOLIDATION_MAX_ATTEMPTS = max(1, int(os.getenv("CORTEX_CONSOLIDATION_MAX_ATTEMPTS", "3")))
CORTEX_CONSOLIDATION_QUARANTINE_HOURS = max(
    1, int(os.getenv("CORTEX_CONSOLIDATION_QUARANTINE_HOURS", "24"))
)
CORTEX_MAX_ADJUDICATIONS_PER_NIGHT = int(os.getenv("CORTEX_MAX_ADJUDICATIONS_PER_NIGHT", "20"))
CORTEX_NEIGHBOR_TOP_K = int(os.getenv("CORTEX_NEIGHBOR_TOP_K", "3"))
CORTEX_NEIGHBOR_SIM_THRESHOLD = float(os.getenv("CORTEX_NEIGHBOR_SIM_THRESHOLD", "0.80"))
CORTEX_MAX_VARIANTS = int(os.getenv("CORTEX_MAX_VARIANTS", "5"))

# ─── Cortex Memory Phase 3 (decay: dual-strength S/R model) ───────────
# Storage strength S only grows (spacing effect: gains shrink when R is
# still high); retrievability R is a pure function computed at read time.
# States derive from R with hysteresis. base/growth here are INITIAL
# values — the live params live in CORTEX_DECAY_STATE_FILE and get
# damped-calibrated against the revival rate.
CORTEX_DECAY_ENABLED = os.getenv("CORTEX_DECAY_ENABLED", "true").lower() == "true"
CORTEX_DECAY_BASE_DAYS = float(os.getenv("CORTEX_DECAY_BASE_DAYS", "21"))
CORTEX_DECAY_GROWTH = float(os.getenv("CORTEX_DECAY_GROWTH", "1.8"))
CORTEX_REVALIDATIONS_PER_NIGHT = int(os.getenv("CORTEX_REVALIDATIONS_PER_NIGHT", "3"))
CORTEX_REVIVAL_TARGET_LOW = float(os.getenv("CORTEX_REVIVAL_TARGET_LOW", "0.05"))
CORTEX_REVIVAL_TARGET_HIGH = float(os.getenv("CORTEX_REVIVAL_TARGET_HIGH", "0.10"))

# ─── Cortex Memory Phase 4 (claim ledger + falsified) ─────────────────
# Falsification is conservative: >=2 contradicting claims from
# INDEPENDENT insights, plus an LLM refute confirmation, before a page
# is killed (the file stays — it records what we used to believe).
# Un-merge tracking feeds adjudication strictness: when users keep
# splitting merged pages, equivalent verdicts demote to links.
CORTEX_LEDGER_ENABLED = os.getenv("CORTEX_LEDGER_ENABLED", "true").lower() == "true"
CORTEX_FALSIFY_PER_NIGHT = int(os.getenv("CORTEX_FALSIFY_PER_NIGHT", "2"))
CORTEX_FALSIFY_SAMPLES = max(1, int(os.getenv("CORTEX_FALSIFY_SAMPLES", "3")))
# Cortex Phase 5 (read side). @ling-recall feeds the LLM the whole Cortex when
# it fits (<= LLM_MAX claims) — at this scale retrieval is the wrong tool, the
# LLM handles typos/concepts/framing far better. Above LLM_MAX, hybrid recall
# pre-filters to PREFILTER candidates first. TOP_K caps the rendered citations.
CORTEX_RECALL_TOP_K = max(1, int(os.getenv("CORTEX_RECALL_TOP_K", "8")))
CORTEX_RECALL_LLM_MAX = max(1, int(os.getenv("CORTEX_RECALL_LLM_MAX", "150")))
CORTEX_RECALL_PREFILTER = max(1, int(os.getenv("CORTEX_RECALL_PREFILTER", "40")))
# Cortex Phase 5 F3 (tension digest): a claim is "dogmatic" (echo-chamber fuel)
# when its falsifiability is <= DOGMATIC_FALS yet confidence >= DOGMATIC_CONF;
# "thin evidence" when it has <= THIN_EVIDENCE_MAX sources.
CORTEX_TENSION_DOGMATIC_FALS = float(os.getenv("CORTEX_TENSION_DOGMATIC_FALS", "0.25"))
CORTEX_TENSION_DOGMATIC_CONF = float(os.getenv("CORTEX_TENSION_DOGMATIC_CONF", "0.5"))
CORTEX_TENSION_THIN_EVIDENCE_MAX = max(0, int(os.getenv("CORTEX_TENSION_THIN_EVIDENCE_MAX", "1")))
# Cortex Phase 5 F1 (grounded insight). DEFAULT OFF — must stay off until ALL
# anti-echo-chamber defenses land (injection+gate+framing, provenance firewall
# in consolidation, canary). Inject up to GROUND_TOP_K relevant claims with
# falsifiability >= GROUND_MIN_FALSIFIABILITY as DIALECTICAL priors; ground only
# GROUND_FRACTION of seeds, leaving the rest cold for the echo-chamber canary.
CORTEX_GROUNDED_INSIGHT_ENABLED = (
    os.getenv("CORTEX_GROUNDED_INSIGHT_ENABLED", "false").lower() == "true"
)
CORTEX_GROUND_MIN_FALSIFIABILITY = float(os.getenv("CORTEX_GROUND_MIN_FALSIFIABILITY", "0.5"))
CORTEX_GROUND_TOP_K = max(1, int(os.getenv("CORTEX_GROUND_TOP_K", "3")))
# MMR trade-off when picking priors from the falsifiable pool: 1.0 = pure
# relevance (the old top-k behavior that concentrated grounding on hub claims),
# 0.0 = pure diversity. 0.5 balances relevant-but-distinct priors.
CORTEX_GROUND_MMR_LAMBDA = max(0.0, min(1.0, float(os.getenv("CORTEX_GROUND_MMR_LAMBDA", "0.5"))))
CORTEX_GROUND_FRACTION = max(0.0, min(1.0, float(os.getenv("CORTEX_GROUND_FRACTION", "0.7"))))
# Bounds for M4 auto-tuning of the grounding fraction. Never ground every seed
# (the cold control group must survive for the canary) and never so few that
# grounding is pointless.
CORTEX_GROUND_MIN_FRACTION = max(
    0.0, min(1.0, float(os.getenv("CORTEX_GROUND_MIN_FRACTION", "0.3")))
)
CORTEX_GROUND_MAX_FRACTION = max(
    0.0, min(1.0, float(os.getenv("CORTEX_GROUND_MAX_FRACTION", "0.85")))
)
# Phase 6 (learning artifacts) — the on-demand @ling-visualize is always
# available. The two user-facing OUTPUT PREFERENCES it grows (auto-attach to
# synthesis/insight, extra Mermaid on argument maps) live in Scripture, NOT
# here: they're taste, not deployment risk, so a creator should flip them in
# Scripture.md and have it take effect immediately. See DynamicSettings below
# (VISUAL_ROUTER_ENABLED / ARGUMENT_MAP_MERMAID).
# Metacognition M2 (self-diagnosis): for each red/yellow axis in the M1
# scorecard, run one lean LLM call → structured root-cause + candidate fixes
# (read-only analysis; M3 turns the best into gated proposals). LLM-costed →
# default OFF until validated. SELF_ASSESSMENT_HISTORY_MAX caps the trend log.
SELF_DIAGNOSIS_ENABLED = os.getenv("SELF_DIAGNOSIS_ENABLED", "false").lower() == "true"
SELF_ASSESSMENT_HISTORY_MAX = max(2, int(os.getenv("SELF_ASSESSMENT_HISTORY_MAX", "180")))
# Metacognition M3 (self-improvement): when the weekly task should auto-GENERATE
# revision proposals (queued, never applied) after diagnosis. The @ling-improve
# command can generate/approve on-demand regardless. Default OFF — the system
# should propose changes to itself only when asked, until validated.
SELF_IMPROVE_ENABLED = os.getenv("SELF_IMPROVE_ENABLED", "false").lower() == "true"
# A proposal sitting in _pending past this many days is a stalled review — M3
# surfaces it so the human-gate step doesn't silently rot (the 2026-07-12 audit
# found one proposal pending 28 days, unseen). Read-only nudge, no auto-action.
SELF_IMPROVE_STALE_DAYS = max(1, int(os.getenv("SELF_IMPROVE_STALE_DAYS", "14")))
# Metacognition M4 (numeric auto-tune): the only no-human-gate phase, so it is
# deliberately confined to safe numeric knobs, each bound to an outcome metric,
# adjusted with damping (±20%/step), a min-sample gate, and AUTO-ROLLBACK on
# regression. Models the decay-calibration loop. Default OFF.
AUTOTUNE_ENABLED = os.getenv("AUTOTUNE_ENABLED", "false").lower() == "true"
CORTEX_UNMERGE_STRICT_AT = float(os.getenv("CORTEX_UNMERGE_STRICT_AT", "0.10"))
CORTEX_UNMERGE_RELAX_AT = float(os.getenv("CORTEX_UNMERGE_RELAX_AT", "0.05"))
CORTEX_UNMERGE_MIN_SAMPLES = int(os.getenv("CORTEX_UNMERGE_MIN_SAMPLES", "5"))

# ─── Insight generation mix (backlog: doc-anchored seeds) ─────────────
# Nightly insights target concrete documents chosen by interest-weighted
# sampling with an exploration share — doc-anchored material produces
# more falsifiable claims than vault-wide rumination. Weekly full-vault
# insight is kept as a separate task.
INSIGHT_SEED_EPSILON = float(os.getenv("INSIGHT_SEED_EPSILON", "0.2"))
INSIGHT_SEED_TARGETS = int(os.getenv("INSIGHT_SEED_TARGETS", "2"))

# ─── Paths ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent.parent.absolute()
SYSTEM_ENGINE_DIR = PROJECT_ROOT / "System_Engine"
SCRATCH_DIR = SYSTEM_ENGINE_DIR / "scratch"
PID_FILE = SYSTEM_ENGINE_DIR / "daemon.pid"

WIKI_VAULT_DIR = PROJECT_ROOT / "lings-desktop"
INDEX_FILE = WIKI_VAULT_DIR / "index.md"
LOG_FILE = WIKI_VAULT_DIR / "log.md"
MAINTENANCE_LOG_FILE = WIKI_VAULT_DIR / "maintenance.log.md"
SCRIPTURE_DIR = WIKI_VAULT_DIR / "Scripture"
SCRIPTURE_FILE = SCRIPTURE_DIR / "Scripture.md"
PERSONAS_DIR = SCRIPTURE_DIR / "Personas"
PROFILES_DIR = SCRIPTURE_DIR / "Profiles"
PROFILES_PENDING_DIR = PROFILES_DIR / "_pending"
GUIDELINES_DIR = SCRIPTURE_DIR / "Guidelines"
# Metacognition M3: self-improvement proposal queue. Generated prompt/template
# revisions land in _pending (NEVER auto-applied); approve moves the original
# to _applied/ as a backup and writes the revision. Mirrors Profiles/_pending.
IMPROVEMENTS_DIR = SCRIPTURE_DIR / "Improvements"
IMPROVEMENTS_PENDING_DIR = IMPROVEMENTS_DIR / "_pending"
IMPROVEMENTS_APPLIED_DIR = IMPROVEMENTS_DIR / "_applied"
IMPROVEMENTS_REJECTED_DIR = IMPROVEMENTS_DIR / "_rejected"
TEMPLATES_DIR = WIKI_VAULT_DIR / "Templates"
PROMPTS_DIR = TEMPLATES_DIR / "Prompts"
OPERATIONS_DIR = TEMPLATES_DIR / "Operations"
PAGES_DIR = WIKI_VAULT_DIR / "pages"
NOTES_DIR = WIKI_VAULT_DIR / "Notes"
# Packed source code for @ling-code-review / @ling-architect. A top-level vault
# dir deliberately OUTSIDE pages/Notes/Cortex, so _should_index() never ingests
# or RAG-indexes packed code (see watchers/vault_watcher.py::_should_index).
CODE_REVIEW_DIR = WIKI_VAULT_DIR / "CodeReview"
CORTEX_DIR = WIKI_VAULT_DIR / "Cortex"
INSIGHTS_DIR = WIKI_VAULT_DIR / "Insights"
TAG_MAP_FILE = PAGES_DIR / "_tagScrapbook.md"

CLIPPINGS_DIR = WIKI_VAULT_DIR / "Clippings"
CONSOLIDATE_DIR = WIKI_VAULT_DIR / "Consolidate"
TO_LLM_DIR = WIKI_VAULT_DIR / "toLingLing"
FROM_LLM_DIR = WIKI_VAULT_DIR / "fromLingLing"
EXCALIDRAW_DIR = WIKI_VAULT_DIR / "Excalidraw"
ASSETS_DIR = WIKI_VAULT_DIR / "Assets"
SKILLS_DIR = WIKI_VAULT_DIR / "Skills"
BACKUPS_DIR = PROJECT_ROOT / "Backups"

# Scout (scheduled crawler → daily digest). The targets list is user-edited
# vault content (Scripture/Scout.md frontmatter); behavioral knobs live in
# DynamicSettings below. Only paths here (infra). The mirror dir sits under
# Notes/ ON PURPOSE — fromLingLing/ is not RAG-indexed, Notes/ is.
SCOUT_TARGETS_FILE = SCRIPTURE_DIR / "Scout.md"
SCOUT_MIRROR_DIR = NOTES_DIR / "Scout"

DATABASE_DIR = WIKI_VAULT_DIR / "Database"
MAINTENANCE_STATE_FILE = DATABASE_DIR / "maintenance_state.json"
SCOUT_STATE_FILE = DATABASE_DIR / "scout_state.json"
EVIDENCE_TRACEBACK_STATE_FILE = DATABASE_DIR / "evidence_traceback_state.json"
INSIGHT_SIGNALS_FILE = DATABASE_DIR / "insight_signals.json"
PLANS_DIR = DATABASE_DIR / "plans"
RETRIEVAL_BENCH_FILE = SCRATCH_DIR / "retrieval_bench.yml"
# Auto-grown regression cases live separately so rewrites never clobber the
# hand-written file (or its comments). Wipe the auto file freely to reset.
RETRIEVAL_BENCH_AUTO_FILE = SCRATCH_DIR / "retrieval_bench_auto.yml"
BENCH_HISTORY_FILE = DATABASE_DIR / "bench_history.json"
FACET_BACKFILL_STATE_FILE = DATABASE_DIR / "facet_backfill_state.json"
INGEST_FAILURE_STATE_FILE = DATABASE_DIR / "ingest_failure_state.json"
INGEST_ARTIFACT_BACKUP_DIR = BACKUPS_DIR / "ingest_artifact_patches"
INGEST_ARTIFACT_PENDING_DIR = WIKI_VAULT_DIR / "_pending" / "LearningArtifacts"
DAYDREAM_STATE_FILE = DATABASE_DIR / "daydream_state.json"
# Live daemon status for out-of-process readers (the TUI). Written by
# ui.set_status on every activity transition; .kb_lock only marks hard locks,
# so it can't be the busy signal — the real busy flag is in-memory in the daemon.
DAEMON_STATUS_FILE = DATABASE_DIR / "daemon_status.json"
CORTEX_STATE_FILE = DATABASE_DIR / "cortex_state.json"
CORTEX_ADJUDICATION_CACHE = DATABASE_DIR / "cortex_adjudications.json"
SEED_HISTORY_FILE = DATABASE_DIR / "seed_history.json"
CORTEX_DECAY_STATE_FILE = DATABASE_DIR / "cortex_decay_state.json"
CORTEX_LEDGER_STATE_FILE = DATABASE_DIR / "cortex_ledger_state.json"
# Metacognition: self-assessment scorecard history (one snapshot per run →
# trend), capped append-only. M2 diagnosis reads it for chronic-vs-new.
SELF_ASSESSMENT_HISTORY_FILE = DATABASE_DIR / "self_assessment_history.json"
# Metacognition M4: live auto-tuned numeric knob overrides (seeded from config
# defaults, adjusted by the autotuner, read by consumers). Mirrors how decay
# keeps its live base_days in cortex_decay_state.json.
AUTOTUNE_STATE_FILE = DATABASE_DIR / "autotune_state.json"
BENCH_AUTO_MAX_CASES = int(os.getenv("BENCH_AUTO_MAX_CASES", "30"))
BENCH_AUTO_PER_RUN = int(os.getenv("BENCH_AUTO_PER_RUN", "5"))
RAW_DIR = WIKI_VAULT_DIR / "raw"
RAW_CONSOLIDATE_DIR = RAW_DIR / "consolidate"
RAW_PROMPTS_DIR = RAW_DIR / "prompts"
RAW_ASSETS_DIR = RAW_DIR / "assets"
RAW_MERGED_DIR = RAW_DIR / "merged"


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL | re.MULTILINE)


class DynamicSettings:
    """Settings that can be reloaded at runtime from Scripture.md frontmatter."""

    # (yaml_key, attr_name, type_coercer)
    _BINDINGS: tuple[tuple[str, str, type], ...] = (
        ("be_a", "AGENT_ROLE", str),
        ("say", "OUTPUT_LANGUAGE", str),
        ("use_template", "USE_TEMPLATE", str),
        ("creativity", "CREATIVITY", float),
        ("max_output", "MAX_OUTPUT", int),
        ("memory_limit", "MEMORY_LIMIT", int),
        ("search_depth", "SEARCH_DEPTH", int),
        ("strict_mode", "STRICT_MODE", bool),
        ("digest_limit", "DIGEST_LIMIT", int),
        ("digest_overlap", "DIGEST_OVERLAP", int),
        # Thoughtful-splitter knobs (P2):
        ("overlap_chars", "OVERLAP_CHARS", int),
        ("digest_max_factor", "DIGEST_MAX_FACTOR", float),
        ("digest_min_factor", "DIGEST_MIN_FACTOR", float),
        ("dreaming_from", "DREAMING_FROM", int),
        ("dreaming_to", "DREAMING_TO", int),
        # Daydream: daytime makeup + spontaneous reflection (sibling of dreaming):
        ("daydream", "DAYDREAM_ENABLED", bool),
        ("daydream_spontaneous", "DAYDREAM_SPONTANEOUS_ENABLED", bool),
        ("daydream_consolidation_budget", "DAYDREAM_CONSOLIDATION_BUDGET", int),
        ("daydream_bite_adjudications", "DAYDREAM_BITE_ADJUDICATIONS", int),
        ("daydream_insight_budget", "DAYDREAM_INSIGHT_BUDGET", int),
        ("daydream_spontaneous_budget", "DAYDREAM_SPONTANEOUS_BUDGET", int),
        ("self_healing", "SELF_HEALING", bool),
        # Phase 6 learning-aid output preferences (user taste, hot-reloadable):
        ("visual_router", "VISUAL_ROUTER_ENABLED", bool),
        ("argument_map_mermaid", "ARGUMENT_MAP_MERMAID", bool),
        ("ontology_bias", "ONTOLOGY_BIAS", bool),
        # Inline key-point highlighting on part notes (== == spans):
        ("highlight_spans", "HIGHLIGHT_ENABLED", bool),
        ("highlight_max", "HIGHLIGHT_MAX", int),
        # Splitter selection (moved here per "config in Scripture, not .env"):
        ("use_thoughtful_splitter", "USE_THOUGHTFUL_SPLITTER", bool),
        ("thoughtful_use_llm", "THOUGHTFUL_USE_LLM_FOR_INGEST", bool),
        # index.md "🆕 最近新增" — how many newest docs to surface at the top:
        ("recent_count", "RECENT_COUNT", int),
        # Daily-insight operation rotation: comma-separated Skills/ strategy
        # names cycled deterministically by date (anti-homogenization):
        ("insight_rotation", "INSIGHT_ROTATION", str),
        # Scout crawler digest: master switch, report language ("" = follow
        # OUTPUT_LANGUAGE), and per-target item cap:
        ("scout", "SCOUT_ENABLED", bool),
        ("scout_language", "SCOUT_LANGUAGE", str),
        ("scout_max_items", "SCOUT_MAX_ITEMS_PER_TARGET", int),
        ("scout_fetch_content", "SCOUT_FETCH_CONTENT", bool),
        ("scout_bridging", "SCOUT_BRIDGING", bool),
        ("scout_mirror", "SCOUT_MIRROR", bool),
        # Cortex graph density (O0): the LINK floor decides which neighbor
        # pairs get adjudicated at all (lower = denser graph, more edges);
        # the MERGE floor additionally guards the destructive equivalent→merge
        # path (a low-similarity "equivalent" verdict links instead of merges).
        ("cortex_link_threshold", "CORTEX_LINK_THRESHOLD", float),
        ("cortex_merge_threshold", "CORTEX_MERGE_THRESHOLD", float),
        # Cortex evidence traceback (A2): falsifier-first corroboration scan
        # over thin-evidence claims. Dry-run reporting only in its current
        # form; batch bounds the nightly LLM spend, the distance gate keeps
        # weakly-related passages out of judgment.
        ("evidence_traceback", "EVIDENCE_TRACEBACK_ENABLED", bool),
        ("evidence_traceback_apply", "EVIDENCE_TRACEBACK_APPLY", bool),
        ("evidence_traceback_batch", "EVIDENCE_TRACEBACK_BATCH", int),
        ("evidence_traceback_max_distance", "EVIDENCE_TRACEBACK_MAX_DISTANCE", float),
    )

    def __init__(self):
        self._reload_lock = threading.Lock()
        self.AGENT_ROLE = "assistant"
        self.OUTPUT_LANGUAGE = "Traditional Chinese"
        self.USE_TEMPLATE: str | None = None
        self.DIGEST_LIMIT = 5000
        self.DIGEST_OVERLAP = 500
        # Thoughtful-splitter knobs:
        self.OVERLAP_CHARS = 300  # Phase 3b structural overlap size
        self.DIGEST_MAX_FACTOR = 1.5  # max_size = target * factor
        self.DIGEST_MIN_FACTOR = 0.25  # min_size = target * factor
        self.DREAMING_FROM = 1
        self.DREAMING_TO = 5
        # Daydream knobs (hot-reloadable). Daytime makeup + spontaneous
        # reflection; per-day budgets bound the LLM spend.
        self.DAYDREAM_ENABLED = True
        self.DAYDREAM_SPONTANEOUS_ENABLED = True
        self.DAYDREAM_CONSOLIDATION_BUDGET = 10  # insight-bites/day
        self.DAYDREAM_BITE_ADJUDICATIONS = 4  # per-bite adjudication cap
        self.DAYDREAM_INSIGHT_BUDGET = 1  # makeup generations/day
        self.DAYDREAM_SPONTANEOUS_BUDGET = 1  # spontaneous generations/day
        self.SELF_HEALING = True
        self.CREATIVITY = 0.4
        self.MAX_OUTPUT = 4096
        self.MEMORY_LIMIT = 32768
        self.SEARCH_DEPTH = 3
        self.STRICT_MODE = True
        # Phase 6: auto-attach learning artifacts to synthesis/insight output,
        # and the optional deterministic Mermaid graph on argument maps. Default
        # off — opt in via Scripture.md.
        self.VISUAL_ROUTER_ENABLED = False
        self.ARGUMENT_MAP_MERMAID = False
        # When picking among the "relationship graph" family (concept_map /
        # class_diagram / ontology), bias the classifier toward ontology so any
        # type-able relation (is-a / part-of / instance-of) renders as a proper
        # ontology rather than a flat concept web. Set ontology_bias: false in
        # Scripture to fall back to neutral classification.
        self.ONTOLOGY_BIAS = False
        # Inline highlighting: wrap up to N verbatim key spans in == == on each
        # part note. Spans ride along on the existing part-digest call (no extra
        # LLM round-trip); a deterministic pass applies the markers afterwards.
        self.HIGHLIGHT_ENABLED = True
        self.HIGHLIGHT_MAX = 5
        # index.md surfaces the N most-recently-added docs in a "🆕 最近新增"
        # block at the top (newest first); the alphabetical list stays below.
        # 0 disables the block.
        self.RECENT_COUNT = 15
        self.INSIGHT_ROTATION = "montecarlo"
        # Scout: default OFF — enable with `scout: true` in Scripture.md once
        # Scripture/Scout.md has a targets list.
        self.SCOUT_ENABLED = False
        self.SCOUT_LANGUAGE = ""
        self.SCOUT_MAX_ITEMS_PER_TARGET = 10
        # Fetch each item's page and ground the per-item analysis in the
        # actual content (one extra HTTP GET + LLM call per new item):
        self.SCOUT_FETCH_CONTENT = True
        # P2.3/P2.4: attach related-vault-note [[links]] per item (needs rag),
        # and mirror the report into Notes/Scout/ so it's RAG-searchable:
        self.SCOUT_BRIDGING = True
        self.SCOUT_MIRROR = True
        # Cortex graph density (O0). link < merge: neighbors down to 0.60 get
        # adjudicated (edges), but merging two claims into one still needs 0.80+
        # similarity. The legacy CORTEX_NEIGHBOR_SIM_THRESHOLD (0.80) was one
        # knob doing both jobs — too high, so the graph starved of edges.
        self.CORTEX_LINK_THRESHOLD = 0.60
        self.CORTEX_MERGE_THRESHOLD = 0.80
        # Evidence traceback (A2): default OFF — enable with
        # `evidence_traceback: true` in Scripture.md. Dry-run only for now.
        self.EVIDENCE_TRACEBACK_ENABLED = False
        # Apply mode (default OFF): when true the scan MUTATES claim pages
        # (append evidence / counterpoint, gentle reinforce) instead of only
        # reporting. Opt-in on top of the scan after reviewing dry-run hit rates.
        self.EVIDENCE_TRACEBACK_APPLY = False
        self.EVIDENCE_TRACEBACK_BATCH = 5
        self.EVIDENCE_TRACEBACK_MAX_DISTANCE = 0.45
        # Splitter selection: env value is the default (deployment), but
        # Scripture (use_thoughtful_splitter / thoughtful_use_llm) can override
        # at runtime — see _BINDINGS above.
        self.USE_THOUGHTFUL_SPLITTER = USE_THOUGHTFUL_SPLITTER
        self.THOUGHTFUL_USE_LLM_FOR_INGEST = THOUGHTFUL_USE_LLM_FOR_INGEST

    def reload(self):
        if not SCRIPTURE_FILE.exists():
            return

        try:
            content = SCRIPTURE_FILE.read_text(encoding="utf-8").strip()
            match = _FRONTMATTER_RE.search(content)
            if not match:
                logging.warning("Scripture.md: failed to find valid YAML frontmatter.")
                return
            yaml_data = yaml.safe_load(match.group(1))
            if not yaml_data:
                logging.warning("Scripture.md: frontmatter is empty.")
                return

            # Parse everything first, then apply under the lock in one tight
            # loop so concurrent readers never observe a half-reloaded mix
            # (e.g. new persona with the previous template).
            staged: list[tuple[str, object]] = []
            for key, attr, coercer in self._BINDINGS:
                if key not in yaml_data:
                    continue
                try:
                    value = coercer(yaml_data[key])
                except (TypeError, ValueError) as e:
                    logging.warning(f"Scripture.md: bad value for {key!r}: {e}")
                    continue
                if coercer is str:
                    value = value.lower() if attr == "AGENT_ROLE" else value
                staged.append((attr, value))

            with self._reload_lock:
                for attr, value in staged:
                    setattr(self, attr, value)

            logging.info(
                f"Scripture says: Be a {'strict' if self.STRICT_MODE else 'chatty'} {self.AGENT_ROLE}. "
                f"Search Depth={self.SEARCH_DEPTH}. Read {self.DIGEST_LIMIT} chars. "
                f"Dreaming {self.DREAMING_FROM}-{self.DREAMING_TO}."
            )
        except Exception as e:
            logging.error(f"Failed to reload settings: {e}")


settings = DynamicSettings()


_MANAGED_DIRECTORIES = (
    CLIPPINGS_DIR,
    CONSOLIDATE_DIR,
    TO_LLM_DIR,
    FROM_LLM_DIR,
    PAGES_DIR,
    NOTES_DIR,
    EXCALIDRAW_DIR,
    ASSETS_DIR,
    RAW_CONSOLIDATE_DIR,
    RAW_PROMPTS_DIR,
    RAW_ASSETS_DIR,
    RAW_MERGED_DIR,
    SCRIPTURE_DIR,
    PERSONAS_DIR,
    PROFILES_DIR,
    PROFILES_PENDING_DIR,
    GUIDELINES_DIR,
    SKILLS_DIR,
    BACKUPS_DIR,
    TEMPLATES_DIR,
    PROMPTS_DIR,
    OPERATIONS_DIR,
    SCRATCH_DIR,
    CORTEX_DIR,
    IMPROVEMENTS_PENDING_DIR,
    CODE_REVIEW_DIR,
)


def ensure_directories():
    """Ensure all required directories exist."""
    for directory in _MANAGED_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
