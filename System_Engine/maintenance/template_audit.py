"""Template audit — find wiki pages rendered with outdated template versions.

Pages stamp `template` / `template_version` into their frontmatter at
generation time (IngestionPipeline._template_stamp). Templates declare a
`version:` in their own frontmatter. This audit walks `pages/`, compares
each stamp against the template's current version, and reports drift so
the vault doesn't silently become a mix of old and new layouts.

Report policy mirrors the routing report: one-line summary always goes to
maintenance.log.md; a full report lands in `fromLingLing/` only when
outdated pages exist.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    FROM_LLM_DIR,
    MAINTENANCE_LOG_FILE,
    PAGES_DIR,
    TEMPLATES_DIR,
)
from core.parser import parse_markdown_metadata


@dataclass
class TemplateAuditResult:
    status: str                       # "succeeded" | "skipped"
    message: str
    scanned: int = 0
    stamped: int = 0
    outdated: dict = field(default_factory=dict)   # template -> list[(page, page_ver, current_ver)]
    report_path: Path | None = None


def current_template_versions(templates_dir: Path = None) -> dict[str, object]:
    """Map template name -> declared frontmatter version (versioned only)."""
    templates_dir = templates_dir or TEMPLATES_DIR
    versions: dict[str, object] = {}
    if not templates_dir.exists():
        return versions
    for path in templates_dir.glob("*.md"):
        if "." in path.stem:          # skip localized variants (foo.zh.md)
            continue
        try:
            meta = parse_markdown_metadata(path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.debug(f"Template audit: cannot parse {path.name}: {e}")
            continue
        version = meta.get("version")
        if version is not None:
            versions[path.stem] = version
    return versions


def run_template_audit(
    *,
    pages_dir: Path = None,
    templates_dir: Path = None,
    report_dir: Path = None,
    log_path: Path = None,
) -> TemplateAuditResult:
    pages_dir = pages_dir or PAGES_DIR
    templates_dir = templates_dir or TEMPLATES_DIR
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE

    versions = current_template_versions(templates_dir)
    if not versions:
        return TemplateAuditResult(
            status="skipped",
            message="No versioned templates found; nothing to audit.",
        )
    if not pages_dir.exists():
        return TemplateAuditResult(status="skipped", message="pages/ does not exist.")

    scanned = 0
    stamped = 0
    outdated: dict[str, list] = defaultdict(list)
    for page in pages_dir.rglob("*.md"):
        scanned += 1
        try:
            meta = parse_markdown_metadata(page.read_text(encoding="utf-8"))
        except Exception:
            continue
        template = meta.get("template")
        page_version = meta.get("template_version")
        if not template or page_version is None:
            continue
        stamped += 1
        current = versions.get(str(template))
        if current is not None and str(page_version) != str(current):
            outdated[str(template)].append(
                (page.relative_to(pages_dir), page_version, current)
            )

    outdated_count = sum(len(v) for v in outdated.values())
    result = TemplateAuditResult(
        status="succeeded",
        message=(
            f"Template audit: {scanned} pages scanned, {stamped} stamped, "
            f"{outdated_count} outdated across {len(outdated)} template(s)."
        ),
        scanned=scanned,
        stamped=stamped,
        outdated=dict(outdated),
    )

    _append_maintenance_log(log_path, result)
    if outdated_count:
        result.report_path = _write_full_report(report_dir, result, versions)
    return result


def _append_maintenance_log(log_path: Path, result: TemplateAuditResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Template Audit | {result.message}\n")
    except Exception as e:
        logging.warning(f"Template audit: failed to append maintenance log: {e}")


def _write_full_report(
    report_dir: Path, result: TemplateAuditResult, versions: dict
) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"✅sys-template-audit-{stamp}.md"

        lines = [
            "# 🧱 Template 版本稽核",
            "",
            f"- 掃描頁面：**{result.scanned}**，其中 **{result.stamped}** 頁帶版本戳記",
            f"- 過期頁面：**{sum(len(v) for v in result.outdated.values())}**",
            "",
        ]
        for template, pages in sorted(result.outdated.items()):
            lines.append(f"## `{template}`（目前版本 {versions.get(template)}）")
            lines.append("")
            for rel_path, page_ver, current in pages[:50]:
                lines.append(f"- `pages/{rel_path}`：v{page_ver} → v{current}")
            if len(pages) > 50:
                lines.append(f"- …以及另外 {len(pages) - 50} 頁（完整清單見 maintenance log）")
            lines.append("")
        lines += [
            "---",
            "*過期頁面不會被自動重渲染。要更新可在文件 frontmatter 指定 "
            "`profile:` 後重新 ingest 原始檔，或保留舊版（戳記僅供追蹤）。*",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Template audit: failed to write report: {e}")
        return None
