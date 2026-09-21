from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dialt_recipes import twilio as bridge
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator


EXAMPLE = Path(__file__).resolve().parent
if str(EXAMPLE) not in sys.path:
    sys.path.insert(0, str(EXAMPLE))

import dialer  # noqa: E402
import host  # noqa: E402
import workflow  # noqa: E402
from settings import Settings  # noqa: E402
from store import CallAttempt, CallStore, OutboundCallRequest  # noqa: E402


ACCOUNT_SID = "AC" + "1" * 32
CALL_SID = "CA" + "2" * 32
OTHER_CALL_SID = "CA" + "3" * 32
STREAM_SID = "MZ" + "4" * 32
FROM_NUMBER = "+14155550100"
TO_NUMBER = "+14155552671"
NOW_IN_WINDOW = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    settings = Settings(
        dialt_api_key="ck_test",
        twilio_auth_token="twilio-test-token",
        public_base_url="https://voice.example.com",
        twilio_account_sid=ACCOUNT_SID,
        twilio_from_number=FROM_NUMBER,
        state_db=tmp_path / "outbound.sqlite3",
        allowed_countries=("US",),
        calling_window_start=time(9, 0),
        calling_window_end=time(20, 0),
        max_active_calls=1,
    )
    return replace(settings, **overrides)


def make_request(key: str = "appointment-123-reminder-1", **overrides: Any) -> OutboundCallRequest:
    request = OutboundCallRequest(
        idempotency_key=key,
        to=TO_NUMBER,
        recipient_name="Taylor",
        recipient_timezone="America/New_York",
        appointment_summary="boiler service on January 20 at 10:30 a.m.",
        consent_reference="crm-consent-123",
    )
    return replace(request, **overrides)


@pytest.fixture
def store(tmp_path: Path) -> CallStore:
    result = CallStore(tmp_path / "calls.sqlite3")
    result.initialize()
    return result


def reserve_bound(store: CallStore, request: OutboundCallRequest | None = None) -> CallAttempt:
    attempt, created = store.reserve(request or make_request(), max_active_calls=2)
    assert created is True
    return store.bind_twilio_call(attempt.attempt_id, CALL_SID)


def set_base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = {
        "DIALT_API_KEY": "ck_test",
        "TWILIO_ACCOUNT_SID": ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": "twilio-test-token",
        "TWILIO_FROM_NUMBER": FROM_NUMBER,
        "PUBLIC_BASE_URL": "https://voice.example.com/",
        "OUTBOUND_STATE_DB": str(tmp_path / "calls.sqlite3"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "DIALT_URL",
        "DIALT_VOICE",
        "OUTBOUND_MACHINE_DETECTION",
        "OUTBOUND_RING_TIMEOUT_S",
        "OUTBOUND_ALLOWED_COUNTRIES",
        "OUTBOUND_CALLING_WINDOW_START",
        "OUTBOUND_CALLING_WINDOW_END",
        "OUTBOUND_MAX_ACTIVE_CALLS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_load_validated_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_base_env(monkeypatch, tmp_path)
    settings = Settings.from_env()

    assert settings.dialt_api_key == "ck_test"
    assert settings.twilio_account_sid == ACCOUNT_SID
    assert settings.twilio_from_number == FROM_NUMBER
    assert settings.public_base_url == "https://voice.example.com"
    assert settings.machine_detection == "human-only"
    assert settings.ring_timeout_s == 30
    assert settings.allowed_countries == ("US",)
    assert settings.calling_window_start == time(9, 0)
    assert settings.calling_window_end == time(20, 0)
    assert settings.max_active_calls == 1
    assert settings.state_db == tmp_path / "calls.sqlite3"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PUBLIC_BASE_URL", "http://voice.example.com", "absolute https"),
        ("PUBLIC_BASE_URL", "https://voice.example.com/callbacks", "origin without a path"),
        ("TWILIO_ACCOUNT_SID", "ACshort", "AC-prefixed"),
        ("TWILIO_FROM_NUMBER", "415-555-0100", "E.164"),
        ("OUTBOUND_MACHINE_DETECTION", "voicemail", "human-only or off"),
        ("OUTBOUND_RING_TIMEOUT_S", "9", "between 10 and 600"),
        ("OUTBOUND_RING_TIMEOUT_S", "not-an-integer", "must be integers"),
        ("OUTBOUND_MAX_ACTIVE_CALLS", "0", "between 1 and 100"),
        ("OUTBOUND_ALLOWED_COUNTRIES", "USA", "ISO alpha-2"),
        ("OUTBOUND_CALLING_WINDOW_START", "9am", "HH:MM"),
        ("OUTBOUND_CALLING_WINDOW_END", "08:59", "must start before"),
    ],
)
def test_settings_reject_unsafe_or_malformed_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


def test_settings_report_all_missing_required_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DIALT_API_KEY")
    monkeypatch.delenv("TWILIO_FROM_NUMBER")
    with pytest.raises(RuntimeError, match=r"DIALT_API_KEY.*TWILIO_FROM_NUMBER"):
        Settings.from_env()


def test_load_request_requires_an_exact_string_only_document(tmp_path: Path) -> None:
    path = tmp_path / "call.json"
    path.write_text(json.dumps(make_request().__dict__))
    assert dialer.load_request(path) == make_request()

    path.write_text(json.dumps({**make_request().__dict__, "campaign": "all"}))
    with pytest.raises(ValueError, match="contain exactly"):
        dialer.load_request(path)

    path.write_text(json.dumps({**make_request().__dict__, "recipient_name": 42}))
    with pytest.raises(ValueError, match="must be a string"):
        dialer.load_request(path)


def test_request_validation_normalizes_text_and_checks_local_time(
    tmp_path: Path, store: CallStore
) -> None:
    request = make_request(
        idempotency_key="  test-123  ",
        to=f" {TO_NUMBER} ",
        recipient_name=" Taylor ",
        recipient_timezone=" America/New_York ",
        appointment_summary=" service visit ",
        consent_reference=" consent-1 ",
    )
    result = dialer.validate_request(
        request, make_settings(tmp_path), store, now=NOW_IN_WINDOW
    )
    assert result == make_request(
        key="test-123",
        recipient_name="Taylor",
        appointment_summary="service visit",
        consent_reference="consent-1",
    )


@pytest.mark.parametrize(
    ("overrides", "now", "message"),
    [
        ({"idempotency_key": "contains spaces"}, NOW_IN_WINDOW, "unsupported characters"),
        ({"to": "4155552671"}, NOW_IN_WINDOW, "valid E.164"),
        ({"to": "+442079460958"}, NOW_IN_WINDOW, "not allowlisted"),
        ({"recipient_timezone": "Mars/Olympus"}, NOW_IN_WINDOW, "IANA timezone"),
        ({"consent_reference": "  "}, NOW_IN_WINDOW, "must be non-empty"),
        ({"appointment_summary": "x" * 501}, NOW_IN_WINDOW, "at most 500"),
        ({}, datetime(2026, 1, 15, 2, 0, tzinfo=UTC), "outside the configured"),
    ],
)
def test_request_validation_rejects_ineligible_calls(
    tmp_path: Path,
    store: CallStore,
    overrides: dict[str, Any],
    now: datetime,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        dialer.validate_request(
            make_request(**overrides), make_settings(tmp_path), store, now=now
        )


def test_request_validation_honors_durable_suppression(
    tmp_path: Path, store: CallStore
) -> None:
    store.suppress(TO_NUMBER, "opted_out")
    with pytest.raises(ValueError, match="suppressed"):
        dialer.validate_request(
            make_request(), make_settings(tmp_path), store, now=NOW_IN_WINDOW
        )


def test_ineligible_request_never_reaches_twilio(tmp_path: Path, store: CallStore) -> None:
    class MustNotCall:
        def create(self, **_: Any) -> None:
            raise AssertionError("Twilio must not be contacted for an ineligible request")

    store.suppress(TO_NUMBER, "opted_out")
    with pytest.raises(ValueError, match="suppressed"):
        asyncio.run(dialer.launch_call(
            make_request(), make_settings(tmp_path), store,
            client=SimpleNamespace(calls=MustNotCall()), now=NOW_IN_WINDOW,
        ))
    assert store.get_by_idempotency_key(make_request().idempotency_key) is None


def test_reservation_rechecks_suppression_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> None:
    original_validate = dialer.validate_request

    def suppress_after_validation(*args: Any, **kwargs: Any) -> OutboundCallRequest:
        normalized = original_validate(*args, **kwargs)
        store.suppress(normalized.to, "raced with validation")
        return normalized

    class MustNotCall:
        def create(self, **_: Any) -> None:
            raise AssertionError("Twilio must not be contacted after suppression wins the race")

    monkeypatch.setattr(dialer, "validate_request", suppress_after_validation)
    with pytest.raises(ValueError, match="suppressed"):
        asyncio.run(dialer.launch_call(
            make_request(), make_settings(tmp_path), store,
            client=SimpleNamespace(calls=MustNotCall()), now=NOW_IN_WINDOW,
        ))
    assert store.get_by_idempotency_key(make_request().idempotency_key) is None


def test_store_reservation_is_idempotent_and_rejects_conflicts(store: CallStore) -> None:
    request = make_request()
    first, created = store.reserve(request, max_active_calls=1)
    assert created is True

    duplicate, created = store.reserve(request, max_active_calls=1)
    assert created is False
    assert duplicate.attempt_id == first.attempt_id

    with pytest.raises(ValueError, match="different call request"):
        store.reserve(
            make_request(recipient_name="A different person"), max_active_calls=1
        )


def test_store_enforces_active_limit_but_releases_terminal_attempts(store: CallStore) -> None:
    first, _ = store.reserve(make_request("first"), max_active_calls=1)
    with pytest.raises(RuntimeError, match="active outbound-call limit"):
        store.reserve(make_request("second"), max_active_calls=1)

    store.bind_twilio_call(first.attempt_id, CALL_SID)
    store.record_status(first.attempt_id, CALL_SID, "completed", 4)
    second, created = store.reserve(make_request("second"), max_active_calls=1)
    assert created is True
    assert second.idempotency_key == "second"


def test_status_updates_are_ordered_idempotent_and_sid_bound(store: CallStore) -> None:
    attempt = reserve_bound(store)
    ringing = store.record_status(attempt.attempt_id, CALL_SID, "ringing", 2)
    assert ringing.call_status == "ringing" and ringing.last_status_sequence == 2

    stale = store.record_status(attempt.attempt_id, CALL_SID, "initiated", 1)
    assert stale.call_status == "ringing" and stale.last_status_sequence == 2
    duplicate = store.record_status(attempt.attempt_id, CALL_SID, "ringing", 2)
    assert duplicate == stale

    with pytest.raises(ValueError, match="conflicting states"):
        store.record_status(attempt.attempt_id, CALL_SID, "completed", 2)
    with pytest.raises(ValueError, match="does not match"):
        store.record_status(attempt.attempt_id, OTHER_CALL_SID, "completed", 3)

    completed = store.record_status(attempt.attempt_id, CALL_SID, "completed", 3)
    assert completed.call_status == "completed" and completed.launch_status == "completed"


def test_store_verification_gates_and_idempotently_records_outcomes(store: CallStore) -> None:
    attempt, _ = store.reserve(make_request(), max_active_calls=1)
    with pytest.raises(ValueError, match="must be verified"):
        store.record_outcome(attempt.attempt_id, outcome="confirmed", callback_requested=False)

    first_verification = store.verify_recipient(attempt.attempt_id)
    second_verification = store.verify_recipient(attempt.attempt_id)
    assert first_verification == {
        "verified": True,
        "appointment_summary": attempt.appointment_summary,
        "duplicate": False,
    }
    assert second_verification["duplicate"] is True

    first = store.record_outcome(
        attempt.attempt_id, outcome="reschedule_requested", callback_requested=True
    )
    duplicate = store.record_outcome(
        attempt.attempt_id, outcome="reschedule_requested", callback_requested=True
    )
    assert first == {
        "recorded": True,
        "outcome": "reschedule_requested",
        "callback_requested": True,
        "duplicate": False,
    }
    assert duplicate["duplicate"] is True
    with pytest.raises(ValueError, match="cannot be overwritten"):
        store.record_outcome(
            attempt.attempt_id, outcome="confirmed", callback_requested=False
        )


def test_late_opt_out_is_never_lost_after_a_primary_outcome(store: CallStore) -> None:
    attempt, _ = store.reserve(make_request(), max_active_calls=1)
    store.verify_recipient(attempt.attempt_id)
    store.record_outcome(
        attempt.attempt_id, outcome="confirmed", callback_requested=False
    )

    result = store.record_outcome(
        attempt.attempt_id, outcome="opted_out", callback_requested=False
    )
    assert result == {
        "recorded": True,
        "outcome": "opted_out",
        "callback_requested": False,
        "duplicate": False,
        "prior_outcome": "confirmed",
    }
    updated = store.get(attempt.attempt_id)
    assert updated is not None
    assert updated.conversation_outcome == "confirmed"
    assert updated.opted_out is True
    assert store.is_suppressed(TO_NUMBER) is True
    assert store.record_outcome(
        attempt.attempt_id, outcome="opted_out", callback_requested=False
    )["duplicate"] is True


def test_workflow_validators_enforce_the_model_facing_tool_contracts() -> None:
    workflow.validate_verification({"recipient_confirmed": True})
    for invalid in (
        {"recipient_confirmed": False},
        {"recipient_confirmed": True, "attempt_id": "attacker-selected"},
        {},
        "yes",
    ):
        with pytest.raises(ValueError):
            workflow.validate_verification(invalid)  # type: ignore[arg-type]

    assert workflow.validate_outcome({"outcome": "confirmed"}) == {
        "outcome": "confirmed"
    }
    assert workflow.validate_outcome({
        "outcome": "reschedule_requested", "callback_request": False,
    }) == {"outcome": "reschedule_requested", "callback_request": False}
    for invalid in (
        {"outcome": "reschedule_requested"},
        {"outcome": "confirmed", "callback_request": True},
        {"outcome": "reschedule_requested", "callback_request": "yes"},
        {"outcome": "confirmed", "to": "+19999999999"},
        {"outcome": "invented"},
    ):
        with pytest.raises(ValueError):
            workflow.validate_outcome(invalid)

    manifests = {tool["name"]: tool for tool in workflow.tool_manifest()}
    assert set(manifests) == {"verify_recipient", "record_call_outcome"}
    all_properties = {
        property_name
        for tool in manifests.values()
        for property_name in tool["parameters"]["properties"]
    }
    assert all_properties == {"recipient_confirmed", "outcome", "callback_request"}


def test_workflow_state_gates_facts_and_accepts_only_one_terminal_outcome() -> None:
    state = workflow.OutboundCallState("attempt-1")
    with pytest.raises(ValueError, match="must be verified"):
        state.record_outcome({"outcome": "confirmed"})

    first = state.verify_recipient({"recipient_confirmed": True})
    duplicate = state.verify_recipient({"recipient_confirmed": True})
    assert first["appointment_summary"] == workflow.DEMO_REMINDER.appointment_summary
    assert first["duplicate"] is False and duplicate["duplicate"] is True

    recorded = state.record_outcome({
        "outcome": "reschedule_requested", "callback_request": False,
    })
    assert recorded["appointment_changed"] is False
    assert recorded["next_step"] == "No scheduling callback was requested."
    assert state.record_outcome({
        "outcome": "reschedule_requested", "callback_request": False,
    })["duplicate"] is True
    with pytest.raises(ValueError, match="different call outcome"):
        state.record_outcome({"outcome": "confirmed"})

    opted_out = state.record_outcome({"outcome": "opted_out"})
    assert opted_out["prior_outcome"] == "reschedule_requested"
    assert state.opted_out is True
    assert state.recorded_args == {
        "outcome": "reschedule_requested", "callback_request": False,
    }
    assert state.record_outcome({"outcome": "opted_out"})["duplicate"] is True
    with pytest.raises(ValueError, match="opt-out"):
        state.record_outcome({"outcome": "confirmed"})


@pytest.mark.parametrize("outcome", ["opted_out", "wrong_number"])
def test_suppression_outcomes_block_future_calls(store: CallStore, outcome: str) -> None:
    attempt, _ = store.reserve(make_request(outcome), max_active_calls=2)
    result = store.record_outcome(
        attempt.attempt_id, outcome=outcome, callback_requested=False
    )
    assert result["recorded"] is True
    assert store.is_suppressed(TO_NUMBER) is True


def test_launch_call_uses_exact_safe_twilio_arguments(tmp_path: Path, store: CallStore) -> None:
    calls: list[dict[str, Any]] = []

    class FakeCalls:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(sid=CALL_SID, status="queued")

    settings = make_settings(tmp_path, ring_timeout_s=42)
    attempt, created = asyncio.run(
        dialer.launch_call(
            make_request(), settings, store, client=SimpleNamespace(calls=FakeCalls()),
            now=NOW_IN_WINDOW,
        )
    )

    assert created is True
    assert calls == [{
        "to": TO_NUMBER,
        "from_": FROM_NUMBER,
        "url": settings.http_url(f"/twilio/calls/{attempt.attempt_id}/answer"),
        "method": "POST",
        "fallback_url": settings.http_url(f"/twilio/calls/{attempt.attempt_id}/fallback"),
        "fallback_method": "POST",
        "status_callback": settings.http_url(f"/twilio/calls/{attempt.attempt_id}/status"),
        "status_callback_method": "POST",
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
        "timeout": 42,
        "record": False,
        "machine_detection": "Enable",
    }]
    assert attempt.twilio_call_sid == CALL_SID


def test_launch_call_never_retries_an_unknown_create_outcome(
    tmp_path: Path, store: CallStore
) -> None:
    count = 0

    class FailingCalls:
        def create(self, **_: Any) -> None:
            nonlocal count
            count += 1
            raise TimeoutError("response lost")

    settings = make_settings(tmp_path)
    client = SimpleNamespace(calls=FailingCalls())
    with pytest.raises(RuntimeError, match="unknown outcome; do not retry"):
        asyncio.run(
            dialer.launch_call(
                make_request(), settings, store, client=client, now=NOW_IN_WINDOW
            )
        )

    unknown = store.get_by_idempotency_key(make_request().idempotency_key)
    assert unknown is not None
    assert unknown.launch_status == "unknown"
    assert unknown.twilio_call_sid is None
    assert unknown.last_error == "response lost"

    store.suppress(TO_NUMBER, "manual test suppression")
    same, created = asyncio.run(
        dialer.launch_call(
            make_request(), settings, store, client=client,
            now=datetime(2026, 1, 15, 2, 0, tzinfo=UTC),
        )
    )
    assert created is False and same.attempt_id == unknown.attempt_id
    assert count == 1


def test_launch_call_preserves_a_callback_that_wins_the_create_error_race(
    tmp_path: Path, store: CallStore
) -> None:
    class CallbackThenFailure:
        def create(self, **_: Any) -> None:
            reserved = store.get_by_idempotency_key(make_request().idempotency_key)
            assert reserved is not None
            store.bind_twilio_call(reserved.attempt_id, CALL_SID, "ringing")
            raise TimeoutError("the response arrived after Twilio's callback")

    attempt, created = asyncio.run(
        dialer.launch_call(
            make_request(), make_settings(tmp_path), store,
            client=SimpleNamespace(calls=CallbackThenFailure()), now=NOW_IN_WINDOW,
        )
    )
    assert created is True
    assert attempt.twilio_call_sid == CALL_SID
    assert attempt.call_status == "ringing"
    assert attempt.launch_status == "ringing"
    assert attempt.last_error is None


def test_mark_launch_unknown_atomically_preserves_a_bound_callback(store: CallStore) -> None:
    attempt = reserve_bound(store)
    resolved = store.mark_launch_unknown(attempt.attempt_id, "late create error")
    assert resolved.twilio_call_sid == CALL_SID
    assert resolved.call_status == "queued"
    assert resolved.launch_status == "queued"
    assert resolved.last_error is None


@pytest.mark.parametrize("place_call", [False, True])
def test_cli_contacts_twilio_only_with_explicit_place_call_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    place_call: bool,
) -> None:
    settings = make_settings(tmp_path)
    request = make_request()
    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(sid=CALL_SID, status="queued")

    original_validate = dialer.validate_request
    monkeypatch.setattr(dialer, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(dialer, "load_request", lambda _: request)
    monkeypatch.setattr(dialer, "validate_request", lambda req, cfg, db, **kwargs:
                        original_validate(req, cfg, db, now=NOW_IN_WINDOW))
    monkeypatch.setattr(dialer, "Client", lambda *_:
                        SimpleNamespace(calls=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(sys, "argv", ["dialer.py", "unused-call.json"] +
                        (["--place-call"] if place_call else []))

    assert asyncio.run(dialer._main()) == 0
    result = json.loads(capsys.readouterr().out)
    persisted = CallStore(settings.state_db).get_by_idempotency_key(request.idempotency_key)
    if place_call:
        assert len(calls) == 1 and calls[0]["to"] == TO_NUMBER
        assert result["created"] is True and result["twilio_call_sid"] == CALL_SID
        assert persisted is not None and persisted.twilio_call_sid == CALL_SID
    else:
        assert calls == []
        assert result["valid"] is True and result["would_place_call"] is True
        assert persisted is None


def callback_form(attempt: CallAttempt, **updates: str) -> dict[str, str]:
    form = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": attempt.twilio_call_sid or CALL_SID,
        "From": FROM_NUMBER,
        "To": attempt.to_number,
        "Direction": "outbound-api",
    }
    form.update(updates)
    return form


def signed_post(
    client: TestClient,
    settings: Settings,
    path: str,
    form: dict[str, str],
    *,
    valid_signature: bool = True,
):
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(
        settings.http_url(path), form
    )
    if not valid_signature:
        signature = "invalid"
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


@pytest.fixture
def hosted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> tuple[TestClient, Settings, CallStore]:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    monkeypatch.setattr(host, "get_store", lambda: store)
    with TestClient(host.app) as client:
        yield client, settings, store


def test_signed_answer_connects_human_with_only_an_opaque_stream_parameter(hosted) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)
    path = f"/twilio/calls/{attempt.attempt_id}/answer"
    response = signed_post(
        client, settings, path, callback_form(attempt, AnsweredBy="human")
    )

    assert response.status_code == 200
    assert f'<Parameter name="attempt_id" value="{attempt.attempt_id}" />' in response.text
    assert 'url="wss://voice.example.com/twilio/media"' in response.text
    assert attempt.appointment_summary not in response.text
    assert attempt.recipient_name not in response.text
    assert attempt.to_number not in response.text
    assert store.get(attempt.attempt_id).answered_by == "human"  # type: ignore[union-attr]


@pytest.mark.parametrize("answered_by", ["machine_start", "fax", "unknown", ""])
def test_human_only_amd_hangs_up_every_nonhuman_answer(hosted, answered_by: str) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store, make_request(f"amd-{answered_by or 'missing'}"))
    path = f"/twilio/calls/{attempt.attempt_id}/answer"
    form = callback_form(attempt)
    if answered_by:
        form["AnsweredBy"] = answered_by
    response = signed_post(client, settings, path, form)
    assert response.status_code == 200
    assert response.text == host.HANGUP_TWIML
    assert "<Stream" not in response.text


def test_signed_status_and_fallback_update_only_the_bound_attempt(hosted) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)

    status_path = f"/twilio/calls/{attempt.attempt_id}/status"
    status_response = signed_post(
        client,
        settings,
        status_path,
        callback_form(attempt, CallStatus="ringing", SequenceNumber="2"),
    )
    assert status_response.status_code == 204
    assert store.get(attempt.attempt_id).call_status == "ringing"  # type: ignore[union-attr]

    fallback_path = f"/twilio/calls/{attempt.attempt_id}/fallback"
    fallback_response = signed_post(
        client,
        settings,
        fallback_path,
        callback_form(attempt, ErrorCode="11200"),
    )
    assert fallback_response.status_code == 200
    assert fallback_response.text == host.FALLBACK_TWIML
    updated = store.get(attempt.attempt_id)
    assert updated is not None
    assert updated.stream_status == "stream-error" and updated.last_error == "11200"


def test_signed_stream_status_resolves_by_twilio_call_sid(hosted) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)
    path = "/twilio/streams/status"
    form = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "StreamEvent": "stream-error",
        "StreamError": "31951",
    }
    response = signed_post(client, settings, path, form)
    assert response.status_code == 204
    updated = store.get(attempt.attempt_id)
    assert updated is not None
    assert updated.stream_status == "stream-error" and updated.last_error == "31951"


@pytest.mark.parametrize("route", ["answer", "status", "fallback", "stream"])
def test_every_http_callback_rejects_an_invalid_signature(hosted, route: str) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)
    if route == "stream":
        path = "/twilio/streams/status"
        form = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID,
                "StreamEvent": "stream-started"}
    else:
        path = f"/twilio/calls/{attempt.attempt_id}/{route}"
        form = callback_form(attempt, AnsweredBy="human", CallStatus="ringing",
                             SequenceNumber="1")
    response = signed_post(client, settings, path, form, valid_signature=False)
    assert response.status_code == 403


def test_http_callbacks_reject_query_strings_even_when_the_signature_matches(hosted) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)
    path = f"/twilio/calls/{attempt.attempt_id}/answer?unexpected=value"
    response = signed_post(
        client, settings, path, callback_form(attempt, AnsweredBy="human")
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"AccountSid": "AC" + "9" * 32}, "AccountSid"),
        ({"From": "+14155550199"}, "From"),
        ({"To": "+14155550199"}, "To"),
        ({"Direction": "inbound"}, "not for an outbound"),
        ({"CallSid": OTHER_CALL_SID}, "CallSid"),
    ],
)
def test_signed_callbacks_reject_attempt_metadata_mismatches(
    hosted, updates: dict[str, str], message: str
) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)
    path = f"/twilio/calls/{attempt.attempt_id}/answer"
    response = signed_post(
        client, settings, path, callback_form(attempt, AnsweredBy="human", **updates)
    )
    assert response.status_code == 409
    assert message in response.text


def test_signed_callbacks_reject_unknown_attempts_and_calls(hosted) -> None:
    client, settings, store = hosted
    attempt = reserve_bound(store)

    unknown_path = "/twilio/calls/not-an-attempt/answer"
    response = signed_post(
        client, settings, unknown_path, callback_form(attempt, AnsweredBy="human")
    )
    assert response.status_code == 404

    stream_path = "/twilio/streams/status"
    stream_form = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": OTHER_CALL_SID,
        "StreamEvent": "stream-started",
    }
    response = signed_post(client, settings, stream_path, stream_form)
    assert response.status_code == 404


class FakeMediaWebSocket:
    def __init__(self, signature: str, start: dict[str, Any], *, query: str = ""):
        self.headers = {"x-twilio-signature": signature}
        self.url = SimpleNamespace(query=query)
        self.start = start
        self.accepted = False
        self.closed: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.closed.append(code)

    async def receive_text(self) -> str:
        return json.dumps({"event": "start", "start": self.start})


def media_signature(settings: Settings) -> str:
    return RequestValidator(settings.twilio_auth_token).compute_signature(
        settings.websocket_url("/twilio/media"), {}
    )


def test_media_websocket_requires_a_signature_and_a_bound_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> None:
    settings = make_settings(tmp_path)
    attempt = reserve_bound(store)
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    monkeypatch.setattr(host, "get_store", lambda: store)

    invalid = FakeMediaWebSocket("invalid", {})
    asyncio.run(host.media_stream(invalid))  # type: ignore[arg-type]
    assert invalid.accepted is False and invalid.closed == [1008]

    mismatch = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": ACCOUNT_SID, "streamSid": STREAM_SID, "callSid": OTHER_CALL_SID,
         "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(mismatch))  # type: ignore[arg-type]
    assert mismatch.accepted is True and mismatch.closed == [1008]


def test_media_websocket_routes_one_valid_stream_to_the_bound_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> None:
    settings = make_settings(tmp_path)
    attempt = reserve_bound(store)
    attempt = store.record_answer(attempt.attempt_id, CALL_SID, "human")
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    monkeypatch.setattr(host, "get_store", lambda: store)
    seen: list[tuple[Any, ...]] = []

    async def fake_run_bridge(*args: Any) -> None:
        seen.append(args)

    monkeypatch.setattr(host, "_run_bridge", fake_run_bridge)
    websocket = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": ACCOUNT_SID, "streamSid": STREAM_SID, "callSid": CALL_SID,
         "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(websocket))  # type: ignore[arg-type]

    assert websocket.accepted is True and websocket.closed == [1000]
    assert len(seen) == 1
    assert seen[0][:3] == (websocket, STREAM_SID, CALL_SID)
    claimed = seen[0][3]
    assert claimed.attempt_id == attempt.attempt_id
    assert claimed.media_stream_sid == STREAM_SID
    assert seen[0][4] is settings


def test_media_websocket_rejects_queries_wrong_accounts_and_duplicate_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> None:
    settings = make_settings(tmp_path)
    attempt = reserve_bound(store)
    store.record_answer(attempt.attempt_id, CALL_SID, "human")
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    monkeypatch.setattr(host, "get_store", lambda: store)
    bridges: list[str] = []

    async def fake_run_bridge(*args: Any) -> None:
        bridges.append(args[1])

    monkeypatch.setattr(host, "_run_bridge", fake_run_bridge)
    query = FakeMediaWebSocket(media_signature(settings), {}, query="unexpected=value")
    asyncio.run(host.media_stream(query))  # type: ignore[arg-type]
    assert query.accepted is False and query.closed == [1008]

    wrong_account = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": "AC" + "9" * 32, "streamSid": STREAM_SID, "callSid": CALL_SID,
         "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(wrong_account))  # type: ignore[arg-type]
    assert wrong_account.accepted is True and wrong_account.closed == [1008]

    first = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": ACCOUNT_SID, "streamSid": STREAM_SID, "callSid": CALL_SID,
         "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(first))  # type: ignore[arg-type]
    assert first.closed == [1000]

    second = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": ACCOUNT_SID, "streamSid": "MZ" + "5" * 32,
         "callSid": CALL_SID, "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(second))  # type: ignore[arg-type]
    assert second.closed == [1008]
    assert bridges == [STREAM_SID]


@pytest.mark.parametrize(
    ("answered_by", "terminal"),
    [("machine_start", False), ("human", True)],
)
def test_media_websocket_rejects_nonhuman_and_terminal_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: CallStore,
    answered_by: str,
    terminal: bool,
) -> None:
    settings = make_settings(tmp_path)
    attempt = reserve_bound(store)
    store.record_answer(attempt.attempt_id, CALL_SID, answered_by)
    if terminal:
        store.record_status(attempt.attempt_id, CALL_SID, "completed", 1)
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    monkeypatch.setattr(host, "get_store", lambda: store)

    async def must_not_run(*_: Any) -> None:
        raise AssertionError("an ineligible media stream must not start Dialt")

    monkeypatch.setattr(host, "_run_bridge", must_not_run)
    websocket = FakeMediaWebSocket(
        media_signature(settings),
        {"accountSid": ACCOUNT_SID, "streamSid": STREAM_SID, "callSid": CALL_SID,
         "customParameters": {"attempt_id": attempt.attempt_id}},
    )
    asyncio.run(host.media_stream(websocket))  # type: ignore[arg-type]
    assert websocket.accepted is True and websocket.closed == [1008]


@pytest.mark.parametrize("callback_requested", [True, False])
def test_bridge_builds_private_call_context_and_routes_tools_to_the_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore,
    callback_requested: bool,
) -> None:
    settings = make_settings(tmp_path, voice="custom_voice")
    attempt = reserve_bound(store)
    monkeypatch.setattr(host, "get_store", lambda: store)
    captured: dict[str, Any] = {}

    async def fake_call_bridge(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(host, "run_call_bridge", fake_call_bridge)
    websocket = object()
    asyncio.run(host._run_bridge(websocket, STREAM_SID, CALL_SID, attempt, settings))

    mode = captured["mode"]
    assert captured["args"] == (websocket, STREAM_SID, CALL_SID)
    assert captured["settings"] is settings
    assert mode.voice == "custom_voice"
    assert attempt.recipient_name in mode.greeting
    assert attempt.appointment_summary not in mode.greeting
    assert attempt.appointment_summary not in mode.instructions
    assert [tool["name"] for tool in mode.tools] == [
        "verify_recipient", "record_call_outcome"
    ]

    execute_tool = captured["hooks"].execute_tool
    with pytest.raises(ValueError, match="not clearly confirmed"):
        asyncio.run(execute_tool("verify_recipient", {"recipient_confirmed": False}))

    verified = asyncio.run(
        execute_tool("verify_recipient", {"recipient_confirmed": True})
    )
    assert verified["appointment_summary"] == attempt.appointment_summary
    outcome = asyncio.run(execute_tool(
        "record_call_outcome",
        {"outcome": "reschedule_requested", "callback_request": callback_requested},
    ))
    assert outcome["recorded"] is True
    assert outcome["appointment_changed"] is False
    assert outcome["callback_requested"] is callback_requested
    assert outcome["callback_scheduled"] is False
    expected_next_step = (
        "Your callback request was recorded, but no callback has been scheduled."
        if callback_requested else "No scheduling callback was requested."
    )
    assert outcome["next_step"] == expected_next_step
    assert "within one business day" not in mode.instructions
    saved = store.get(attempt.attempt_id)
    assert saved is not None and saved.callback_requested is callback_requested
    simulation = workflow.OutboundCallState(attempt.attempt_id)
    simulation.verify_recipient({"recipient_confirmed": True})
    assert simulation.record_outcome({
        "outcome": "reschedule_requested", "callback_request": callback_requested,
    }) == outcome
    with pytest.raises(RuntimeError, match="No handler configured"):
        asyncio.run(execute_tool("place_another_call", {"to": "+19999999999"}))


def test_bridge_emits_a_failed_tool_result_for_a_durable_outcome_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CallStore
) -> None:
    settings = make_settings(tmp_path)
    attempt = reserve_bound(store)
    store.verify_recipient(attempt.attempt_id)
    monkeypatch.setattr(host, "get_store", lambda: store)

    def fail_write(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("temporary storage failure")

    monkeypatch.setattr(store, "record_outcome", fail_write)
    result_sent = asyncio.Event()

    class FakeSession:
        def __init__(self) -> None:
            self.results: list[tuple[str, Any, str, bool]] = []

        async def events(self):
            yield SimpleNamespace(
                type="tool_call",
                data={"id": "outcome-1", "name": "record_call_outcome",
                      "args": {"outcome": "confirmed"}},
                audio=None,
            )
            await result_sent.wait()

        async def send_tool_result(
            self, tool_id: str, result: Any, *, outcome: str, verified: bool
        ) -> None:
            self.results.append((tool_id, result, outcome, verified))
            result_sent.set()

    session = FakeSession()

    class FakeConnection:
        async def __aenter__(self) -> FakeSession:
            return session

        async def __aexit__(self, *_: Any) -> None:
            return None

    async def connect(*_: Any, **__: Any) -> FakeConnection:
        return FakeConnection()

    class QuietWebSocket:
        async def iter_text(self):
            await asyncio.Event().wait()
            yield ""  # pragma: no cover - keeps this an async generator

        async def send_json(self, _: dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(bridge.DialtSession, "connect", connect)
    asyncio.run(
        host._run_bridge(QuietWebSocket(), STREAM_SID, CALL_SID, attempt, settings)
    )

    assert session.results == [(
        "outcome-1",
        {"error": "tool_failed", "detail": "temporary storage failure"},
        "failed",
        False,
    )]
    assert store.get(attempt.attempt_id).conversation_outcome is None  # type: ignore[union-attr]


def test_generated_evals_embed_the_authoritative_workflow() -> None:
    expected_files = {
        "confirmed.json",
        "reschedule_requested.json",
        "wrong_number.json",
        "opt_out_before_verification.json",
        "opt_out_after_verification.json",
        "ambiguous_identity.json",
        "outcome_storage_failure.json",
    }
    files = {path.name for path in (EXAMPLE / "evals").glob("*.json")}
    assert files == expected_files

    expected_mode = workflow.session_mode()
    expected_tools = {tool["name"] for tool in expected_mode["tools"]}
    expected_checks = {
        "confirmed.json": {("tool_called", "verify_recipient"),
                           ("tool_called", "record_call_outcome")},
        "reschedule_requested.json": {("tool_called", "verify_recipient"),
                                      ("tool_called", "record_call_outcome")},
        "wrong_number.json": {("tool_not_called", "verify_recipient"),
                              ("tool_called", "record_call_outcome")},
        "opt_out_before_verification.json": {("tool_not_called", "verify_recipient"),
                                             ("tool_called", "record_call_outcome")},
        "opt_out_after_verification.json": {("tool_called", "verify_recipient"),
                                            ("tool_called", "record_call_outcome")},
        "ambiguous_identity.json": {("tool_not_called", "verify_recipient"),
                                    ("tool_not_called", "record_call_outcome")},
        "outcome_storage_failure.json": {("tool_called", "verify_recipient"),
                                         ("tool_called", "record_call_outcome")},
    }
    for filename in sorted(expected_files):
        document = json.loads((EXAMPLE / "evals" / filename).read_text())
        assert document["starter"] == ""
        assert document["target"] == expected_mode
        assert set(document["fixtures"]) == expected_tools
        checks = {(check["type"], check.get("value")) for check in document["checks"]}
        assert checks >= expected_checks[filename]
        assert any(check["type"] == "judge" for check in document["checks"])

    outcomes = {
        filename: json.loads((EXAMPLE / "evals" / filename).read_text())["fixtures"]
        ["record_call_outcome"]["result"].get("outcome")
        for filename in expected_files
    }
    assert outcomes == {
        "confirmed.json": "confirmed",
        "reschedule_requested.json": "reschedule_requested",
        "wrong_number.json": "wrong_number",
        "opt_out_before_verification.json": "opted_out",
        "opt_out_after_verification.json": "opted_out",
        "ambiguous_identity.json": "confirmed",
        "outcome_storage_failure.json": None,
    }
    failure_fixture = json.loads(
        (EXAMPLE / "evals" / "outcome_storage_failure.json").read_text()
    )["fixtures"]["record_call_outcome"]["result"]
    assert failure_fixture == {
        "error": "tool_failed",
        "detail": "temporary storage failure",
    }
