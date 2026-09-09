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
    swaps. Deferred cases retain the same shape but are outside the default full-call set."""
    intake = collect_cases([EVALS / "intake"], modality="voice")
    full = collect_cases([EVALS / "full_call"], modality="voice")
    deferred = collect_cases([EVALS / "deferred"], modality="voice")
    assert [case.name for case in intake] == [
        "intake stays honest when the caller asks for a person",
        "intake accepts a corrected date of birth",
        "intake hands off when the caller gives everything at once",
    ]
    assert [case.name for case in full] == [
        "the caller asks for a person and the specialist reads out the appointment",
    ]
    assert [case.name for case in deferred] == [
        "the specialist keeps another patient's appointment private",
        "intake takes the details and the specialist moves the appointment",
    ]
    for case in intake + full + deferred:
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
    for case in full + deferred:
        assert list(case.target_tools) == WORKFLOW.tool_manifest()


def test_the_private_case_looks_up_a_patient_who_is_not_the_caller() -> None:
    document = json.loads((EVALS / "deferred" / "caller_is_not_the_patient.json").read_text())
    record = document["fixtures"]["lookup_patient"]["result"]
    assert record["patient"] == "Priya Nair" and "Sam Okafor" in document["simulator"]["instructions"]
    assert {check["type"] for check in document["checks"]} >= {"not_contains", "judge"}


def test_deferred_cases_are_routed_out_of_the_default_full_call_set() -> None:
    """Deferred failures retain their original documents but cannot make the default suite green."""
    full_names = {case.name for case in collect_cases([EVALS / "full_call"], modality="text")}
    deferred_names = {case.name for case in collect_cases([EVALS / "deferred"], modality="text")}
    assert full_names.isdisjoint(deferred_names)
    assert deferred_names == {
        "the specialist keeps another patient's appointment private",
        "intake takes the details and the specialist moves the appointment",
    }
    metadata = (EVALS / "deferred" / "README.md").read_text()
    assert "20260909T103826Z_post_backend_fix" in metadata
    assert "recipe-target-9322f5a672" in metadata
    assert "recipe-target-ef62786a38" in metadata


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


def test_handoff_records_once_and_requests_the_specialist_after_the_tool_result() -> None:
    """The handoff request owns the outgoing-turn boundary and atomically applies the new
    agent. The existing one-shot tool choice preserves the recipe's lookup-first evidence, and
    the context note is sent only after the server accepted the handoff."""
    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def handoff_agent(self, *, instructions, tools, voice, context, operation_id=None):
            self.calls.append(("handoff_agent", instructions, [tool["name"] for tool in tools],
                               voice, context, operation_id))
            return {"accepted": True, "status": "applied"}

        async def set_tool_choice(self, choice, *, one_shot=False):
            self.calls.append(("set_tool_choice", choice, one_shot))

        async def inject_context(self, text, *, role, reply):
            self.calls.append(("inject_context", text, role, reply))
            return {"accepted": True, "reply_started": True}

    state = HandoffState(intake_voice="chime", specialist_voice="warm")
    call = {"summary": "Appointment query.", "details": DETAILS, "caller_confirmed": True}
    assert state.handoff(dict(call)) == {"handoff_requested": True}
    assert state.handoff(dict(call)) == {"handoff_requested": True, "duplicate": True}
    assert state.details == DETAILS and state.summary == "Appointment query."

    session = Session()
    ack = asyncio.run(state.pass_call_on(session))
    assert ack["switched"] is True and ack["status"] == "applied"
    assert ack["reply"]["accepted"] is True
    assert [c[0] for c in session.calls] == ["handoff_agent", "set_tool_choice", "inject_context"]
    assert session.calls[0] == ("handoff_agent", WORKFLOW.specialist_instructions(),
                                ["lookup_patient", "reschedule_appointment"], "warm",
                                state.handover_note(), None)
    assert session.calls[1] == ("set_tool_choice", {"tool": "lookup_patient"}, True)
    note = session.calls[2]
    assert note[2:] == ("context", True)
    assert note[1] == WORKFLOW.HANDOFF_COMPLETE_CONTEXT
    assert [event["type"] for event in state.events] == ["handoff", "passed"]


def test_rejected_handoff_does_not_inject_a_reply() -> None:
    class Session:
        async def handoff_agent(self, **kwargs):
            return {"accepted": False, "reason": "rejected"}

        async def set_tool_choice(self, *args, **kwargs):
            raise AssertionError("a rejected handoff must not change tool choice")

        async def inject_context(self, *args, **kwargs):
            raise AssertionError("a rejected handoff must not request a reply")

    state = HandoffState()
    state.handoff({"summary": "Appointment query.", "details": DETAILS, "caller_confirmed": True})
    assert asyncio.run(state.pass_call_on(Session())) == {
        "accepted": False, "status": "rejected", "switched": False,
        "handoff": {"accepted": False, "reason": "rejected"}, "reply": None,
        "reply_started": False,
    }


def test_opener_exception_keeps_the_handoff_applied() -> None:
    class Session:
        async def handoff_agent(self, **kwargs):
            return {"accepted": True, "status": "applied"}

        async def set_tool_choice(self, *args, **kwargs):
            return None

        async def inject_context(self, *args, **kwargs):
            raise RuntimeError("connection closed")

    state = HandoffState()
    state.handoff({"summary": "Appointment query.", "details": DETAILS, "caller_confirmed": True})
    ack = asyncio.run(state.pass_call_on(Session()))
    assert ack["switched"] and ack["status"] == "applied"
    assert ack["reply"] is None and ack["opener_error"] == "connection closed"


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
    document = json.loads((EVALS / "deferred" / "intake_then_specialist.json").read_text())
    fixed = document["fixtures"]["handoff_to_agent"]["result"]
    live = HandoffState().handoff({"summary": "Appointment query.", "details": DETAILS,
                                   "caller_confirmed": True})
    # The live result is handoff_requested alone: the pass, not the tool result, tells the
    # specialist who it is. The fixed result adds a note only because no specialist follows in
    # a hosted or dialt-sim run, and the intake persona has to end the call itself.
    assert fixed["handoff_requested"] is live["handoff_requested"] is True
    assert set(live) <= set(fixed) and set(fixed) - set(live) == {"note"}
