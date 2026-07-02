"""Batch-2 T2: Compare/Classify/Outline/Explain operations register from the real vault dir."""

import pytest

from core.config import OPERATIONS_DIR, SKILLS_DIR
from services.capability_manager import CapabilityManager


NEW_OPERATIONS = {
    "compare": "medium",
    "classify": "low",
    "outline": "low",
    "explain": "medium",
}

EXISTING_OPERATIONS = [
    "answer_from_sources",
    "critique",
    "digest_sources",
    "load_sources",
    "plan",
    "refute",
    "synthesize",
]


@pytest.fixture(scope="module")
def manager():
    return CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)


@pytest.mark.parametrize("name,cost_class", sorted(NEW_OPERATIONS.items()))
def test_new_operation_registered(manager, name, cost_class):
    spec = manager.get(name)
    assert spec is not None and spec.found, f"{name} not picked up from {OPERATIONS_DIR}"
    assert spec.type == "operation"
    assert spec.cost_class == cost_class
    assert spec.expected_inputs, f"{name} has no expected_inputs"
    assert spec.produces, f"{name} has no produces"
    assert spec.description


@pytest.mark.parametrize("name", EXISTING_OPERATIONS)
def test_existing_operations_still_registered(manager, name):
    spec = manager.get(name)
    assert spec is not None and spec.found, f"pre-existing operation {name} went missing"
    assert spec.type == "operation"
