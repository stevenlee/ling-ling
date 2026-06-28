import logging
import re
import sys
from pathlib import Path
from core.config import PROJECT_ROOT, TAG_MAP_FILE, FROM_LLM_DIR, COMMAND_PREFIX
from core.tag_manager import TagManager
from core.vault_utils import update_file_tags
from core.parser import parse_markdown_metadata
from services.llm_client import LLMClient
from agents.tag_patrol_agent import TagPatrolAgent

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def repair_tags_interactively(report_file_override: Path | None = None):
    report_file = report_file_override if report_file_override is not None else PROJECT_ROOT / "lings-desktop" / f"{COMMAND_PREFIX}repair-tags.md"
    
    if not report_file.exists():
        logging.warning(f"{COMMAND_PREFIX}repair-tags.md not found. Generating new report...")
        agent = TagPatrolAgent(LLMClient())
        agent.execute()
        print(f"\n💦 指令檔不存在。系統已自動為您掃描並生成了「{COMMAND_PREFIX}repair-tags.md」。")
        return

    logging.info(f"TagRepair: Reading report from {report_file.name}")
    content = report_file.read_text(encoding='utf-8')
    
    # Updated Regex to match grouped format: - [x] Reason: `bad` -> `good` (Affects: ...) | PATHS: `path1;path2`
    pattern = r'- \[[xvV]\] (.*?): `(.*?)` -> `(.*?)` \(影響: .*?\) \| PATHS: `(.*?)`'
    matches = list(re.finditer(pattern, content))
    
    if not matches:
        logging.info("No checked items detected in the report.")
        print("\nℹ️ 目前報告中沒有勾選任何項目。")
        return

    logging.info(f"TagRepair: Found {len(matches)} groups to fix. Initializing tools...")
    tm = TagManager(TAG_MAP_FILE)
    llm = LLMClient()
    
    repairs_executed = []
    processed_files_count = 0
    learned_count = 0
    
    for m in matches:
        reason_text = m.group(1).strip()
        bad_tag = m.group(2).strip()
        good_tag = m.group(3).strip()
        paths_str = m.group(4).strip()
        filepaths = [Path(p.strip()) for p in paths_str.split(';') if p.strip()]
        
        final_good_tag = good_tag
        
        # Determine translation if needed (only once per group!)
        if good_tag == '[AUTO-LEARN]':
            learned_map = llm.translate_tags([bad_tag])
            target = None
            for k, v in learned_map.items():
                if k.strip().lower() == bad_tag.lower():
                    target = v
                    break
            
            if target:
                tm.add_mapping(bad_tag, target)
                final_good_tag = tm.normalize(target)
                learned_count += 1
            else:
                logging.warning(f"LLM failed to translate tag: {bad_tag}")
                continue
        else:
            final_good_tag = tm.normalize(good_tag)

        # Apply to all files in group
        for filepath in filepaths:
            if not filepath.exists():
                logging.warning(f"File missing: {filepath}")
                continue
            try:
                file_content = filepath.read_text(encoding='utf-8')
                meta = parse_markdown_metadata(file_content)
                current_tags = set(meta.get('tags', []))
                
                # Replace bad with good
                if bad_tag in current_tags:
                    current_tags.remove(bad_tag)
                current_tags.add(final_good_tag)
                
                update_file_tags(filepath, sorted(list(current_tags)))
                processed_files_count += 1
            except Exception as e:
                logging.error(f"Failed to repair {filepath.name}: {e}")

        repairs_executed.append({
            "reason": "修正格式" if "格式" in reason_text else "補齊英文對照",
            "bad": bad_tag,
            "good": final_good_tag,
            "count": len(filepaths)
        })
        logging.info(f"Applied bulk fix: {bad_tag} -> {final_good_tag} ({len(filepaths)} files)")

    # 3. Report Success
    from datetime import datetime
    summary = f"🎉 批量修復完成！成功處理了 {len(repairs_executed)} 組標籤，影響了 {processed_files_count} 個檔案。"
    if learned_count > 0:
        summary += f" (外加從 AI 學習了 {learned_count} 個標籤對照)"
    
    print(f"\n{summary}")
    
    report_lines = [
        "---",
        "title: \"標籤批量修復結果\"",
        "type: report",
        f"date_created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "---",
        "",
        "# ✅ 標籤批量修復程序報告",
        "",
        f"> {summary}",
        "",
        "### 📝 修復明細",
        ""
    ]
    
    for item in repairs_executed:
        report_lines.append(f"- [{item['reason']}] `{item['bad']}` → `{item['good']}` (影響 {item['count']} 個檔案)")
    
    report_content = "\n".join(report_lines)
    report_filename = f"✅sys-tag-bulk-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
    report_path = FROM_LLM_DIR / report_filename
    report_path.write_text(report_content, encoding='utf-8')
    logging.info(f"TagRepair: Detailed report written to {report_path.name}")

if __name__ == "__main__":
    import sys
    override = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    repair_tags_interactively(override)
