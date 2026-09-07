import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from dialt_recipes.cli import collect_cases


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "agent_handoff"
EVALS = EXAMPLE / "evals"
WORKFLOW_SPEC = importlib.util.spec_from_file_location("agent_handoff_workflow", EXAMPLE / "workflow.py")
assert WORKFLOW_SPEC is not None and WORKFLOW_SPEC.loader is not None
WORKFLOW = importlib.util.module_from_spec(WORKFLOW_SPEC)
sys.modules[WORKFLOW_SPEC.name] = WORKFLOW
WORKFLOW_SPEC.loader.exec_module(WORKFLOW)
HandoffState = WORKFLOW.HandoffState

DETAILS = {"name": "Priya Nair", "account_reference": "4471", "reason": "next payment date"}


def test_cases_use_the_hosted_shape_and_the_workflow_prompt() -> None:
    """The case documents carry the workflow's instructions and tools verbatim, so a prompt
    or manifest edit that forgets the cases fails here rather than drifting. Intake cases
    declare only the hand-off tool, as a real host starts the call; full-call cases declare
    every tool for host.py, which starts them with the intake manifest and swaps."""
    intake = collect_cases([EVALS / "intake"], modality="voice")
    full = collect_cases([EVALS / "full_call"], modality="voice")
    assert [case.name for case in intake] == [
        "intake stays honest when the caller asks for a person",
        "intake accepts a corrected account reference",
        "intake hands off when the caller gives everything at once",
    ]
    assert [case.name for case in full] == [
        "the caller asks for a person and the specialist explains the missed payment",
        "intake takes the details and the specialist answers the call",
    ]
    for case in intake + full:
        assert case.target_instructions == WORKFLOW.instructions()
        assert case.target.get("voice") == WORKFLOW.DEFAULT_INTAKE_VOICE
        assert "handoff_to_agent" in {check["value"] for check in case.checks
                                      if check["type"] == "tool_called"}
        assert set(case.fixtures) == {tool["name"] for tool in case.target_tools}
    for case in intake:
        assert list(case.target_tools) == WORKFLOW.intake_tools()
        assert "end the call" in case.simulator_instructions
    for case in full:
        assert list(case.target_tools) == WORKFLOW.tool_manifest()


def test_both_roles_are_declared_up_front() -> None:
    text = WORKFLOW.instructions()
    assert "handoff_to_agent" in text and "account specialist" in text
    assert text.index("Evidence:") < text.index("After handoff_to_agent returns")


def test_intake_can_only_hand_off() -> None:
    """The phase boundary is a tool contract the host enforces, not a prompt rule alone."""
    assert [tool["name"] for tool in WORKFLOW.intake_tools()] == ["handoff_to_agent"]
    assert WORKFLOW.intake_tools()[0] == WORKFLOW.tool_manifest()[0]


def test_handoff_validates_before_anything_changes_on_the_call() -> None:
    state = HandoffState()
    with pytest.raises(ValueError, match="requires: details, summary"):
        state.handoff({"summary": "x"})
    with pytest.raises(ValueError, match="incomplete"):
        state.handoff({"summary": "x", "details": {"name": "Priya Nair"}})
    with pytest.raises(ValueError, match="summary"):
        state.handoff({"summary": " ", "details": DETAILS})
    assert not state.handed_off and state.events == []


def test_handoff_switches_the_voice_once_and_returns_the_handover_note() -> None:
    class Session:
        def __init__(self) -> None:
            self.voices: list[str] = []
            self.tools: list[object] = []

        async def set_voice(self, voice: str) -> None:
            self.voices.append(voice)

        async def set_tools(self, tools) -> None:
            self.tools.append([tool["name"] for tool in tools])

    state = HandoffState(intake_voice="chime", specialist_voice="warm")
    session = Session()
    first = asyncio.run(state.handoff_on(session, {"summary": "Payment date query.", "details": DETAILS}))
    again = asyncio.run(state.handoff_on(session, {"summary": "Payment date query.", "details": DETAILS}))
    assert first == {"handoff_complete": True,
                     "note": "The account specialist now has the call and the intake details for Priya Nair."}
    assert again == {"handoff_complete": True, "duplicate": True}
    assert session.voices == ["warm"]
    assert session.tools == [["handoff_to_agent", "lookup_account", "move_payment_date"]]
    assert state.details == DETAILS and state.summary == "Payment date query."
    assert [event["type"] for event in state.events] == ["handoff"]


def test_voices_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("INTAKE_VOICE", "circuit")
    monkeypatch.setenv("SPECIALIST_VOICE", "british_female")
    state = HandoffState()
    assert (state.intake_voice, state.specialist_voice) == ("circuit", "british_female")
    monkeypatch.delenv("INTAKE_VOICE")
    monkeypatch.delenv("SPECIALIST_VOICE")
    assert HandoffState().intake_voice == WORKFLOW.DEFAULT_INTAKE_VOICE


def test_fixed_handoff_result_matches_the_live_shape() -> None:
    document = json.loads((EVALS / "full_call" / "intake_then_specialist.json").read_text())
    fixed = document["fixtures"]["handoff_to_agent"]["result"]
    assert fixed["handoff_complete"] is True and "note" in fixed
