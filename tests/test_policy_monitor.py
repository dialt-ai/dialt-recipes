import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
    monitor._batch_end = len(monitor.lines)          # as _check records before it sends

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


class FakePolicy:
    """A scripted policy session: events come from a queue the test feeds."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.results: list[tuple[str, dict, str]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_tool_result(self, call_id, value, *, outcome, verified) -> None:
        self.results.append((call_id, value, outcome))

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        pass

    def emit(self, type_: str, **data) -> None:
        self.queue.put_nowait(SimpleNamespace(type=type_, data=data))


def start_with_fake(monitor, policy, live):
    monitor._policy, monitor._live = policy, live
    monitor._tasks = [asyncio.create_task(monitor._read_policy()),
                      asyncio.create_task(monitor._run_checks())]


def test_checks_batch_lines_and_only_a_final_done_ends_a_check() -> None:
    async def run():
        monitor = POLICY.PolicyMonitor("wss://unused", "key", check_timeout_s=0.2,
                                       deliver_interval_s=0)
        policy, live = FakePolicy(), FakeLive()
        start_with_fake(monitor, policy, live)

        monitor.note("caller", "hello")
        await asyncio.sleep(0)                       # the checker sends the first batch
        monitor.note("agent", "hi")                  # arrives during the check: next batch
        monitor.note("caller", "chest pain now")
        policy.emit("done", turn_id="t1-bridge")     # a bridge turn does not end the check
        await asyncio.sleep(0.05)
        assert not monitor._idle.is_set() and policy.sent == ["caller: hello"]
        policy.emit("tool_call", id="c1", name="raise_flag",
                    args={"rule": "emergency", "evidence": "chest pain"})
        policy.emit("tool_call", id="c2", name="something_else", args={})
        policy.emit("done", turn_id="t1-final")
        await asyncio.sleep(0.05)
        assert policy.sent[1] == "agent: hi\ncaller: chest pain now"
        policy.emit("done", turn_id="t2")
        await asyncio.wait_for(monitor.settle(), 1)

        assert [r[2] for r in policy.results] == ["succeeded", "failed"]
        assert [flag["rule"] for flag in monitor.flags] == ["emergency"]
        assert monitor.flags[0]["after_line"] == 1 and monitor.flags[0]["delivered"]
        assert live.calls[0]["reply"] is True
        assert monitor.checks == 2 and not monitor.dead

        # A timed-out check: the late done is ignored, the next check is unaffected.
        monitor.note("caller", "slow one")
        await asyncio.sleep(0.3)
        assert monitor.flags[-1]["error"] == "check timed out"
        monitor.note("caller", "after the slow one")
        await asyncio.sleep(0.05)
        policy.emit("done", turn_id="t3-late")       # belongs to the timed-out check
        await asyncio.sleep(0.05)
        assert not monitor._idle.is_set()
        policy.emit("done", turn_id="t4")
        await asyncio.wait_for(monitor.settle(), 1)
        assert monitor.checks == 4

        # The policy session ends: the monitor is dead, settle returns, lines are kept.
        policy.queue.put_nowait(None)
        await asyncio.sleep(0.05)
        assert monitor.dead and monitor.flags[-1]["error"] == "policy session ended"
        monitor.note("caller", "still talking")
        await asyncio.wait_for(monitor.settle(), 1)
        assert monitor.checks == 4 and monitor.lines[-1] == "caller: still talking"
        await monitor.close()

    asyncio.run(run())
