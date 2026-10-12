from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from dotenv import load_dotenv
from twilio.rest import Client

from settings import Settings
from store import CallAttempt, CallStore, OutboundCallRequest


REQUEST_FIELDS = {
    "idempotency_key",
    "to",
    "recipient_name",
    "recipient_timezone",
    "appointment_summary",
    "consent_reference",
}


def load_request(path: Path) -> OutboundCallRequest:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read outbound call request: {exc}") from exc
    if not isinstance(document, dict) or set(document) != REQUEST_FIELDS:
        raise ValueError(f"call request must contain exactly: {', '.join(sorted(REQUEST_FIELDS))}")
    if any(not isinstance(document[field], str) for field in REQUEST_FIELDS):
        raise ValueError("every outbound call request value must be a string")
    return OutboundCallRequest(**document)


def _required_text(value: str, name: str, *, maximum: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must be non-empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def validate_request(request: OutboundCallRequest, settings: Settings, store: CallStore, *,
                     now: datetime | None = None) -> OutboundCallRequest:
    idempotency_key = _required_text(
        request.idempotency_key, "idempotency_key", maximum=120
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", idempotency_key):
        raise ValueError("idempotency_key contains unsupported characters")

    try:
        parsed = phonenumbers.parse(request.to.strip(), None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("to must be a valid E.164 phone number") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("to must be a valid E.164 phone number")
    to_number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    if to_number != request.to.strip():
        raise ValueError("to must be written in canonical E.164 format")
    region = phonenumbers.region_code_for_number(parsed)
    if region not in settings.allowed_countries:
        raise ValueError(f"destination country {region or 'unknown'} is not allowlisted")
    if store.is_suppressed(to_number):
        raise ValueError("destination is suppressed by an opt-out or wrong-number outcome")
    if store.active_count() >= settings.max_active_calls:
        raise RuntimeError(f"active outbound-call limit reached ({settings.max_active_calls})")

    recipient_timezone = _required_text(
        request.recipient_timezone, "recipient_timezone", maximum=80
    )
    try:
        timezone = ZoneInfo(recipient_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("recipient_timezone must be an IANA timezone name") from exc
    current = (now or datetime.now(UTC)).astimezone(timezone)
    if not settings.calling_window_start <= current.time() < settings.calling_window_end:
        raise ValueError(
            "recipient local time is outside the configured outbound calling window "
            f"({settings.calling_window_start:%H:%M}-{settings.calling_window_end:%H:%M})"
        )

    return replace(
        request,
        idempotency_key=idempotency_key,
        to=to_number,
        recipient_name=_required_text(request.recipient_name, "recipient_name", maximum=120),
        recipient_timezone=recipient_timezone,
        appointment_summary=_required_text(
            request.appointment_summary, "appointment_summary", maximum=500
        ),
        consent_reference=_required_text(
            request.consent_reference, "consent_reference", maximum=200
        ),
    )


def existing_attempt(request: OutboundCallRequest, store: CallStore) -> CallAttempt | None:
    """Resolve a prior key before time/suppression checks; it can never authorize another call."""
    key = request.idempotency_key.strip()
    existing = store.get_by_idempotency_key(key)
    if existing is None:
        return None
    supplied = (
        request.to.strip(),
        request.recipient_name.strip(),
        request.recipient_timezone.strip(),
        request.appointment_summary.strip(),
        request.consent_reference.strip(),
    )
    reserved = (
        existing.to_number,
        existing.recipient_name,
        existing.recipient_timezone,
        existing.appointment_summary,
        existing.consent_reference,
    )
    if supplied != reserved:
        raise ValueError("idempotency key is already bound to a different call request")
    return existing


async def launch_call(request: OutboundCallRequest, settings: Settings, store: CallStore, *,
                      client: Any | None = None, now: datetime | None = None) -> tuple[CallAttempt, bool]:
    existing = existing_attempt(request, store)
    if existing is not None:
        return existing, False
    request = validate_request(request, settings, store, now=now)
    attempt, created = store.reserve(request, max_active_calls=settings.max_active_calls)
    if not created:
        return attempt, False

    answer_url = settings.http_url(f"/twilio/calls/{attempt.attempt_id}/answer")
    status_url = settings.http_url(f"/twilio/calls/{attempt.attempt_id}/status")
    fallback_url = settings.http_url(f"/twilio/calls/{attempt.attempt_id}/fallback")
    arguments: dict[str, Any] = {
        "to": attempt.to_number,
        "from_": settings.twilio_from_number,
        "url": answer_url,
        "method": "POST",
        "fallback_url": fallback_url,
        "fallback_method": "POST",
        "status_callback": status_url,
        "status_callback_method": "POST",
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
        "timeout": settings.ring_timeout_s,
        "record": False,
    }
    if settings.machine_detection == "human-only":
        arguments["machine_detection"] = "Enable"

    twilio = client or Client(settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        call = await asyncio.to_thread(twilio.calls.create, **arguments)
        call_sid = str(call.sid)
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
            raise RuntimeError("Twilio create-call response did not contain a valid CallSid")
    except Exception as exc:
        # A signed callback can win the race with a timed-out create response and bind the SID.
        # In that case the call is known to exist; never replace that evidence with "unknown".
        resolved = store.mark_launch_unknown(
            attempt.attempt_id, str(exc) or type(exc).__name__
        )
        if resolved.twilio_call_sid is not None:
            return resolved, True
        raise RuntimeError(
            "Twilio call creation had an unknown outcome; do not retry this idempotency key "
            "until the attempt is reconciled"
        ) from exc

    status = str(getattr(call, "status", "queued") or "queued")
    return store.bind_twilio_call(attempt.attempt_id, call_sid, status), True


def _summary(attempt: CallAttempt, *, created: bool | None = None) -> dict[str, Any]:
    number = attempt.to_number
    masked = f"{number[:3]}…{number[-2:]}" if len(number) > 5 else "…"
    result: dict[str, Any] = {
        "attempt_id": attempt.attempt_id,
        "destination": masked,
        "launch_status": attempt.launch_status,
        "call_status": attempt.call_status,
        "twilio_call_sid": attempt.twilio_call_sid,
    }
    if created is not None:
        result["created"] = created
    return result


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate one authorized outbound call; pass --place-call to contact Twilio."
    )
    parser.add_argument("request", type=Path, help="local JSON call request (keep it untracked)")
    parser.add_argument(
        "--place-call", action="store_true", help="perform the paid external Twilio call"
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).with_name(".env"))
    settings = Settings.from_env()
    store = CallStore(settings.state_db)
    store.initialize()
    request = load_request(args.request)
    existing = existing_attempt(request, store)
    normalized = request if existing is not None else validate_request(request, settings, store)

    if not args.place_call:
        print(json.dumps({
            "valid": True,
            "would_place_call": existing is None,
            "existing_attempt": _summary(existing) if existing else None,
            "note": "validation only; rerun with --place-call to contact Twilio",
        }, indent=2))
        return 0

    attempt, created = await launch_call(normalized, settings, store)
    print(json.dumps(_summary(attempt, created=created), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
