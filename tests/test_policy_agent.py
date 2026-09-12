import importlib.util
import json
import sys
from pathlib import Path

import pytest
from dialt import DialtMode

from dialt_recipes.cli import collect_cases

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "policy_agent"
EVALS = EXAMPLE / "evals"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"policy_agent_{name}", EXAMPLE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WORKFLOW = load("workflow")
HOST = load("host")
RULE_IDS = WORKFLOW.RULE_IDS


def documents(subset: str) -> list[dict]:
    files = sorted((EVALS / subset).glob("*.json"))
    return [json.loads(file.read_text()) for file in files]


def test_the_policy_is_valid_for_the_sdk_and_names_three_distinct_rules() -> None:
    assert len(set(RULE_IDS)) == len(RULE_IDS) == 3
    mode = DialtMode(**{key: value for key, value in WORKFLOW.session_mode().items()
                        if key != "kind"})
    assert mode.policy == WORKFLOW.POLICY
    actions = {rule["id"]: rule["action"] for rule in WORKFLOW.POLICY["rules"]}
    assert actions == {"emergency": "speak_now", "clinical_advice": "next_turn",
                       "complaint": "next_turn"}
    with pytest.raises(ValueError):
        DialtMode(policy={"rules": [{**WORKFLOW.POLICY["rules"][0], "action": "shout"}]})


@pytest.mark.parametrize("subset,count", [("full_call", 4), ("scripted", 12)])
def test_cases_carry_the_workflow_and_standard_hosted_policy_checks(subset, count) -> None:
    docs = documents(subset)
    assert len(docs) == count
    cases = collect_cases([EVALS / subset], modality="text")
    for document, case in zip(docs, cases):
        assert case.target_instructions == WORKFLOW.INSTRUCTIONS
        assert list(case.target_tools) == WORKFLOW.tool_manifest()
        assert case.target["policy"] == WORKFLOW.POLICY
        assert set(case.fixtures) == {tool["name"] for tool in case.target_tools}
        assert "policy" not in document
        checks = [c for c in case.checks if c["type"] == "policy_flag"]
        assert checks and all(c["value"] in RULE_IDS for c in checks)


def test_the_scripted_set_covers_every_rule_and_the_near_misses() -> None:
    docs = documents("scripted")
    expected = {c["value"] for document in docs for c in document["checks"]
                if c["type"] == "policy_flag" and c.get("min_count", 1) > 0}
    assert expected == set(RULE_IDS)
    quiet = [d for d in docs if all(c.get("min_count", 1) == 0 for c in d["checks"] if c["type"] == "policy_flag")]
    assert len(quiet) >= 4
    for document in docs:
        assert "Say exactly the following lines" in document["simulator"]["instructions"]
        assert "1. " in document["simulator"]["instructions"]
    assert sum(all(c.get("min_count", 1) == 0 for c in d["checks"] if c["type"] == "policy_flag")
               for d in documents("full_call")) == 1


def test_extended_controls_are_separate_and_sdk_validated():
    mode = DialtMode.from_wire(WORKFLOW.extended_mode())
    assert mode.policy["batch_guidance"] and mode.policy["recheck_corrections"]
    assert mode.policy["wait_for_check"] == ["reschedule_appointment"]
    assert "batch_guidance" not in WORKFLOW.POLICY
    cases = collect_cases([EVALS / "extended"], modality="text")
    assert len(cases) == 4
    assert all(any(c["type"] == "policy_flag" for c in case.checks) for case in cases)
