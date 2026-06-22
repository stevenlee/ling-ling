"""Single source of truth for the commands the TUI can compose.

Each CommandSpec knows the `@ling-<trigger>` filename token (which is what
PromptWatcher routes on) and the fields the user fills in. `build_command_file`
renders a file in the EXACT text format PromptWatcher parses — wikilinks,
`/slash` options, `Confidence:` / `as <type>` lines — so a round-trip test can
feed the output back through PromptWatcher and confirm it routes correctly.

Keep this list aligned with watchers/prompt_watcher.py::INTENT_ROUTES.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.config import COMMAND_PREFIX

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str            # "links" | "text" | "choice" | "flag"
    required: bool = False
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class CommandSpec:
    intent: str          # must equal PromptWatcher intent_key (asserted in tests)
    trigger: str         # the @ling-<trigger> filename token
    label: str
    group: str           # "Brain" | "Cortex" | "Maintenance" | "KB"
    help: str = ""
    fields: tuple[Field, ...] = ()


_TARGETS = Field("targets", "目標 [[筆記]]", "links", help="一個或多個 wikilink 目標")
_BODY = Field("body", "指令內文", "text", help="自由文字指示")

# Ordered for display; grouped. Triggers/intents mirror INTENT_ROUTES.
COMMANDS: tuple[CommandSpec, ...] = (
    # ── Brain ops (fire a cognition/maintenance pass now) ──
    CommandSpec("dream", "dream", "做夢 (生成一條反思)", "Brain",
                "立刻跑一次 doc-anchored 洞察生成"),
    CommandSpec("consolidate", "consolidate", "鞏固 (insight→Cortex 主張)", "Brain",
                "消化未鞏固的 insight 積壓"),
    CommandSpec("decay", "decay", "衰減 / 強化 Cortex", "Brain"),
    CommandSpec("ledger", "ledger", "Cortex 帳本 (falsification)", "Brain"),
    CommandSpec("assess", "assess", "自我體檢 (read-only)", "Brain",
                "彙整品質訊號成健康記分卡"),
    CommandSpec("resynthesize", "resynthesize", "重新 synthesis 一份文件", "Brain",
                "把原始檔重新投入 Consolidate", fields=(Field("targets", "文件 [[標題]]", "links", required=True),)),
    CommandSpec("insight", "insight", "洞察 (可指定策略/目標)", "Brain", fields=(
        _TARGETS, _BODY,
        Field("strategy", "策略", "choice",
              choices=("montecarlo", "recency", "tag-cluster"), help="/<策略>"),
        Field("planner", "planner 預覽", "flag"),
        Field("execute", "執行計畫", "flag"),
    )),
    # ── Cortex queries ──
    CommandSpec("review", "review", "書評／報導 (發布稿)", "Cortex",
                "把一篇 Synthesis 寫成助學習的書評/報導；genre 省略時：標題有專利號→patent，否則 book",
                fields=(
                    Field("targets", "目標 [[筆記]]", "links", required=True),
                    Field("as_type", "genre", "choice",
                          choices=("book", "explainer", "paper", "patent")),
                )),
    CommandSpec("recall", "recall", "回想 (蒸餾主張)", "Cortex", fields=(_TARGETS, _BODY)),
    CommandSpec("tensions", "tensions", "知識張力掃描", "Cortex"),
    CommandSpec("cortex", "cortex", "Cortex 三層驗證", "Cortex"),
    CommandSpec("visualize", "visualize", "視覺化 (圖表)", "Cortex", fields=(
        Field("targets", "目標 [[筆記]]", "links", required=True),
        Field("as_type", "指定類型", "choice", choices=(
            "comparison_table", "flowchart", "mindmap", "timeline",
            "quadrant", "concept_map", "argument_map")),
    )),
    CommandSpec("lens", "lens", "概念透鏡 (掃描實例)", "Cortex", fields=(
        Field("targets", "目標 [[筆記]]", "links", required=True),
        Field("body", "Count: 概念", "text", required=True, help="要找的概念"),
        Field("confidence", "信心", "choice", choices=("high", "medium", "low")),
    )),
    # ── Maintenance ──
    CommandSpec("merge", "merge", "合併筆記", "Maintenance",
                fields=(Field("targets", "[[A]] [[B]]", "links", required=True),)),
    CommandSpec("patrol", "patrol", "全庫健康巡邏", "Maintenance"),
    CommandSpec("linter", "repair-db", "資料庫修復同步", "Maintenance"),
    CommandSpec("patrol_tags", "patrol-tags", "標籤巡邏", "Maintenance"),
    CommandSpec("repair_tags", "repair-tags", "標籤修復", "Maintenance"),
    CommandSpec("profiles", "profiles", "Profile 路由管理", "Maintenance", fields=(_BODY,)),
    # ── KB admin ──
    CommandSpec("kb_zip", "zip", "備份知識庫", "KB"),
    CommandSpec("kb_unzip", "unzip", "還原知識庫", "KB", fields=(_TARGETS,)),
)


def build_command_file(spec: CommandSpec, values: dict, *, stamp: str) -> tuple[str, str]:
    """Render (filename, content) for a command. `stamp` is the timestamp token
    (caller supplies it so the result is deterministic for tests)."""
    lines: list[str] = []

    targets = values.get("targets") or []
    if isinstance(targets, str):
        # Titles contain spaces/parens (e.g. "X (Stitched)"), so NEVER split on
        # whitespace. If the user typed Obsidian-style [[...]] links, extract
        # those verbatim; otherwise the whole field is a single title.
        s = targets.strip()
        bracketed = [t.strip() for t in _WIKILINK_RE.findall(s)]
        targets = bracketed if bracketed else ([s] if s else [])
    if targets:
        lines.append(" ".join(f"[[{t}]]" for t in targets))

    body = (values.get("body") or "").strip()
    if body:
        # lens uses "Count: <concept>"; everything else treats body as directive.
        lines.append(f"Count: {body}" if spec.intent == "lens" else body)

    for f in spec.fields:
        if f.kind == "flag" and values.get(f.key):
            lines.append(f"/{f.key}")
        elif f.kind == "choice" and values.get(f.key):
            v = str(values[f.key]).strip()
            if not v:
                continue
            if f.key == "confidence":
                lines.append(f"Confidence: {v}")
            elif f.key == "as_type":
                lines.append(f"as {v}")
            elif f.key == "template":
                lines.append(f"/template {v}")
            else:  # strategy and any other slash-style choice
                lines.append(f"/{v}")

    content = ("\n".join(lines).strip() + "\n") if lines else "\n"
    filename = f"{COMMAND_PREFIX}{spec.trigger}-{stamp}.md"
    return filename, content
