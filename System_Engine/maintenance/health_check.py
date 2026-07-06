import sys
from pathlib import Path

# Add System_Engine to sys.path
sys.path.append(str(Path(__file__).parent.parent.absolute()))


def check_structure():
    print("🔍 Checking Project Structure...")
    from core.config import ensure_directories, PROJECT_ROOT, PROMPTS_DIR

    ensure_directories()
    print(f"✅ Directories verified at {PROJECT_ROOT}")

    # Keep in sync with tests/test_prompt_assets.py::test_required_agent_prompts_exist
    # (that test is the enforced gate; this is the human-facing health readout).
    required_prompts = [
        "system_base.md",
        "mermaid_rules.md",
        "agent_counter.md",
        "agent_insight.md",
        "agent_linter.md",
        "agent_merge.md",
        "agent_recall.md",
        "agent_tag_patrol.md",
    ]
    for p in required_prompts:
        if (PROMPTS_DIR / p).exists():
            print(f"✅ Prompt template found: {p}")
        else:
            print(f"💧 Prompt template MISSING: {p}")


def check_agents():
    print("\n🔍  Auditing Agent Refactor...")
    errors = 0

    from services.llm_client import LLMClient

    LLMClient()

    agents_to_check = [
        ("MergeAgent", "agents.merge_agent"),
        ("TagPatrolAgent", "agents.tag_patrol_agent"),
        ("InsightAgent", "agents.insight_agent"),
        ("LinterAgent", "agents.linter_agent"),
    ]

    for name, module_path in agents_to_check:
        try:
            module = __import__(module_path, fromlist=[name])
            agent_class = getattr(module, name)

            # Check for BaseAgent inheritance (indirectly via __init__ signature)
            import inspect

            sig = inspect.signature(agent_class.__init__)
            params = list(sig.parameters.keys())

            # Expected params: self, llm, rag (or similar)
            if "llm" in params or "rag_manager" in params:
                print(f"✅ {name}: Initialization signature looks correct.")
            else:
                print(f"💧 {name}: Unexpected __init__ signature: {params}")
                errors += 1

            # Check for execute method
            if hasattr(agent_class, "execute"):
                print(f"✅ {name}: execute() method found.")
            else:
                print(f"💧 {name}: execute() method MISSING!")
                errors += 1

        except Exception as e:
            print(f"💧 {name}: Failed to import/init: {e}")
            errors += 1

    return errors


def check_parser():
    print("\n🧪 Testing Parser Utility (Safe Unwrapping)...")
    try:
        from core.parser import clean_llm_response

        test_input = "```markdown\n# Hello\n```"
        cleaned = clean_llm_response(test_input)
        if cleaned == "# Hello":
            print("✅ Parser: clean_llm_response works correctly.")
            return 0
        else:
            print(f"💧 Parser: clean_llm_response failed. Got: {repr(cleaned)}")
            return 1
    except Exception as e:
        print(f"💧 Parser: Test failed: {e}")
        return 1


def main():
    print("=" * 50)
    print("🌿  LING-LING SYSTEM HEALTH CHECK (Refactored)")
    print("=" * 50)

    errors = 0
    check_structure()
    errors += check_agents()
    errors += check_parser()

    print("\n" + "=" * 50)
    if errors == 0:
        print("🎉 [PASSED] System is healthy and following the new Agent pattern!")
    else:
        print(f"💧 [FAILED] Found {errors} issues. Please fix them.")
    print("=" * 50)

    sys.exit(errors)


if __name__ == "__main__":
    main()
