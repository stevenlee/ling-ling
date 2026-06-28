"""ImproveAgent — the review queue for self-improvement proposals (M3).

`@ling-improve` subcommands (parsed from the prompt body / filename suffix):
  - (none) / `list`     → pending revision proposals (one line each)
  - `show <id>`         → full rationale + unified diff for one proposal
  - `generate`          → run M1→M2→M3 now: assess → diagnose → queue proposals
  - `approve <id>`      → apply a reviewed proposal (backs up the original)
  - `reject <id>`       → discard a proposal

The whole point of M3 is that the system can propose changes to its own
prompts/templates, but a human stays in the loop: generation only queues,
approval is explicit and guarded, and every approve is one rollback away
(the original is backed up under Improvements/_applied/).
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import (
    GUIDELINES_DIR,
    IMPROVEMENTS_APPLIED_DIR,
    IMPROVEMENTS_PENDING_DIR,
    IMPROVEMENTS_REJECTED_DIR,
    PERSONAS_DIR,
    TEMPLATES_DIR,
    WIKI_VAULT_DIR,
)
from core.ui import ui
from services.improvement_store import (
    get_proposal,
    list_proposals,
    approve_proposal,
    reject_proposal,
    unified_diff,
)

_APPROVE_RE = re.compile(r'approve[:\s]+([\w\-.]+)', re.IGNORECASE)
_REJECT_RE = re.compile(r'reject[:\s]+([\w\-.]+)', re.IGNORECASE)
_SHOW_RE = re.compile(r'show[:\s]+([\w\-.]+)', re.IGNORECASE)
_ALLOWED = [TEMPLATES_DIR, PERSONAS_DIR, GUIDELINES_DIR]


class ImproveAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = (context.get("user_directive") or "").strip()

        if (m := _APPROVE_RE.search(directive)):
            body, title = self._approve(m.group(1)), f"Improve-approve-{m.group(1)}"
        elif (m := _REJECT_RE.search(directive)):
            body, title = self._reject(m.group(1)), f"Improve-reject-{m.group(1)}"
        elif (m := _SHOW_RE.search(directive)):
            body, title = self._show(m.group(1)), f"Improve-show-{m.group(1)}"
        elif re.search(r'\bgenerate\b', directive, re.IGNORECASE):
            body, title = self._generate(), "Improve-generate"
        else:
            body, title = self._list(), "Improve-總覽"

        _, full_markdown = self._write_report(title, body, "ins-improve")
        return full_markdown

    # ── subcommands ──────────────────────────────────────────────────

    def _list(self) -> str:
        props = list_proposals(IMPROVEMENTS_PENDING_DIR)
        if not props:
            return ("# 🛠️ 自我改善提案\n\n目前沒有待審提案。\n\n"
                    "用 `@ling-improve generate` 跑一次自評→診斷→產生提案。")
        lines = ["# 🛠️ 自我改善提案（待審）", "",
                 "> 提案不會自動套用。檢視 → `@ling-improve approve <id>` 生效（原檔自動備份）"
                 "／`reject <id>` 退回。", ""]
        for p in props:
            lines.append(f"- `{p['id']}` — 軸「{p['axis']}」→ `{p['target_path']}`")
            lines.append(f"  - 根因：{(p.get('rationale') or '').strip()[:120]}")
            lines.append(f"  - 細看：`@ling-improve show {p['id']}`")
        return "\n".join(lines)

    def _show(self, pid: str) -> str:
        p = get_proposal(pid, IMPROVEMENTS_PENDING_DIR)
        if not p:
            return f"# ❓ 找不到提案 `{pid}`\n\n用 `@ling-improve list` 看現有提案。"
        fixes = "\n".join(f"- {f}" for f in p.get("addressed_fixes", []))
        diff = unified_diff(p)
        edits = p.get("edits") or []
        edits_md = ""
        if edits:
            parts = ["## 套用的編輯（find → replace）", ""]
            for i, e in enumerate(edits, 1):
                why = f" — {e['why']}" if e.get("why") else ""
                parts.append(f"{i}.{why}")
                parts.append(f"   - 原：`{(e.get('find') or '')[:120].replace(chr(10), ' ⏎ ')}`")
                parts.append(f"   - 改：`{(e.get('replace') or '')[:120].replace(chr(10), ' ⏎ ')}`")
            edits_md = "\n".join(parts) + "\n\n"
        return (
            f"# 🔍 提案 `{pid}`\n\n"
            f"- **軸**：{p['axis']}\n- **目標檔**：`{p['target_path']}`\n\n"
            f"## 根因\n{p.get('rationale') or '—'}\n\n"
            f"## 要落實的改善\n{fixes or '—'}\n\n"
            f"{edits_md}"
            f"## 變更（diff）\n```diff\n{diff or '（無差異）'}\n```\n\n"
            f"---\n生效：`@ling-improve approve {pid}`　退回：`@ling-improve reject {pid}`"
        )

    def _approve(self, pid: str) -> str:
        res = approve_proposal(
            pid, vault_dir=WIKI_VAULT_DIR, pending_dir=IMPROVEMENTS_PENDING_DIR,
            applied_dir=IMPROVEMENTS_APPLIED_DIR, allowed_dirs=_ALLOWED,
        )
        (ui.success if res["ok"] else ui.error)(f"improve approve {pid}: {res['message']}")
        head = "✅ 已套用" if res["ok"] else "💧 未套用"
        extra = ("\n\n*原檔已備份到 `Scripture/Improvements/_applied/`,如需回退可從該處還原。*"
                 if res["ok"] else "")
        return f"# {head}：`{pid}`\n\n{res['message']}{extra}"

    def _reject(self, pid: str) -> str:
        res = reject_proposal(pid, pending_dir=IMPROVEMENTS_PENDING_DIR,
                              rejected_dir=IMPROVEMENTS_REJECTED_DIR)
        return f"# 🗑️ 退回 `{pid}`\n\n{res['message']}"

    def _generate(self) -> str:
        trace_store = getattr(self.llm, "trace_store", None)
        if trace_store is None:
            return "# 💦 無法產生\n\nLLM client 沒有 trace_store,無法自評。"
        from maintenance.self_assessment import run_self_assessment
        from maintenance.self_diagnosis import run_self_diagnosis
        from maintenance.self_improve import run_self_improve

        assessment = run_self_assessment(trace_store)
        diagnosis = run_self_diagnosis(self.llm, assessment)
        improve = run_self_improve(self.llm, assessment, diagnosis)

        lines = [
            "# 🛠️ 自我改善：產生提案",
            "",
            f"自評：{assessment.message}",
            "",
        ]
        if improve.proposals:
            lines += ["## 新提案（待審）", ""]
            for p in improve.proposals:
                lines.append(f"- `{p['id']}` → `{p['target_path']}` — `@ling-improve show {p['id']}`")
        else:
            lines.append("本次未產生可提案的修訂。")
        if improve.skipped_axes:
            lines += ["", "## 未提案的軸（需人工/工程,非單一 prompt 可解）", ""]
            lines += [f"- 「{ax}」：{reason}" for ax, reason in improve.skipped_axes]
        lines += ["", "---", "*提案皆需 `@ling-improve approve <id>` 才會生效;原檔會先備份。*"]
        return "\n".join(lines)
