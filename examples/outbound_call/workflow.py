"""Conversation workflow for an authorized outbound service-appointment reminder.

The host supplies a ``ReminderContext`` from its persisted attempt. The model initially sees
only the business and recipient's first name: appointment facts remain server-side until a
host-bound verification tool releases them. Neither tool accepts a phone number, contact
identifier or appointment identifier, so the model cannot choose or alter the bound attempt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_VOICE = "southern_us_female"
MAX_APPOINTMENT_SUMMARY_CHARS = 500
CALLBACK_REQUEST_RECORDED = (
    "Your callback request was recorded, but no callback has been scheduled."
)
OUTCOME_VALUES = (
    "confirmed",
    "reschedule_requested",
    "wrong_number",
    "opted_out",
)


@dataclass(frozen=True)
class ReminderContext:
    """Server-owned facts the agent needs for one reminder conversation.

    Deliberately absent: telephone numbers and contact, customer or appointment identifiers.
    Those remain with the outbound attempt in the application.
    """

    business_name: str
    recipient_first_name: str
    appointment_summary: str

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if len(self.appointment_summary) > MAX_APPOINTMENT_SUMMARY_CHARS:
            raise ValueError(
                f"appointment_summary must be at most {MAX_APPOINTMENT_SUMMARY_CHARS} characters"
            )


DEMO_REMINDER = ReminderContext(
    business_name="Northstar Home Services",
    recipient_first_name="Jordan",
    appointment_summary="annual boiler service on Tuesday 22 September at 10:30 a.m.",
)


def greeting(context: ReminderContext = DEMO_REMINDER) -> str:
    """The fixed outbound opener: identify the business and automation before asking who answered."""
    return (
        f"Hello, this is an automated assistant calling from {context.business_name}. "
        f"May I speak with {context.recipient_first_name}?"
    )


def instructions(context: ReminderContext = DEMO_REMINDER) -> str:
    """Build the pre-verification prompt without appointment facts or application identifiers."""
    return f"""You are making an authorized outbound call for {context.business_name}. You are an
automated assistant, not a person. The fixed greeting has already identified the business,
disclosed that you are automated, and asked for {context.recipient_first_name}; do not repeat it
unless the person did not hear it or asks who is calling.

Identity boundary:
- Treat only a clear first-person confirmation that the person is {context.recipient_first_name}
  as verification. A question such as "Who is this?", an uncertain answer, silence, or someone
  merely repeating the name is not verification. Clarify without revealing why you called.
- Before verification, do not mention an appointment, reminder, service type, date or time. Do
  not ask for a date of birth, address, phone number, or any contact, customer or appointment ID.
- If they clearly say they are not {context.recipient_first_name} or that the number is wrong,
  apologize without revealing appointment details, call record_call_outcome with wrong_number,
  and end after its result. If identity stays ambiguous, say you cannot continue, then end
  without calling either application tool.

Once the intended recipient is clearly verified, call verify_recipient with
recipient_confirmed=true. Only its successful result gives you the appointment summary. Say that
returned summary as a reminder, then ask whether they plan to keep it or need help rescheduling.
If verification fails, reveal no purpose or appointment facts and end.

- If they clearly confirm, call record_call_outcome with confirmed.
- If they want a different time, explain that this call cannot change the appointment. Offer to
  record a request for a scheduling callback, without guaranteeing a callback or its timing.
  Include callback_request=true only if they explicitly accept that offer; include false if
  they explicitly decline it. Then record reschedule_requested.
- If they ask not to be called again at any point, do not persuade them or ask why. Stop the
  reminder flow and record opted_out immediately. Before identity verification, still reveal no
  appointment details.

Both application tools are bound by the host to this call, so never ask for or supply phone
numbers, contact IDs or appointment IDs. Use record_call_outcome once for the clear conversation
outcome. An opt-out is the exception: if the person asks not to be called even after another
outcome was recorded during the goodbye, call it again with opted_out so suppression is saved.
It records a response; it does not change an appointment or itself place a callback. Never say
an outcome was saved, an appointment was changed, or a callback was arranged until the tool
result confirms the corresponding fact. If a tool reports failure, say plainly what failed.
Give a next step only if the tool returned one; never invent one. Finish with a brief, truthful
goodbye and end the call."""


GREETING = greeting()
INSTRUCTIONS = instructions()


def tool_manifest() -> list[dict[str, Any]]:
    return [
        {
            "name": "verify_recipient",
            "description": (
                "After the person clearly confirms in the first person that they are the "
                "recipient named in the greeting, ask the host to release this attempt's "
                "appointment reminder facts. Never call this for an ambiguous answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_confirmed": {
                        "type": "boolean",
                        "description": (
                            "True only after the named recipient clearly confirms that they "
                            "are speaking."
                        ),
                    },
                },
                "required": ["recipient_confirmed"],
                "additionalProperties": False,
            },
            "expected_duration": "instant",
            "status_label": "recipient verification",
        },
        {
            "name": "record_call_outcome",
            "description": (
                "Record the final response for the outbound attempt already bound by the host. "
                "This does not change an appointment or place a callback. Use callback_request "
                "only with reschedule_requested, after the recipient explicitly accepts or "
                "declines a scheduling callback. An opted_out request must be recorded even if "
                "another response was just recorded. Its arguments deliberately contain no "
                "name, phone number, contact ID or appointment ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": list(OUTCOME_VALUES),
                        "description": "The one clear terminal outcome of this conversation.",
                    },
                    "callback_request": {
                        "type": "boolean",
                        "description": (
                            "Only for reschedule_requested: whether the recipient explicitly "
                            "accepted the offered scheduling callback."
                        ),
                    },
                },
                "required": ["outcome"],
                "additionalProperties": False,
            },
            "expected_duration": "instant",
            "status_label": "call outcome",
        }
    ]


def validate_outcome(args: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized tool payload without mutating call or durable state.

    A host can run this before its atomic database write, then hydrate ``OutboundCallState``
    from the committed value. That keeps a failed durable write from leaving only the in-memory
    recipe state committed.
    """
    if not isinstance(args, dict):
        raise ValueError("call outcome must be an object")
    allowed = {"outcome", "callback_request"}
    unexpected = set(args) - allowed
    if unexpected:
        raise ValueError(f"unexpected call outcome fields: {', '.join(sorted(unexpected))}")

    outcome = args.get("outcome")
    if outcome not in OUTCOME_VALUES:
        raise ValueError(f"outcome must be one of: {', '.join(OUTCOME_VALUES)}")

    canonical: dict[str, Any] = {"outcome": outcome}
    if outcome == "reschedule_requested":
        if "callback_request" not in args:
            raise ValueError("callback_request is required for reschedule_requested")
        callback_request = args["callback_request"]
        if not isinstance(callback_request, bool):
            raise ValueError("callback_request must be a boolean")
        canonical["callback_request"] = callback_request
    elif "callback_request" in args:
        raise ValueError("callback_request is only valid for reschedule_requested")
    return canonical


def validate_verification(args: dict[str, Any]) -> None:
    """Validate the privacy gate without mutating or releasing appointment facts."""
    if not isinstance(args, dict) or set(args) != {"recipient_confirmed"}:
        raise ValueError("recipient verification requires only recipient_confirmed")
    if args["recipient_confirmed"] is not True:
        raise ValueError("the named recipient has not clearly confirmed their identity")


def session_mode(
    context: ReminderContext = DEMO_REMINDER, *, voice: str = DEFAULT_VOICE
) -> dict[str, Any]:
    """A start-frame mode for either the Twilio host or generated simulation cases."""
    return {
        "kind": "dialt",
        "voice": voice,
        "instructions": instructions(context),
        "greeting": greeting(context),
        "tools": tool_manifest(),
        "end_call": True,
    }


@dataclass
class OutboundCallState:
    """In-memory illustration of the two host-bound tool contracts.

    The live Twilio host must use the pure validators above and commit verification/outcome in
    its durable attempt store atomically. This class is useful for focused unit tests and local
    callback simulations; its successful result is not evidence of durable persistence.
    """

    attempt_id: str
    reminder: ReminderContext = DEMO_REMINDER
    recipient_verified: bool = False
    recorded_args: dict[str, Any] | None = None
    opted_out: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("attempt_id must be non-empty text")
        if self.recorded_args is not None:
            self.recorded_args = validate_outcome(self.recorded_args)
            if self.recorded_args["outcome"] == "opted_out":
                self.opted_out = True
            if (
                self.recorded_args["outcome"] in {"confirmed", "reschedule_requested"}
                and not self.recipient_verified
            ):
                raise ValueError("a verified outcome requires recipient_verified=True")

    def verify_recipient(self, args: dict[str, Any]) -> dict[str, Any]:
        """Open the appointment-fact gate after explicit conversational confirmation."""
        validate_verification(args)
        if self.opted_out:
            raise ValueError("recipient verification cannot follow an opt-out")
        duplicate = self.recipient_verified
        if self.recorded_args is not None and not duplicate:
            raise ValueError("recipient verification cannot follow a terminal call outcome")
        if not duplicate:
            self.recipient_verified = True
            self.events.append(
                {"type": "outbound_recipient_verified", "attempt_id": self.attempt_id}
            )
        return {
            "verified": True,
            "duplicate": duplicate,
            "appointment_summary": self.reminder.appointment_summary,
        }

    def record_outcome(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate one primary outcome while always accepting a later opt-out.

        An exact repeat is safe and reports ``duplicate``. Anything that would revise the
        primary payload fails rather than silently overwriting application state. An opt-out
        is stored independently so a request made during the goodbye cannot be discarded.
        """
        canonical = validate_outcome(args)
        if canonical["outcome"] == "opted_out":
            duplicate = self.opted_out
            prior_outcome = (
                self.recorded_args["outcome"] if self.recorded_args is not None else None
            )
            if not duplicate:
                self.opted_out = True
                if self.recorded_args is None:
                    self.recorded_args = canonical
                self.events.append(
                    {"type": "outbound_call_opt_out", "attempt_id": self.attempt_id}
                )
            result = self._result(canonical, duplicate=duplicate)
            if prior_outcome not in {None, "opted_out"}:
                result["prior_outcome"] = prior_outcome
            return result

        if self.opted_out:
            raise ValueError("an opt-out is already recorded for this call")
        if canonical["outcome"] in {"confirmed", "reschedule_requested"}:
            if not self.recipient_verified:
                raise ValueError("recipient must be verified before recording this outcome")

        if self.recorded_args is not None:
            if canonical == self.recorded_args:
                return self._result(canonical, duplicate=True)
            raise ValueError("a different call outcome has already been recorded")

        self.recorded_args = canonical
        self.events.append(
            {"type": "outbound_call_outcome", "attempt_id": self.attempt_id, **canonical}
        )
        return self._result(canonical, duplicate=False)

    def _result(self, recorded: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "recorded": True,
            "outcome": recorded["outcome"],
            "duplicate": duplicate,
        }
        if recorded["outcome"] == "reschedule_requested":
            result["appointment_changed"] = False
            result["callback_requested"] = recorded["callback_request"]
            result["callback_scheduled"] = False
            if recorded.get("callback_request") is True:
                result["next_step"] = CALLBACK_REQUEST_RECORDED
            elif recorded.get("callback_request") is False:
                result["next_step"] = "No scheduling callback was requested."
        return result
