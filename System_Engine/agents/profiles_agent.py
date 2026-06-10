"""ProfilesAgent — manage routing profiles from a `@ling-profiles` prompt.

Subcommands (parsed from the prompt body / filename suffix):
  - (none) / `list`    → overview of active profiles + pending drafts
  - `pending`          → details of each _pending/ draft bundle
  - `approve <name>`   → activate a reviewed draft: persona → Personas/,
                         template → Templates/, profile → Profiles/

Approve is the ergonomic end of the review queue: drafts are quality-gated
on the way in (never auto-activated), and one command on the way out.
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import (
    FROM_LLM_DIR,
    PERSONAS_DIR,
    PROFILES_DIR,
    PROFILES_PENDING_DIR,
    TEMPLATES_DIR,
)
from core.ui import ui
from services.profile_manager import ProfileManager, _parse_profile_file

_APPROVE_RE = re.compile(r'approve[:\s]+([\w\-]+)', re.IGNORECASE)


class ProfilesAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = (context.get("user_directive") or "").strip()
        pm = ProfileManager(PROFILES_DIR, pending_dir=PROFILES_PENDING_DIR)

        approve_match = _APPROVE_RE.search(directive)
        if approve_match:
            body, title = self._approve(pm, approve_match.group(1).lower())
        elif re.search(r'\bpending\b', directive, re.IGNORECASE):
            body, title = self._render_pending(pm), "Profiles-待審草稿"
        else:
            body, title = self._render_list(pm), "Profiles-總覽"

        path, full_markdown = self._write_report(title, body, "report_profiles")
        ui.success(f"Profiles 報告完成：{path.name}")
        return full_markdown

    # ── Subcommands ──────────────────────────────────────────────────

    def _approve(self, pm: ProfileManager, name: str) -> tuple[str, str]:
        result = pm.approve_pending(
            name,
            personas_dir=PERSONAS_DIR,
            templates_dir=TEMPLATES_DIR,
            notify_dir=FROM_LLM_DIR,
        )
        if result["ok"]:
            moved = "\n".join(f"- `{p}`" for p in result["moved"])
            body = (
                f"# ✅ Profile「{name}」已生效\n\n"
                f"以下檔案已搬入正式位置：\n\n{moved}\n\n"
                f"下一次 ingestion 即會納入路由選項。"
            )
            ui.success(f"Profile approved: {name}")
        else:
            errors = "\n".join(f"- {e}" for e in result["errors"])
            body = (
                f"# ❌ Profile「{name}」生效失敗\n\n{errors}\n\n"
                f"請檢查 `Scripture/Profiles/_pending/{name}/` 後重試。"
            )
            ui.error(f"Profile approve failed: {name}")
        return body, f"Profiles-approve-{name}"

    # ── Rendering ────────────────────────────────────────────────────

    @staticmethod
    def _render_list(pm: ProfileManager) -> str:
        lines = ["# 🧭 Routing Profiles 總覽", "", "## 已生效", ""]
        specs = pm.all()
        if specs:
            lines.append("| Profile | Persona | Template | 適用情境 |")
            lines.append("| --- | --- | --- | --- |")
            for s in sorted(specs, key=lambda x: x.name):
                hint = (s.applicable_when or s.description or "").replace("|", "/")
                lines.append(f"| `{s.name}` | {s.persona} | {s.template} | {hint[:80]} |")
        else:
            lines.append("（尚無 profile——放入 `Scripture/Profiles/*.md` 即生效）")

        pending = pm.list_pending()
        lines += ["", "## ⏳ 待審草稿", ""]
        if pending:
            for name in pending:
                lines.append(f"- `{name}` — `@ling-profiles approve {name}` 一鍵生效")
        else:
            lines.append("（無）")
        return "\n".join(lines)

    @staticmethod
    def _render_pending(pm: ProfileManager) -> str:
        lines = ["# ⏳ 待審核的 Profile 草稿", ""]
        pending = pm.list_pending()
        if not pending:
            return "# ⏳ 待審核的 Profile 草稿\n\n目前沒有待審草稿。"
        for name in pending:
            bundle = pm.pending_dir / name
            spec = _parse_profile_file(bundle / f"{name}.md")
            lines.append(f"## `{name}`")
            if spec:
                lines.append(f"- Persona：`{spec.persona}`")
                lines.append(f"- Template：`{spec.template}`")
                if spec.description:
                    lines.append(f"- 描述：{spec.description}")
            files = sorted(p.name for p in bundle.glob("*.md"))
            lines.append(f"- 檔案：{', '.join(f'`{f}`' for f in files)}")
            lines.append(f"- 生效指令：`@ling-profiles approve {name}`")
            lines.append("")
        return "\n".join(lines)
