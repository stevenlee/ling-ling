"""C3: architecture_scan — deterministic ast facts from a packed note."""

from services.architecture_scan import format_facts, scan_architecture

_PACKED = """---
type: packed-code
source_paths:
  - System_Engine/services/foo.py
  - System_Engine/agents/bar.py
identifiers:
  - Foo
  - helper
---

# Packed

## System_Engine/services/foo.py

```python
import re
from agents.bar import Bar


class Foo:
    def method(self):
        return 1


def helper():
    return 2
```

## System_Engine/agents/bar.py

```python
from difflib import SequenceMatcher


class Bar:
    pass
```
"""


def test_extracts_modules_classes_functions():
    scan = scan_architecture(_PACKED)
    mods = {m["path"]: m for m in scan["modules"]}
    foo = mods["System_Engine/services/foo.py"]
    assert foo["classes"] == ["Foo"]
    assert foo["functions"] == ["helper"]  # top-level only; method() excluded


def test_classifies_internal_vs_external_imports():
    scan = scan_architecture(_PACKED)
    assert set(scan["internal_roots"]) == {"services", "agents"}
    foo = next(m for m in scan["modules"] if m["path"].endswith("foo.py"))
    assert "agents.bar" in foo["imports_internal"]  # agents is a packed root
    assert "re" in foo["imports_external"]
    bar = next(m for m in scan["modules"] if m["path"].endswith("bar.py"))
    assert "difflib" in bar["imports_external"]


def test_hash_heading_inside_fence_does_not_drop_module():
    # REGRESSION (review fix): a top-column `## note` inside the fence severed
    # the section, its closing fence vanished, and the module silently
    # disappeared from the facts.
    text = (
        "---\ntype: packed-code\nsource_paths:\n  - pkg/mod.py\n---\n\n"
        "## pkg/mod.py\n\n```python\n## top-column note\nclass Keep:\n    pass\n```\n"
    )
    scan = scan_architecture(text)
    assert [m["path"] for m in scan["modules"]] == ["pkg/mod.py"]
    assert scan["modules"][0]["classes"] == ["Keep"]


def test_syntax_error_section_flagged_not_dropped():
    text = "## x.py\n\n```python\ndef (:\n```\n"
    scan = scan_architecture(text)
    assert scan["modules"][0]["parse_error"] is True


def test_format_facts_lists_structure():
    facts = format_facts(scan_architecture(_PACKED))
    assert "classes: Foo" in facts
    assert "內部依賴: agents.bar" in facts
    assert "外部依賴: re" in facts
