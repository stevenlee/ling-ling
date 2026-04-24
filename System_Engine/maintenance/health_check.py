import sys
import os
import inspect
from pathlib import Path

# Add System_Engine to sys.path
sys.path.append(str(Path(__file__).parent.parent.absolute()))

def check_structure():
    print("🔍 Checking Project Structure...")
    from core.config import ensure_directories, PROJECT_ROOT
    ensure_directories()
    print(f"✅ Directories verified at {PROJECT_ROOT}")

def check_agents():
    print("\n🕵️  Auditing Agent Method Mappings...")
    errors = 0
    
    # 1. MergeAgent check
    try:
        from agents.merge_agent import MergeAgent
        merger = MergeAgent(Path("."))
        if hasattr(merger, 'merge_entities'):
            print("✅ MergeAgent: merge_entities() found.")
        else:
            print("❌ MergeAgent: merge_entities() MISSING!")
            errors += 1
    except Exception as e:
        print(f"❌ MergeAgent: Failed to import/init: {e}")
        errors += 1

    # 2. TagPatrolAgent check
    try:
        from agents.tag_patrol_agent import TagPatrolAgent
        agent = TagPatrolAgent()
        if hasattr(agent, 'generate_report'):
            print("✅ TagPatrolAgent: generate_report() found.")
        else:
            print("❌ TagPatrolAgent: generate_report() MISSING!")
            errors += 1
    except Exception as e:
        print(f"❌ TagPatrolAgent: Failed to import/init: {e}")
        errors += 1

    # 3. InsightAgent check
    try:
        from agents.insight_agent import InsightAgent
        # We need dummy LLM/RAG for init if they are required
        agent = InsightAgent(Path("."), None, None)
        if hasattr(agent, 'generate_insight'):
            print("✅ InsightAgent: generate_insight() found.")
        else:
            print("❌ InsightAgent: generate_insight() MISSING!")
            errors += 1
    except Exception as e:
        print(f"❌ InsightAgent: Failed to import/init: {e}")
        errors += 1

    # 4. WikiLinter check
    try:
        from maintenance.wiki_linter import WikiLinter
        linter = WikiLinter(Path("."))
        if hasattr(linter, 'perform_repair') and hasattr(linter, 'generate_report'):
            print("✅ WikiLinter: Methods found.")
        else:
            print("❌ WikiLinter: Methods MISSING!")
            errors += 1
    except Exception as e:
        print(f"❌ WikiLinter: Failed to import/init: {e}")
        errors += 1

    return errors

def check_parser():
    print("\n🧪 Testing Parser Utility...")
    try:
        from core.parser import dump_markdown_with_metadata, parse_markdown_metadata
        test_meta = {"title": "Test", "tags": ["a", "b"]}
        test_content = "Hello World"
        md = dump_markdown_with_metadata(test_meta, test_content)
        parsed = parse_markdown_metadata(md)
        
        if "Test" in md and parsed.get("title") == "Test":
            print("✅ Parser: dump/parse cycle successful.")
            return 0
        else:
            print("❌ Parser: Data mismatch during cycle.")
            return 1
    except Exception as e:
        print(f"❌ Parser: Test failed: {e}")
        return 1

def main():
    print("="*50)
    print("🛡️  LING-LING SYSTEM HEALTH CHECK")
    print("="*50)
    
    errors = 0
    errors += check_structure() or 0
    errors += check_agents()
    errors += check_parser()
    
    print("\n" + "="*50)
    if errors == 0:
        print("🎉 [PASSED] System is healthy and ready for release!")
    else:
        print(f"🚩 [FAILED] Found {errors} issues. Please fix them before release.")
    print("="*50)
    
    sys.exit(errors)

if __name__ == "__main__":
    main()
