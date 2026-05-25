# Phase 4: Capability Layer + Lens Dual-Link — Implementation Plan

Compressed handoff for the executing agent. Decisions in this doc are settled; do not re-litigate unless something concrete blocks implementation.

## Scope

**In:**
1. `CapabilityManager` service that scans Operations + Skills, parses frontmatter, exposes lookup / resolve / validate stubs.
2. Add YAML frontmatter to all 7 capability files (2 Operations, 5 Skills).
3. `_build_system_prompt` returns `(prompt_str, resolution_record)`; 5 call sites updated to splice the record into `trace_context["metadata"]["capability_resolution"]`.
4. Strip YAML frontmatter from Operation / Skill bodies before they are concatenated into system prompts.
5. CounterAgent dual-link rendering for lens evidence (Obsidian wikilink + `file:///` URL with line-range fragment).
6. Tests for the above.

**Out (deferred — see [PipelineRunner_roadmap.md](PipelineRunner_roadmap.md)):**
- PipelineRunner.
- Pipeline DSL.
- `@ling-pipeline` intent.
- `validate_inputs` real implementation (ship as stub returning `(True, [])`).
- Renaming `montecarlo.md` → `montecarlo.md` (separate PR; just log a warning in CapabilityManager).

## Critical Ordering

> **Must happen in this order** to avoid poisoning system prompts:
>
> 1. Add a `_strip_frontmatter()` helper, or extend `_load_localized_content`, to remove YAML before returning the body. Cover Operations + Skills paths.
> 2. *Then* add frontmatter to the 7 capability files.
> 3. *Then* add CapabilityManager and the `_build_system_prompt` hook.

Currently [`_load_localized_content`](../services/llm_client.py:326) returns the whole file as system-prompt content. Adding frontmatter to `Operations/synthesize.md` without step 1 would inject the YAML block into every synthesis LLM call.

## Files to Add / Modify

### New
- `System_Engine/services/capability_manager.py`
- `System_Engine/tests/test_capability_manager.py`

### Modified
- `System_Engine/services/llm_client.py`
  - `_load_localized_content` (or new helper) strips frontmatter for Operations/Skills paths.
  - `__init__` instantiates `self.capability_manager`.
  - `_build_system_prompt` returns `(str, dict)`; calls `capability_manager.resolve(...)`.
  - 5 call sites updated: lines [450](../services/llm_client.py:450), [510](../services/llm_client.py:510), [773](../services/llm_client.py:773), [820](../services/llm_client.py:820), [generate_entity_page](../services/llm_client.py:441). Each splices resolution into `trace_context["metadata"]["capability_resolution"]`.
- `System_Engine/agents/counter_agent.py`
  - `_ground_tally_locations` resolves absolute physical path for each Part.
  - `_reference_cell` / `_format_instance` emit dual links.
- `lings-desktop/Templates/Operations/synthesize.md` — add frontmatter.
- `lings-desktop/Templates/Operations/critique.md` — add frontmatter.
- `lings-desktop/Skills/{islands,meta-methods,montecarlo,recency,tag-cluster}.md` — append capability fields to existing frontmatter.
- `README.md` — short Lens dual-link caveat (snippet below).
- `System_Engine/tests/test_counter_agent.py` — assert dual-link format in output.

## Frontmatter Schemas

### Operations (new — flat)

```yaml
---
type: operation
description: <one-line>
expected_inputs:
  - <input_key>
expected_context:
  - <optional_key>
produces:
  - <output_key>
cost_class: low | medium | high
methodology: fixed
---
```

### Skills (existing fields preserved; new fields appended flat)

```yaml
---
# existing — do NOT touch:
description: ...
limit: ...
method: ...
# (plus pipeline / num_rounds / num_sparks / top_k where present)

# new:
type: skill
expected_inputs:
  - user_directive
expected_context:
  - <optional_key>
produces:
  - insight_report
cost_class: low | medium | high
applicable_when:
  <key>: <value>
---
```

**Rules:**
- Canonical id = file stem. Ignore any `name:` field in frontmatter.
- `cost_class`: token estimate — `low` < 2k, `medium` 2–10k, `high` > 10k input tokens for the operation's typical call.
- Unknown / missing fields: default to empty list / `unknown` / `{}`. Never raise on parse failure — log warning and store an empty spec.

## CapabilityManager Interface

```python
# services/capability_manager.py

@dataclass(frozen=True)
class CapabilitySpec:
    name: str                                  # file stem
    type: str                                  # "operation" | "skill"
    source_path: Path
    description: str = ""
    expected_inputs: tuple[str, ...] = ()
    expected_context: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    cost_class: str = "unknown"
    applicable_when: dict = field(default_factory=dict)
    raw_frontmatter: dict = field(default_factory=dict)

class CapabilityManager:
    def __init__(self, operations_dir: Path, skills_dir: Path): ...
    def get(self, name: str) -> CapabilitySpec | None: ...
    def all(self) -> list[CapabilitySpec]: ...
    def resolve(self, *, persona: str | None,
                operation: str | None,
                template: str | None) -> dict: ...
    def validate_inputs(self, name: str,
                        available: set[str]) -> tuple[bool, list[str]]:
        # Phase 4 stub:
        spec = self.get(name)
        if not spec: return (False, [f"capability '{name}' not found"])
        return (True, [])  # real check deferred to Phase 4.5
```

`resolve(...)` returns:

```python
{
    "operation": {"name": ..., "found": bool, "cost_class": ..., "produces": [...], "source": "..."} | None,
    "persona":   {"name": ..., "found": bool, "source": "..."} | None,
    "template":  {"name": ..., "found": bool} | None,
}
```

## `_build_system_prompt` Hook

```python
def _build_system_prompt(self, ...) -> tuple[str, dict]:
    # ... existing load logic (now via frontmatter-stripped helper) ...
    resolution = self.capability_manager.resolve(
        persona=persona if persona != "none" else None,
        operation=operation if operation and operation != "none" else None,
        template=template_name if forced_template != "none" else None,
    )
    return prompt_str, resolution
```

All 5 callers:

```python
system_prompt, cap_resolution = self._build_system_prompt(...)
self._complete_text(
    system_prompt, user_msg,
    trace_context={
        "stage": "...",
        ...,
        "metadata": {
            ...existing keys...,
            "capability_resolution": cap_resolution,
        },
    },
)
```

No `trace_store.py` schema changes — the resolution lands in the existing `llm_calls.metadata_json` blob.

## Lens Dual-Link

### counter_agent.py

Construct both link forms wherever evidence is rendered:

```
[[<PartTitle>#<anchor>|🔗 分析錨點]]   [📄 原始檔 L<start>-L<end>](file:///<abs_path>#L<start>-L<end>)
```

Path resolution: read Part's `source_start_line` / `source_end_line` from frontmatter (already populated by [ingestion_pipeline.py:618](../services/ingestion_pipeline.py:618)). The physical path is the Part file itself in `pages/`.

Place this comment next to the `file:///` link construction:

```python
# file:/// fragment (#L<start>-L<end>) 只在 VS Code-family editor 生效；
# Obsidian / system open 會 ignore line range。詳見 README "Lens dual-link"。
```

### README

Insert under the Phase 4 dated Refactor Notes entry, or near `@ling-lens` in 指令一覽:

> **Lens dual-link 注意事項**：lens 報告的 evidence 同時輸出 Obsidian wikilink 與 `file:///` 連結。`file:///` 連結帶有 `#L<start>-L<end>` fragment——只有 VS Code / Cursor 系列編輯器會跳到指定行，Obsidian 點擊與系統 `open` 會開檔但忽略行號。Wikilink 走 Obsidian 原生導航，永遠可用。

## Tests

- `tests/test_capability_manager.py`
  - scans both dirs, finds 2 operations + 5 skills
  - file stem becomes canonical id (verify `montecarlo` registers as `montecarlo`, no auto-rename)
  - missing frontmatter → empty `CapabilitySpec`, no raise
  - malformed YAML → warning logged, empty spec, no raise
  - `validate_inputs(unknown)` → `(False, [...])`
  - `resolve()` returns the correct shape for all three axes
- `tests/test_counter_agent.py` — add assertion that lens output contains both the wikilink form and a `file:///...#L<n>-L<n>` URL.
- Existing tests must continue to pass — focus on the call-site updates around `_build_system_prompt` (5 sites) so mocks return tuples not strings.

## Decisions Already Settled (do not re-open)

| Question | Decision |
|---|---|
| Skill / Operation id source | File stem; ignore frontmatter `name:` |
| `montecarlo` filename typo | Log warning; rename in separate PR |
| Frontmatter parse failure | Graceful: log warning + empty spec |
| `cost_class` levels | low < 2k, medium 2–10k, high > 10k input tokens |
| Capability metadata in system prompt | **No** — trace metadata only |
| `_build_system_prompt` signature | Returns `(str, dict)` |
| Feature flag for capability trace | Not needed for v1 — cost is parse-once-cached |
| PipelineRunner / DSL | Deferred — see [roadmap](PipelineRunner_roadmap.md) |
| Lens `file:///` fragment caveat | Short README note + short code comment |

## Verification Before Merge

1. `PYTHONPATH=System_Engine venv/bin/python -m pytest -q System_Engine/tests` — all green.
2. Manually trigger an ingestion of any markdown into `Consolidate/`. Confirm:
   - Synthesis runs without YAML appearing inside the generated body (proves frontmatter strip works).
   - `llm_trace.sqlite` `llm_calls.metadata_json` for that run contains `capability_resolution` with non-null operation entry.
3. Manually run `@ling-lens` against a Part-backed article. Inspect the output report:
   - Contains both wikilink and `file:///` form.
   - Click the `file:///` link in VS Code / Cursor — jumps to correct line range.
   - Click the wikilink in Obsidian — opens correct Part anchor.
