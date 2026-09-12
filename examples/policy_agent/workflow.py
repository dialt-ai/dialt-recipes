"""The call and its policy: a clinic appointment line with two tools and three rules.

The agent's instructions say nothing about the policy. That is deliberate: `mode.policy` is
enforced by Dialt's policy agent beside the conversation, so the prompt and the policy can
change independently. A rule the agent must never break belongs in the instructions as well.
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

POLICY: dict[str, Any] = {
    "subject": "a clinic appointment call",
    "report_checks": True,
    "rules": [
        {
            "id": "emergency",
            "when": (
                "The caller says they have, right now or since today, symptoms that may need "
                "urgent care: chest pain, difficulty breathing, signs of a stroke, heavy "
                "bleeding, a severe allergic reaction, or thoughts of harming themselves. "
                "Symptoms described in the past tense, such as an episode weeks ago that a "
                "follow-up appointment is for, are not this rule."
            ),
            "do": (
                "Stop what you are doing. Tell the caller plainly that this line cannot help "
                "with an urgent medical problem and that they should hang up and call emergency "
                "services now. Then end the call."
            ),
            "action": "speak_now",
        },
        {
            "id": "clinical_advice",
            "when": (
                "The caller asks for medical advice: whether symptoms are serious, whether to "
                "take, stop or change a medicine, what a result means. Or the agent has started "
                "to give such advice. Practical questions about an appointment, such as what to "
                "bring, are not this rule."
            ),
            "do": (
                "Do not give medical advice and do not assess symptoms. Say that a clinician "
                "has to answer that, and offer to book an appointment or a nurse callback."
            ),
            "action": "next_turn",
        },
        {
            "id": "complaint",
            "when": (
                "The caller says they want to make a complaint, asks for a manager, or says "
                "they will take the matter further. Thanks or general feedback are not this "
                "rule."
            ),
            "do": (
                "Acknowledge the complaint without arguing, take a one-line summary of it, and "
                "say the practice manager will call them back. Do not offer to transfer the "
                "call."
            ),
            "action": "next_turn",
        },
    ],
}

RULE_IDS = [rule["id"] for rule in POLICY["rules"]]


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
            "status_label": "appointment change",
        },
    ]


def session_mode(*, voice: str = DEFAULT_VOICE) -> dict[str, Any]:
    """The start-frame mode document for a call, policy included."""
    return {"kind": "dialt", "voice": voice, "instructions": INSTRUCTIONS,
            "tools": tool_manifest(), "greeting": GREETING, "end_call": True,
            "policy": POLICY}


def extended_mode() -> dict[str, Any]:
    """Optional occurrence tracking and identity protection using native policy controls."""
    from copy import deepcopy
    mode = deepcopy(session_mode())
    policy = mode["policy"]
    policy.update(recheck_corrections=True, batch_guidance=True,
                  wait_for_check=["reschedule_appointment"],
                  block_on_error=["reschedule_appointment"])
    for rule in policy["rules"]:
        if rule["id"] == "complaint":
            rule["frequency"] = "once_per_turn"
    policy["rules"].append({
        "id": "identity_mismatch",
        "when": "The caller explicitly says the patient record being discussed belongs to someone else. "
                "A caller correcting the spelling of their own name is not this rule.",
        "do": "Do not change that appointment. Clarify whose record is needed before proceeding.",
        "action": "next_turn",
        "block_tools": ["reschedule_appointment"],
    })
    return mode
