"""Two agent personas on one call: an intake voice that takes details, then a specialist voice
that helps. One Dialt session, one conversation history, no second connection.

The intake persona is an obviously synthetic voice that takes the caller's details plainly.
When it has them, it calls `handoff_to_agent`. The host switches the session voice and returns
the handover note as the tool result; the model continues the same call as the specialist,
with everything intake collected still in its context. The specialist voice is a warm,
human-sounding one. Both roles live in the session instructions from the start, so the
handoff changes nothing about the prompt: the tool result is the boundary.
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
    name="the intake step of an account support call",
    objective="Take the caller's name, their account reference and what they are calling "
              "about, then pass the call to the account specialist.",
    fields=(PlanField("name", "The caller's name."),
            PlanField("account_reference",
                      "The caller's account reference, or the last four digits of it."),
            PlanField("reason", "What the caller needs help with, in their words.")),
    completion="Briefly read the details back, then call handoff_to_agent with them. The "
               "specialist is the next step of this same call, so no permission to transfer "
               "is needed. Never say the specialist has the call until the tool result "
               "confirms it.",
    tool_name="record_intake", record_as_you_go=False,
)

SPECIALIST_ROLE = (
    "The account specialist is an automated agent on this same system, not a person. Until "
    "handoff_to_agent has returned, handoff_to_agent is the only tool available to you. "
    "After handoff_to_agent returns handoff_complete, you continue the same call as the "
    "account specialist. You already have the intake details, so do not ask for them again. "
    "Call lookup_account before answering anything about the account, and move_payment_date "
    "to change a payment date once the caller has agreed to the new date. A change has "
    "happened only when the tool result says so. There is no one else to transfer the call "
    "to: offer a callback for anything the tools cannot settle on this call. At any point in "
    "the call, if the caller asks whether they are speaking to a person, answer honestly."
)


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
    return [
        {
            "name": "handoff_to_agent",
            "description": (
                "Pass the call to the account specialist. Call it only after the caller has "
                "heard their details read back and confirmed them, however they were given. "
                "Supply the confirmed details. Returns handoff_complete when the specialist "
                "has the call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "One or two sentences on why the caller rang."},
                    "details": INTAKE_PLAN.evidence_schema(),
                },
                "required": ["summary", "details"],
                "additionalProperties": False,
            },
            "expected_duration": "instant",
            "status_label": "passing the call on",
        },
        {
            "name": "lookup_account",
            "description": (
                "Look up the caller's account by reference: balance, next payment date and "
                "any open arrangement."
            ),
            "parameters": {
                "type": "object",
                "properties": {"account_reference": {"type": "string"}},
                "required": ["account_reference"],
                "additionalProperties": False,
            },
            "read_only": True,
            "expected_duration": "seconds",
            "status_label": "account lookup",
        },
        {
            "name": "move_payment_date",
            "description": (
                "Move the caller's next payment to a new date the caller has agreed to. "
                "Returns the confirmed date, or a reason the move is not allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account_reference": {"type": "string"},
                    "new_date": {"type": "string",
                                 "description": "The agreed date, ISO 8601 (YYYY-MM-DD)."},
                },
                "required": ["account_reference", "new_date"],
                "additionalProperties": False,
            },
            "expected_duration": "seconds",
            "status_label": "payment date change",
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
        if set(args) != {"summary", "details"}:
            raise ValueError("handoff requires: details, summary")
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
            "note": (f"The account specialist now has the call and the intake details for "
                     f"{details['name']}."),
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
