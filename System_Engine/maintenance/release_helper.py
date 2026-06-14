import os
import ast
import sys
import json
from pathlib import Path

# Add System_Engine to path for imports
sys.path.append(str(Path(__file__).parent.parent.absolute()))

try:
    from core.config import PROJECT_ROOT
    from core.version import BUILD_DATE
except ImportError:
    print("💧 Error: Could not import core configuration. Run this script from the project root.")
    sys.exit(1)

class CodeAuditor:
    def __init__(self, target_dir: Path):
        self.target_dir = target_dir
        self.files_audited = 0
        self.functions_found = 0
        self.missing_docstrings = []
        self.audit_data = {}

    def scan(self):
        """Scan all .py files in the target directory."""
        for root, dirs, files in os.walk(self.target_dir):
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
            if "venv" in dirs:
                dirs.remove("venv")
                
            for file in files:
                if file.endswith(".py"):
                    self.audit_file(Path(root) / file)

    def audit_file(self, filepath: Path):
        """Analyze a single Python file using AST."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            
            tree = ast.parse(content)
            self.files_audited += 1
            
            rel_path = filepath.relative_to(self.target_dir)
            self.audit_data[str(rel_path)] = {
                "classes": [],
                "functions": []
            }

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    self.functions_found += 1
                    docstring = ast.get_docstring(node)
                    func_info = {
                        "name": node.name,
                        "args": [arg.arg for arg in node.args.args],
                        "has_docstring": docstring is not None,
                        "line_count": node.end_lineno - node.lineno if hasattr(node, 'end_lineno') else 0
                    }
                    self.audit_data[str(rel_path)]["functions"].append(func_info)
                    
                    if not docstring:
                        self.missing_docstrings.append(f"{rel_path}:{node.lineno} - {node.name}")

                elif isinstance(node, ast.ClassDef):
                    self.audit_data[str(rel_path)]["classes"].append(node.name)

        except Exception as e:
            print(f"💦 Error auditing {filepath}: {e}")

    def generate_console_report(self):
        """Print a summary to the console."""
        print("\n" + "="*50)
        print(f"🔍 LING-LING CODE AUDIT REPORT (build {BUILD_DATE})")
        print("="*50)
        print(f"📁 Files Audited:    {self.files_audited}")
        print(f"functions Found:     {self.functions_found}")
        print(f"Missing Docstrings: {len(self.missing_docstrings)}")
        print("-" * 50)
        
        if self.missing_docstrings:
            print("\n💦  FUNCTIONS MISSING DOCSTRINGS:")
            for item in self.missing_docstrings[:20]:  # Limit to top 20
                print(f"  - {item}")
            if len(self.missing_docstrings) > 20:
                print(f"  ... and {len(self.missing_docstrings) - 20} more.")
        else:
            print("\n✅ All functions have docstrings! Excellent work.")
        print("="*50 + "\n")

    def get_summary_prompt(self):
        """Prepare a prompt for the LLM to summarize the project."""
        summary = "I have audited the Ling-Ling project codebase. Here is the structure:\n"
        for file, data in self.audit_data.items():
            if data["functions"] or data["classes"]:
                summary += f"\n- {file}:\n"
                if data["classes"]:
                    summary += f"  Classes: {', '.join(data['classes'])}\n"
                if data["functions"]:
                    summary += f"  Functions: {', '.join([f['name'] for f in data['functions']])}\n"
        
        prompt = f"""You are a professional software architect. Based on the following codebase analysis, write a high-level **Release Note** for the project "Ling-Ling" (build {BUILD_DATE}).

**Codebase Context:**
{summary}

**Instructions:**
1. Include the build date "{BUILD_DATE}" prominently in the title or header.
2. Write a compelling project description (what is Ling-Ling?).
3. Highlight the key capabilities based on the module names (e.g., agents, watchers, core services).
4. List "What's New" or "Features" in a clean Markdown format.
5. Keep the tone professional but exciting.
6. Do NOT mention specific missing docstrings or minor code issues in the release note.

Please output the Release Note in Markdown.
"""
        return prompt

def main():
    engine_dir = Path(__file__).parent.parent.absolute()
    auditor = CodeAuditor(engine_dir)
    
    print("🚀 Starting codebase audit...")
    auditor.scan()
    auditor.generate_console_report()
    
    print("🎀 Initializing LLM Client...")
    try:
        # Import inside main to prevent import errors during audit stage
        from services.llm_client import LLMClient
        
        llm = LLMClient()
        print(f"🎀 LLM Client Initialized ({llm.provider}). Sending prompt...")
        prompt = auditor.get_summary_prompt()
        release_note = llm.answer_query(prompt, wiki_context="")
        
        # Prepend the build date to ensure it's at the very beginning
        final_content = f"# Release Notes - build {BUILD_DATE}\n\n{release_note}"
        
        output_path = PROJECT_ROOT / "RELEASE_NOTE.md"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        
        print(f"✅ Release Note generated at: {output_path}")
    except ImportError as e:
        print(f"\n💧 Dependency Error: {e}")
        print("\n💡 Fix: It looks like some dependencies are missing in your current Python environment.")
        print(f"Please run the script using your virtual environment:\n")
        print(f"   {PROJECT_ROOT}/venv/bin/python {__file__}")
        print("\nAlternatively, activate your venv first: source venv/bin/activate")
    except Exception as e:
        print(f"💧 Failed to generate Release Note: {e}")

if __name__ == "__main__":
    main()
