import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from dialt_recipes.cli import collect_cases


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "agent_to_agent_handoff"
EVALS = EXAMPLE / "evals"
WORKFLOW_SPEC = importlib.util.spec_from_file_location("agent_to_agent_handoff_workflow",
                                                       EXAMPLE / "workflow.py")
assert WORKFLOW_SPEC is not None and WORKFLOW_SPEC.loader is not None
WORKFLOW = importlib.util.module_from_spec(WORKFLOW_SPEC)
sys.modules[WORKFLOW_SPEC.name] = WORKFLOW
WORKFLOW_SPEC.loader.exec_module(WORKFLOW)
HandoffState = WORKFLOW.HandoffState

DETAILS = {"name": "Priya Nair", "date_of_birth": "1988-03-14", "reason": "next appointment"}


def test_cases_use_the_hosted_shape_and_the_workflow_prompt() -> None:
    """The case documents carry the workflow's instructions and tools verbatim, so a prompt
    or manifest edit that forgets to re-render the cases fails here rather than drifting.
    Intake cases declare only the hand-off tool, as a real host starts the call; full-call
    cases declare every tool for host.py, which starts them with the intake manifest and
    swaps."""
    intake = collect_cases([EVALS / "intake"], modality="voice")
    full = collect_cases([EVALS / "full_call"], modality="voice")
    assert [case.name for case in intake] == [
        "intake stays honest when the caller asks for a person",
        "intake accepts a corrected date of birth",
        "intake hands off when the caller gives everything at once",
    ]
    assert [case.name for case in full] == [
        "the caller asks for a person and the specialist reads out the appointment",
        "the specialist keeps another patient's appointment private",
        "intake takes the details and the specialist moves the appointment",
    ]
    for case in intake + full:
        # Every case starts as intake; the specialist's instructions arrive with the pass and
        # are never in a case document.
        assert case.target_instructions == WORKFLOW.intake_instructions()
        assert "lookup_patient before" not in case.target_instructions
        assert case.target.get("voice") == WORKFLOW.DEFAULT_INTAKE_VOICE
        assert case.target.get("greeting") == WORKFLOW.GREETING and case.starter == ""
        assert set(case.fixtures) == {tool["name"] for tool in case.target_tools}
        if "private" not in case.name:
            assert "handoff_to_agent" in {check["value"] for check in case.checks
                                          if check["type"] == "tool_called"}
    for case in intake:
        assert list(case.target_tools) == WORKFLOW.intake_tools()
        assert "end the call" in case.simulator_instructions
    for case in full:
        assert list(case.target_tools) == WORKFLOW.tool_manifest()


def test_the_private_case_looks_up_a_patient_who_is_not_the_caller() -> None:
    document = json.loads((EVALS / "full_call" / "caller_is_not_the_patient.json").read_text())
    record = document["fixtures"]["lookup_patient"]["result"]
    assert record["patient"] == "Priya Nair" and "Sam Okafor" in document["simulator"]["instructions"]
    assert {check["type"] for check in document["checks"]} >= {"not_contains", "judge"}


def test_each_agent_has_its_own_instructions() -> None:
    """Intake is told nothing about how the specialist works and the specialist nothing about
    intake's collection rules: the pass replaces the instructions, it does not append to them."""
    intake, specialist = WORKFLOW.intake_instructions(), WORKFLOW.specialist_instructions()
    assert "handoff_to_agent" in intake and "Evidence:" in intake
    assert "lookup_patient" not in intake and "reschedule_appointment" not in intake
    assert "lookup_patient" in specialist and "only with the patient themselves" in specialist
    assert "handoff_to_agent" not in specialist and "Evidence:" not in specialist
    assert [tool["name"] for tool in WORKFLOW.specialist_tools()] == [
        "lookup_patient", "reschedule_appointment"]


def test_intake_can_only_hand_off() -> None:
    """The phase boundary is a tool contract the host enforces, not a prompt rule alone."""
    assert [tool["name"] for tool in WORKFLOW.intake_tools()] == ["handoff_to_agent"]
    assert "caller_confirmed" in WORKFLOW.intake_tools()[0]["parameters"]["required"]


def test_handoff_validates_before_anything_changes_on_the_call() -> None:
    state = HandoffState()
    with pytest.raises(ValueError, match="requires: caller_confirmed, details, summary"):
        state.handoff({"summary": "x"})
    with pytest.raises(ValueError, match="not confirmed"):
        state.handoff({"summary": "x", "details": DETAILS, "caller_confirmed": False})
    with pytest.raises(ValueError, match="incomplete"):
        state.handoff({"summary": "x", "details": {"name": "Priya Nair"}, "caller_confirmed": True})
    with pytest.raises(ValueError, match="summary"):
        state.handoff({"summary": " ", "details": DETAILS, "caller_confirmed": True})
    assert not state.handed_off and state.events == []


def test_handoff_records_once_and_the_pass_declares_the_specialist_in_order() -> None:
    """The tool result closes intake's turn with nothing but handoff_complete; the pass, sent
    once that turn has closed, declares the specialist in the order the broker needs: the new
    instructions with new_speaker (the fold), then tools, then voice, then the note that makes
    the specialist speak."""
    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def set_instructions(self, text, *, new_speaker=False):
            self.calls.append(("set_instructions", text, new_speaker))

        async def set_tools(self, tools):
            self.calls.append(("set_tools", [tool["name"] for tool in tools]))

        async def set_voice(self, voice):
            self.calls.append(("set_voice", voice))

        async def inject_context(self, text, *, role, reply):
            self.calls.append(("inject_context", text, role, reply))
            return {"accepted": True, "reply_started": True}

    state = HandoffState(intake_voice="chime", specialist_voice="warm")
    call = {"summary": "Appointment query.", "details": DETAILS, "caller_confirmed": True}
    assert state.handoff(dict(call)) == {"handoff_complete": True}
    assert state.handoff(dict(call)) == {"handoff_complete": True, "duplicate": True}
    assert state.details == DETAILS and state.summary == "Appointment query."

    session = Session()
    ack = asyncio.run(state.pass_call_on(session))
    assert ack["accepted"] is True and state.passed is True
    assert [c[0] for c in session.calls] == [
        "set_instructions", "set_tools", "set_voice", "inject_context"]
    assert session.calls[0] == ("set_instructions", WORKFLOW.specialist_instructions(), True)
    assert session.calls[1] == ("set_tools", ["lookup_patient", "reschedule_appointment"])
    assert session.calls[2] == ("set_voice", "warm")
    note = session.calls[3]
    assert note[2:] == ("context", True)
    assert "Priya Nair" in note[1] and "1988-03-14" in note[1] and "Appointment query." in note[1]
    assert [event["type"] for event in state.events] == ["handoff", "passed"]


def test_the_pass_needs_a_landed_handoff() -> None:
    with pytest.raises(RuntimeError, match="has not landed"):
        asyncio.run(HandoffState().pass_call_on(object()))


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
    live = HandoffState().handoff({"summary": "Appointment query.", "details": DETAILS,
                                   "caller_confirmed": True})
    # The live result is handoff_complete alone: the pass, not the tool result, tells the
    # specialist who it is. The fixed result adds a note only because no specialist follows in
    # a hosted or dialt-sim run, and the intake persona has to end the call itself.
    assert fixed["handoff_complete"] is live["handoff_complete"] is True
    assert set(live) <= set(fixed) and set(fixed) - set(live) == {"note"}
