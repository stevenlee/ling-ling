"""Phase 4: Capability Metadata Layer.

Scans `Templates/Operations/` and `Skills/` for capability frontmatter,
exposes a registry that LLMClient can query while building system prompts.
The resolution record lands in `trace_context.metadata.capability_resolution`
so each LLM call records which capability spec it actually invoked — no
capability metadata is injected into the system prompt itself.

Design decisions (from DesignDoc/Phase4_CapabilityLayer_implementation_plan.md):

- File stem is the canonical capability id. Frontmatter `name:` is read into
  `raw_frontmatter` for backward compatibility (InsightAgent still keys
  strategies by `yaml_data["name"]`) but ignored as id source.
- Parse failures are graceful: log a warning, return an empty CapabilitySpec.
- `validate_inputs` is a Phase 4 stub — real schema check lands later when
  the PipelineRunner needs it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_VALID_COST_CLASSES = frozenset({"low", "medium", "high", "unknown"})


@dataclass(frozen=True)
class CapabilitySpec:
    """Parsed capability metadata. `name` is always the file stem."""

    name: str
    type: str                                       # "operation" | "skill"
    source_path: Path
    description: str = ""
    expected_inputs: tuple[str, ...] = ()
    expected_context: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    cost_class: str = "unknown"
    applicable_when: dict = field(default_factory=dict)
    raw_frontmatter: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.type)

    def to_trace_record(self) -> dict:
        return {
            "name": self.name,
            "found": True,
            "type": self.type,
            "cost_class": self.cost_class,
            "produces": list(self.produces),
            "source": str(self.source_path),
        }


def _as_str_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return ()


def _normalize_cost_class(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    v = value.strip().lower()
    return v if v in _VALID_COST_CLASSES else "unknown"


def _parse_capability_file(path: Path, fallback_type: str) -> CapabilitySpec:
    """Read frontmatter for one capability file. Never raises."""
    name = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logging.warning(f"CapabilityManager: cannot read {path.name}: {e}")
        return CapabilitySpec(name=name, type=fallback_type, source_path=path)

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logging.debug(f"CapabilityManager: no frontmatter in {path.name}")
        return CapabilitySpec(name=name, type=fallback_type, source_path=path)

    try:
        data = yaml.safe_load(match.group(1)) or {}
    except Exception as e:
        logging.warning(f"CapabilityManager: bad YAML in {path.name}: {e}")
        return CapabilitySpec(name=name, type=fallback_type, source_path=path)

    if not isinstance(data, dict):
        logging.warning(f"CapabilityManager: frontmatter in {path.name} is not a mapping")
        return CapabilitySpec(name=name, type=fallback_type, source_path=path)

    declared_type = data.get("type")
    if not isinstance(declared_type, str) or not declared_type:
        declared_type = fallback_type

    applicable_when = data.get("applicable_when") or {}
    if not isinstance(applicable_when, dict):
        applicable_when = {}

    return CapabilitySpec(
        name=name,
        type=declared_type,
        source_path=path,
        description=str(data.get("description") or "").strip(),
        expected_inputs=_as_str_tuple(data.get("expected_inputs")),
        expected_context=_as_str_tuple(data.get("expected_context")),
        produces=_as_str_tuple(data.get("produces")),
        cost_class=_normalize_cost_class(data.get("cost_class")),
        applicable_when=applicable_when,
        raw_frontmatter=data,
    )


class CapabilityManager:
    """Registry of Operation + Skill capability specs.

    Scan happens once at construction. Cheap to rebuild on demand; the
    daemon constructs one CapabilityManager per LLMClient.
    """

    def __init__(self, operations_dir: Path, skills_dir: Path):
        self.operations_dir = Path(operations_dir)
        self.skills_dir = Path(skills_dir)
        self._specs: dict[str, CapabilitySpec] = {}
        self.reload()

    def reload(self) -> None:
        self._specs = {}
        self._scan_dir(self.operations_dir, fallback_type="operation")
        self._scan_dir(self.skills_dir, fallback_type="skill")
        self._warn_on_known_typos()

    def _scan_dir(self, directory: Path, fallback_type: str) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.md")):
            # Skip localized variants (foo.zh.md, foo.ja.md). They share the
            # canonical id with their base file; we only register the base.
            stem = path.stem
            if "." in stem:
                continue
            spec = _parse_capability_file(path, fallback_type=fallback_type)
            self._specs[spec.name] = spec

    def _warn_on_known_typos(self) -> None:
        # Skill file `montecario.md` exists but the canonical pipeline name
        # used in InsightAgent and the synthesis prompt is "montecarlo".
        # File rename is a separate PR; surface it here so it doesn't
        # silently survive future grep-by-name lookups.
        if "montecario" in self._specs and "montecarlo" not in self._specs:
            logging.warning(
                "CapabilityManager: capability id 'montecario' looks like a "
                "typo of 'montecarlo' (pipeline name in InsightAgent). "
                "Rename the file in a follow-up PR."
            )

    def get(self, name: str | None) -> CapabilitySpec | None:
        if not name:
            return None
        return self._specs.get(name)

    def all(self) -> list[CapabilitySpec]:
        return list(self._specs.values())

    def resolve(
        self,
        *,
        persona: str | None = None,
        operation: str | None = None,
        template: str | None = None,
    ) -> dict:
        """Build a resolution record for the current LLM call.

        Stored in `llm_calls.metadata_json` under `capability_resolution`.
        Never injected into the system prompt.
        """
        record: dict = {}

        if operation and operation != "none":
            spec = self.get(operation)
            record["operation"] = (
                spec.to_trace_record() if spec else {"name": operation, "found": False}
            )
        else:
            record["operation"] = None

        if persona and persona != "none":
            # Personas aren't capability-registered (yet); record only what
            # the caller asked for so the trace stays complete.
            record["persona"] = {"name": persona, "found": False, "registered": False}
        else:
            record["persona"] = None

        if template and template != "none":
            record["template"] = {"name": template, "found": False, "registered": False}
        else:
            record["template"] = None

        return record

    def validate_inputs(
        self, name: str, available: set[str] | None = None
    ) -> tuple[bool, list[str]]:
        """Phase 4 stub. Real schema check lands with PipelineRunner."""
        spec = self.get(name)
        if not spec:
            return False, [f"capability '{name}' not found"]
        # available=None means "caller hasn't enumerated inputs yet" — accept.
        if available is None:
            return True, []
        missing = [k for k in spec.expected_inputs if k not in available]
        return (not missing), missing
