import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from dialt_recipes.cli import collect_cases


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "policy_monitor"
EVALS = EXAMPLE / "evals"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"policy_monitor_{name}", EXAMPLE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


POLICY = load("policy")
WORKFLOW = load("workflow")
RULE_IDS = [rule.id for rule in POLICY.CLINIC_RULES]


class FakeLive:
    """A live session that refuses the first injection as the broker does mid-reply."""

    def __init__(self, refuse_first: bool = False) -> None:
        self.calls: list[dict] = []
        self.refuse_first = refuse_first

    async def inject_context(self, text, *, role, reply, message_id):
        self.calls.append({"text": text, "role": role, "reply": reply, "message_id": message_id})
        if self.refuse_first and len(self.calls) == 1:
            return {"accepted": False, "retryable": True, "detail": "a reply is in flight"}
        return {"accepted": True}


def test_rules_are_unique_and_the_policy_session_knows_each_one() -> None:
    assert len(set(RULE_IDS)) == len(RULE_IDS) == 3
    text = POLICY.monitor_instructions()
    for rule_id in RULE_IDS:
        assert f"- {rule_id}:" in text
    assert POLICY.raise_flag_tool()["parameters"]["properties"]["rule"]["enum"] == RULE_IDS
    mode = POLICY.monitor_mode()
    assert mode.modality == "text" and mode.greeting is False and mode.end_call is False


def test_a_flag_is_delivered_once_with_the_rule_action() -> None:
    monitor = POLICY.PolicyMonitor("wss://unused", "key", deliver_interval_s=0)
    live = FakeLive(refuse_first=True)
    monitor._live = live
    monitor.note("caller", "I have crushing chest pain")

    async def run():
        await monitor._raise({"rule": "emergency", "evidence": "chest pain"})
        await monitor._raise({"rule": "emergency", "evidence": "again"})   # once per call
        await monitor._raise({"rule": "clinical_advice", "evidence": "dose"})
        await monitor._raise({"rule": "nonsense", "evidence": "x"})         # unknown: ignored

    asyncio.run(run())
    assert [flag["rule"] for flag in monitor.flags] == ["emergency", "clinical_advice"]
    assert all(flag["delivered"] for flag in monitor.flags)
    assert monitor.flags[0]["after_line"] == 1
    assert [call["reply"] for call in live.calls] == [True, True, False]   # retried, then quiet
    assert live.calls[0]["message_id"] == live.calls[1]["message_id"]      # same idempotency key
    assert live.calls[0]["text"].startswith("[POLICY emergency] ")
    assert live.calls[2]["text"].startswith("[POLICY clinical_advice] ")


def test_note_normalises_and_skips_blank_lines() -> None:
    monitor = POLICY.PolicyMonitor("wss://unused", "key")
    monitor.note("caller", "  hello   there ")
    monitor.note("agent", "   ")
    assert monitor.lines == ["caller: hello there"]


def test_judge_cases_name_real_rules_and_lines() -> None:
    files = sorted((EVALS / "judge").glob("*.json"))
    assert len(files) >= 10
    expected, clean = 0, 0
    for file in files:
        document = json.loads(file.read_text())
        assert document["name"] and document["transcript"]
        assert all(who in {"caller", "agent"} and text for who, text in document["transcript"])
        expect = document["expect"]
        if expect is None:
            clean += 1
            continue
        expected += 1
        assert expect["rule"] in RULE_IDS
        assert 1 <= expect["line"] <= len(document["transcript"])
    assert expected >= 6 and clean >= 4
    assert {json.loads(f.read_text())["expect"]["rule"] for f in files
            if json.loads(f.read_text())["expect"]} == set(RULE_IDS)


def test_full_call_cases_carry_the_workflow_and_a_policy_block() -> None:
    documents = [json.loads(f.read_text()) for f in sorted((EVALS / "full_call").glob("*.json"))]
    assert len(documents) == 4
    cases = collect_cases([EVALS / "full_call"], modality="voice")
    for document, case in zip(documents, cases):
        assert case.target_instructions == WORKFLOW.INSTRUCTIONS
        assert list(case.target_tools) == WORKFLOW.tool_manifest()
        assert set(case.fixtures) == {tool["name"] for tool in case.target_tools}
        policy = document["policy"]
        assert set(policy) == {"expect", "forbid"}
        assert set(policy["expect"]) <= set(RULE_IDS) and set(policy["forbid"]) <= set(RULE_IDS)
        assert not set(policy["expect"]) & set(policy["forbid"])
    assert [d["policy"]["expect"] for d in documents].count([]) == 1
