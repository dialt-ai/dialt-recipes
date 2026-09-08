"""Two agents on one call: an intake voice that takes details, then a specialist voice that
helps. One Dialt session, one conversation, no second connection.

The example is a clinic appointment line. Intake is an obviously synthetic voice that takes
the patient's name, date of birth and reason for calling, reads them back, and calls
`handoff_to_agent` once the caller confirms. The host validates and resolves the tool; intake
finishes its own turn. When that turn has closed, the host passes the call
(`HandoffState.pass_call_on`, built on `dialt_recipes.pass_call_to`): the specialist's
instructions replace intake's, the broker folds the call so far into a transcript the
specialist holds, the specialist's tools and voice are declared, and a short note makes it
speak first. Each agent has its own instructions; neither is told about the other's rules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from dialt import DialtSession
from dialt_recipes import ConversationPlan, PlanField, pass_call_to

DEFAULT_INTAKE_VOICE = "chime"
DEFAULT_SPECIALIST_VOICE = "southern_us_female"


INTAKE_PLAN = ConversationPlan(
    name="the intake step of a clinic appointment call",
    objective="Take the patient's name, their date of birth and what they are calling about, "
              "then pass the call to the scheduling specialist.",
    fields=(PlanField("name", "The patient's name."),
            PlanField("date_of_birth", "The patient's date of birth."),
            PlanField("reason", "What the caller needs help with, in their words.")),
    completion="When you have all three, read them back in one sentence and ask the caller "
               "if that is right. Call handoff_to_agent only after the caller confirms, even "
               "when they gave everything at once. The specialist is the next step of this "
               "same call, so no permission to transfer is needed. When the tool result "
               "reports handoff_complete, tell the caller you are passing them to the "
               "specialist and stop there: the specialist speaks next.",
    record_as_you_go=False,
)

INTAKE_ROLE = (
    "You are the intake step of the clinic's appointment line, an automated agent, not a "
    "person; if the caller asks whether they are speaking to a person, answer honestly. The "
    "scheduling specialist you pass calls to is also an automated agent on this same system. "
    "You cannot look up or change appointments: handoff_to_agent is your only tool."
)

SPECIALIST_ROLE = (
    "You are the clinic's scheduling specialist, an automated agent, not a person; if the "
    "caller asks, say so. Intake has just passed this caller to you on the same call, and the "
    "transcript so far is in your context: you already have the patient's name, date of birth "
    "and reason, so do not ask for them again, and do not say you are connecting or "
    "transferring anyone. Call lookup_patient before answering anything about an appointment, "
    "and discuss a patient's appointments only with the patient themselves: if the caller is "
    "not the patient, do not share any appointment details; say so and offer to have the "
    "clinic call the patient back. Use reschedule_appointment to move an appointment once the "
    "caller has agreed to the new date. A change has happened only when the tool result says "
    "so. There is no one else to transfer the call to: offer a callback for anything the tools "
    "cannot settle on this call."
)

GREETING = ("Hi, you've reached the clinic appointment line. To get started, could I take the "
            "patient's name?")


def intake_instructions() -> str:
    """What the session starts with: the intake plan and intake's own role, nothing about how
    the specialist works."""
    return f"{INTAKE_PLAN.instructions()}\n\n{INTAKE_ROLE}"


def specialist_instructions() -> str:
    """What replaces intake's instructions when the call is passed."""
    return SPECIALIST_ROLE


def intake_tools() -> list[dict[str, Any]]:
    """The phase boundary as a tool contract: intake declares only the hand-off, so however the
    caller front-loads their details it cannot act as the specialist early."""
    return [tool for tool in tool_manifest() if tool["name"] == "handoff_to_agent"]


def specialist_tools() -> list[dict[str, Any]]:
    """What the specialist can do; the hand-off is intake's and is not carried over."""
    return [tool for tool in tool_manifest() if tool["name"] != "handoff_to_agent"]


def tool_manifest() -> list[dict[str, Any]]:
    """Every tool the call can use, for the full-call cases' fixtures."""
    date_of_birth = {"type": "string",
                     "description": "The patient's date of birth, ISO 8601 (YYYY-MM-DD)."}
    return [
        {
            "name": "handoff_to_agent",
            "description": (
                "Pass the call to the scheduling specialist. Before calling it, read the "
                "name, date of birth and reason back to the caller and wait for them to "
                "confirm, even when they gave everything in their first sentence. Supply the "
                "confirmed details. Returns handoff_complete when the specialist will take "
                "the call next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "One or two sentences on why the caller rang."},
                    "details": INTAKE_PLAN.evidence_schema(),
                    "caller_confirmed": {
                        "type": "boolean",
                        "description": ("Whether the caller has heard these details read back "
                                        "and said they are right. The hand-off is refused "
                                        "when this is false."),
                    },
                },
                "required": ["summary", "details", "caller_confirmed"],
                "additionalProperties": False,
            },
            "expected_duration": "instant",
            "status_label": "handover to the automated scheduling specialist",
        },
        {
            "name": "lookup_patient",
            "description": (
                "Look up a patient by name and date of birth: their next appointment (date, "
                "time, clinician) and how far it can be moved."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "date_of_birth": date_of_birth},
                "required": ["name", "date_of_birth"],
                "additionalProperties": False,
            },
            "read_only": True,
            "expected_duration": "seconds",
            "status_label": "patient lookup",
        },
        {
            "name": "reschedule_appointment",
            "description": (
                "Move the patient's next appointment to a new date the caller has agreed to, "
                "keeping the same time and clinician. Returns the confirmed date, or a reason "
                "the move is not allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string",
                                       "description": "The id returned by lookup_patient."},
                    "new_date": {"type": "string",
                                 "description": "The agreed date, ISO 8601 (YYYY-MM-DD)."},
                },
                "required": ["appointment_id", "new_date"],
                "additionalProperties": False,
            },
            "expected_duration": "seconds",
            "status_label": "appointment change",
        },
    ]


@dataclass
class HandoffState:
    """Application-owned state behind `handoff_to_agent`, one per call."""

    intake_voice: str = field(
        default_factory=lambda: os.environ.get("INTAKE_VOICE") or DEFAULT_INTAKE_VOICE)
    specialist_voice: str = field(
        default_factory=lambda: os.environ.get("SPECIALIST_VOICE") or DEFAULT_SPECIALIST_VOICE)
    details: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    handed_off: bool = False
    passed: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate the hand-off and record it. Raises on an incomplete or malformed call, so
        nothing changes on the call until the details are right."""
        if set(args) != {"summary", "details", "caller_confirmed"}:
            raise ValueError("handoff requires: caller_confirmed, details, summary")
        if args["caller_confirmed"] is not True:
            raise ValueError("the caller has not confirmed the details yet: read them back, "
                             "wait for the caller's answer, then call again")
        details = INTAKE_PLAN.validate_evidence(args["details"])
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be non-empty text")
        if self.handed_off:
            return {"handoff_complete": True, "duplicate": True}
        self.details = details
        self.summary = summary.strip()
        self.handed_off = True
        self.events.append({"type": "handoff", "summary": self.summary, "details": details})
        return {"handoff_complete": True}

    def handover_note(self) -> str:
        """The host's note that makes the specialist speak first: who was passed and why."""
        return (f"Intake has just passed the caller to you on this same call. Patient: "
                f"{self.details['name']}, date of birth {self.details['date_of_birth']}. "
                f"Reason: {self.summary}")

    async def pass_call_on(self, session: DialtSession) -> dict[str, Any]:
        """Pass the live call to the specialist once intake's hand-off turn has closed
        (`HandoffBoundary` says when). Returns the broker's acknowledgement of the note."""
        if not self.handed_off:
            raise RuntimeError("the hand-off has not landed")
        ack = await pass_call_to(
            session, instructions=specialist_instructions(), tools=specialist_tools(),
            voice=self.specialist_voice, note=self.handover_note())
        self.passed = True
        self.events.append({"type": "passed", "accepted": bool(ack.get("accepted"))})
        return ack
