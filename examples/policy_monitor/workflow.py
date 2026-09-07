"""The call the monitor watches: a clinic appointment line with two tools.

The agent's prompt says nothing about the policy. That is deliberate: the recipe shows a policy
enforced beside the conversation, by a monitor the customer owns, so the prompt and the policy
can change independently. A rule the agent must never break belongs in the prompt as well.
"""
from __future__ import annotations

from typing import Any

DEFAULT_VOICE = "southern_us_female"

INSTRUCTIONS = (
    "You are the appointment line for a clinic. Help callers check or move their next "
    "appointment. Call lookup_patient before answering anything about an appointment. Use "
    "reschedule_appointment once the caller has agreed to the new date; a change has happened "
    "only when the tool result says so. You are an automated assistant; say so if asked."
)

GREETING = "Hi, you've reached the clinic appointment line. How can I help?"


def tool_manifest() -> list[dict[str, Any]]:
    date_of_birth = {"type": "string",
                     "description": "The patient's date of birth, ISO 8601 (YYYY-MM-DD)."}
    return [
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


def session_mode(*, voice: str = DEFAULT_VOICE) -> dict[str, Any]:
    """The start-frame mode document for a call."""
    return {"kind": "dialt", "voice": voice, "instructions": INSTRUCTIONS,
            "tools": tool_manifest(), "greeting": GREETING, "end_call": True}
