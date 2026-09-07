"""Two agent personas on one call: an intake voice that takes details, then a specialist voice
that helps. One Dialt session, one conversation history, no second connection.

The example is a clinic appointment line. The intake persona is an obviously synthetic voice
that takes the patient's name, date of birth and reason for calling. When it has them, it calls
`handoff_to_agent`. The host declares the specialist's tools, switches the session voice and
returns the handover note as the tool result; the model continues the same call as the
scheduling specialist, with everything intake collected still in its context. The specialist
voice is a warm, human-sounding one. Both roles live in the session instructions from the
start, so the hand-off changes nothing about the prompt: the tool result is the boundary.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from dialt import DialtSession
from dialt_recipes import ConversationPlan, PlanField

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
               "same call, so no permission to transfer is needed. Never say the specialist "
               "has the call until the tool result confirms it.",
    tool_name="record_intake", record_as_you_go=False,
)

SPECIALIST_ROLE = (
    "The scheduling specialist is an automated agent on this same system, not a person. Until "
    "handoff_to_agent has returned, handoff_to_agent is the only tool available to you. "
    "After handoff_to_agent returns handoff_complete, you continue the same call as the "
    "scheduling specialist. You already have the intake details, so do not ask for them "
    "again. Call lookup_patient before answering anything about an appointment, and discuss "
    "a patient's appointments only with the patient themselves: if the caller is not the "
    "patient, do not share any appointment details; say so and offer to have the clinic call "
    "the patient back. Use reschedule_appointment to move an appointment once the caller has "
    "agreed to the new date. A change has happened only when the tool result says so. There "
    "is no one else to transfer the call to: offer a callback for anything the tools cannot "
    "settle on this call. At any point in the call, if the caller asks whether they are "
    "speaking to a person, answer honestly."
)

GREETING = ("Hi, you've reached the clinic appointment line. To get started, could I take the "
            "patient's name?")


def instructions() -> str:
    """One instruction string for the whole call: the intake plan, then the specialist role."""
    return f"{INTAKE_PLAN.instructions()}\n\n{SPECIALIST_ROLE}"


def intake_tools() -> list[dict[str, Any]]:
    """The phase boundary as a tool contract: intake declares only the hand-off, so however the
    caller front-loads their details it cannot act as the specialist early. The host swaps in
    the full manifest when the hand-off lands. Hosted and CLI runs cannot swap, so the cases
    declare every tool and rely on the instructions alone; host.py and a real host start here."""
    return [tool for tool in tool_manifest() if tool["name"] == "handoff_to_agent"]


def tool_manifest() -> list[dict[str, Any]]:
    date_of_birth = {"type": "string",
                     "description": "The patient's date of birth, ISO 8601 (YYYY-MM-DD)."}
    return [
        {
            "name": "handoff_to_agent",
            "description": (
                "Pass the call to the scheduling specialist. Before calling it, read the "
                "name, date of birth and reason back to the caller and wait for them to "
                "confirm, even when they gave "
                "everything in their first sentence. Supply the confirmed details. Returns "
                "handoff_complete when the specialist has the call."
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
    events: list[dict[str, Any]] = field(default_factory=list)

    def handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate the handover and record it. Raises on an incomplete or malformed call, so
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
        return {
            "handoff_complete": True,
            "note": (f"The intake step is finished. You are now the scheduling specialist on "
                     f"this same call. The patient is {details['name']}."),
        }

    async def handoff_on(self, session: DialtSession, args: dict[str, Any]) -> dict[str, Any]:
        """The host side of the tool: validate, declare the specialist's tools, switch the
        session voice, then confirm. Both apply from the next reply, the specialist's first
        line."""
        result = self.handoff(args)
        if not result.get("duplicate"):
            await session.set_tools(tool_manifest())
            await session.set_voice(self.specialist_voice)
        return result
