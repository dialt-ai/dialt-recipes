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
def test_cases_carry_the_workflow_its_policy_and_a_policy_block(subset, count) -> None:
    docs = documents(subset)
    assert len(docs) == count
    cases = collect_cases([EVALS / subset], modality="text")
    for document, case in zip(docs, cases):
        assert case.target_instructions == WORKFLOW.INSTRUCTIONS
        assert list(case.target_tools) == WORKFLOW.tool_manifest()
        assert case.target["policy"] == WORKFLOW.POLICY
        assert set(case.fixtures) == {tool["name"] for tool in case.target_tools}
        policy = document["policy"]
        assert set(policy) == {"expect", "forbid"}
        assert set(policy["expect"]) <= set(RULE_IDS) and set(policy["forbid"]) <= set(RULE_IDS)
        assert not set(policy["expect"]) & set(policy["forbid"])
        assert set(policy["expect"]) | set(policy["forbid"]) or policy["expect"]


def test_the_scripted_set_covers_every_rule_and_the_near_misses() -> None:
    docs = documents("scripted")
    expected = {rule for document in docs for rule in document["policy"]["expect"]}
    assert expected == set(RULE_IDS)
    quiet = [document for document in docs if not document["policy"]["expect"]]
    assert len(quiet) >= 4
    for document in docs:
        assert "Say exactly the following lines" in document["simulator"]["instructions"]
        assert "1. " in document["simulator"]["instructions"]
    assert [d["policy"]["expect"] for d in documents("full_call")].count([]) == 1


def test_host_turns_flags_into_checks_and_a_score() -> None:
    events = [
        {"side": "simulator", "type": "policy_flag", "rule": "complaint", "delivered": True},
        {"side": "target", "type": "asr", "text": "hello"},
        {"side": "target", "type": "policy_flag", "rule": "emergency", "action": "speak_now",
         "evidence": "chest pain", "delivered": True, "reply_started": True, "t_ms": 1200},
        {"side": "target", "type": "policy_flag", "rule": "clinical_advice", "action": "next_turn",
         "evidence": "dose", "delivered": False, "reply_started": False, "t_ms": 3000},
    ]
    flags = HOST.flags_from(events)
    assert [flag["rule"] for flag in flags] == ["emergency", "clinical_advice"]
    assert "side" not in flags[0] and flags[0]["t_ms"] == 1200
    checks = HOST.policy_checks({"expect": ["emergency", "clinical_advice", "complaint"],
                                 "forbid": []}, flags)
    assert [(check["pass"], check["detail"]) for check in checks] == [
        (True, ""), (False, "raised, not delivered"), (False, "not raised")]
    assert HOST.policy_checks({"expect": [], "forbid": ["emergency"]}, flags) == [
        {"type": "policy_flag", "name": "emergency did not fire", "pass": False, "detail": "raised"}]

    summaries = [
        {"passed": True, "flags": flags, "policy": {"expect": ["emergency"], "forbid": ["complaint"]}},
        {"passed": False, "flags": [], "policy": {"expect": ["complaint"], "forbid": []}},
        {"passed": True, "flags": [{"rule": "complaint"}], "policy": {"expect": [], "forbid": RULE_IDS}},
    ]
    assert HOST.score(summaries) == {
        "cases": 3, "passed": 2, "true_positives": 1, "false_positives": 1, "missed": 1,
        "precision": 0.5, "recall": 0.5}
    assert HOST.score([])["precision"] == 1.0
