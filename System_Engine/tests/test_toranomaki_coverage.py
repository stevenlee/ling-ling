"""Drift guard: the toranomaki playbook must stay in sync with the router.

toranomaki/@ling-*.md is a third command surface (alongside command_specs and
INTENT_ROUTES) that nothing else checks — which is exactly how `review`/`blog`
once shipped undocumented. These tests fail when a routable command has no
playbook doc, or a doc points at no route.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from core.config import WIKI_VAULT_DIR
from watchers.prompt_watcher import INTENT_ROUTES

TORANOMAKI = WIKI_VAULT_DIR / "toranomaki"

# Routes that intentionally ship without a user-facing playbook doc. Empty today;
# add a primary trigger here (with a reason) if an internal-only command is added.
_DOC_EXEMPT_TRIGGERS: frozenset[str] = frozenset()

# toranomaki docs that legitimately have no matching route (none today). Add a
# filename token here if a non-command reference doc is introduced.
_ROUTELESS_DOC_EXEMPT: frozenset[str] = frozenset()


def _doc_stems() -> set[str]:
    return {p.stem.replace("@ling-", "").lower() for p in TORANOMAKI.glob("@ling-*.md")}


def _all_triggers() -> set[str]:
    """Every filename trigger across all routes (primaries + aliases), lowered."""
    return {t.lower() for triggers, _slash, _intent in INTENT_ROUTES for t in triggers}


def test_every_routable_command_has_a_doc():
    docs = _doc_stems()
    missing = [
        f"@ling-{triggers[0]}.md (intent={intent})"
        for triggers, _slash, intent in INTENT_ROUTES
        if triggers[0].lower() not in docs and triggers[0].lower() not in _DOC_EXEMPT_TRIGGERS
    ]
    assert not missing, (
        "toranomaki is missing playbook docs for routable commands: "
        + ", ".join(missing)
        + ". Write the doc, or add the trigger to _DOC_EXEMPT_TRIGGERS if it's internal-only."
    )


def test_no_routeless_docs():
    triggers = _all_triggers()
    orphans = [
        f"@ling-{d}.md" for d in _doc_stems()
        if d not in triggers and d not in _ROUTELESS_DOC_EXEMPT
    ]
    assert not orphans, (
        "toranomaki has docs that match no route (stale after a command rename/removal?): "
        + ", ".join(orphans)
        + ". Remove the doc, or add it to _ROUTELESS_DOC_EXEMPT if it's a reference doc."
    )
