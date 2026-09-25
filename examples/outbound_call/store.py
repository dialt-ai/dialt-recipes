from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TERMINAL_CALL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})
CALL_STATUSES = TERMINAL_CALL_STATUSES | {"queued", "initiated", "ringing", "in-progress"}
OUTCOMES = frozenset({"confirmed", "reschedule_requested", "wrong_number", "opted_out"})
STREAM_STATUS_RANK = {"stream-started": 0, "stream-stopped": 1, "stream-error": 2}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class OutboundCallRequest:
    idempotency_key: str
    to: str
    recipient_name: str
    recipient_timezone: str
    appointment_summary: str
    consent_reference: str


@dataclass(frozen=True)
class CallAttempt:
    attempt_id: str
    idempotency_key: str
    to_number: str
    recipient_name: str
    recipient_timezone: str
    appointment_summary: str
    consent_reference: str
    created_at: str
    updated_at: str
    twilio_call_sid: str | None = None
    launch_status: str = "reserved"
    call_status: str | None = None
    last_status_sequence: int = -1
    answered_by: str | None = None
    stream_status: str | None = None
    media_stream_sid: str | None = None
    recipient_verified: bool = False
    conversation_outcome: str | None = None
    callback_requested: bool = False
    opted_out: bool = False
    last_error: str | None = None


class CallStore:
    """Small durable reference store; replace it with application persistence in production."""

    def __init__(self, path: str | Path):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS call_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    to_number TEXT NOT NULL,
                    recipient_name TEXT NOT NULL,
                    recipient_timezone TEXT NOT NULL,
                    appointment_summary TEXT NOT NULL,
                    consent_reference TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    twilio_call_sid TEXT UNIQUE,
                    launch_status TEXT NOT NULL,
                    call_status TEXT,
                    last_status_sequence INTEGER NOT NULL DEFAULT -1,
                    answered_by TEXT,
                    stream_status TEXT,
                    media_stream_sid TEXT UNIQUE,
                    recipient_verified INTEGER NOT NULL DEFAULT 0,
                    conversation_outcome TEXT,
                    callback_requested INTEGER NOT NULL DEFAULT 0,
                    opted_out INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS suppressed_numbers (
                    to_number TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _attempt(row: sqlite3.Row) -> CallAttempt:
        values = dict(row)
        values["callback_requested"] = bool(values["callback_requested"])
        values["opted_out"] = bool(values["opted_out"])
        values["recipient_verified"] = bool(values["recipient_verified"])
        return CallAttempt(**values)

    def get(self, attempt_id: str) -> CallAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return self._attempt(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> CallAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._attempt(row) if row is not None else None

    def get_by_twilio_call_sid(self, call_sid: str) -> CallAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE twilio_call_sid = ?", (call_sid,)
            ).fetchone()
        return self._attempt(row) if row is not None else None

    def is_suppressed(self, to_number: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM suppressed_numbers WHERE to_number = ?", (to_number,)
            ).fetchone()
        return row is not None

    def active_count(self) -> int:
        placeholders = ",".join("?" for _ in TERMINAL_CALL_STATUSES)
        with self._connect() as connection:
            return int(connection.execute(
                f"""
                SELECT COUNT(*) FROM call_attempts
                WHERE launch_status IN ('reserved', 'unknown')
                   OR (call_status IS NULL OR call_status NOT IN ({placeholders}))
                """,
                tuple(TERMINAL_CALL_STATUSES),
            ).fetchone()[0])

    def suppress(self, to_number: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO suppressed_numbers (to_number, reason, created_at) VALUES (?, ?, ?)
                ON CONFLICT(to_number) DO UPDATE SET reason = excluded.reason
                """,
                (to_number, reason, _now()),
            )

    def reserve(self, request: OutboundCallRequest, *, max_active_calls: int) -> tuple[CallAttempt, bool]:
        """Reserve exactly once. A duplicate key returns its original attempt without redialing."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE idempotency_key = ?", (request.idempotency_key,)
            ).fetchone()
            if row is not None:
                attempt = self._attempt(row)
                expected = (
                    request.to,
                    request.recipient_name,
                    request.recipient_timezone,
                    request.appointment_summary,
                    request.consent_reference,
                )
                actual = (
                    attempt.to_number,
                    attempt.recipient_name,
                    attempt.recipient_timezone,
                    attempt.appointment_summary,
                    attempt.consent_reference,
                )
                if actual != expected:
                    raise ValueError("idempotency key is already bound to a different call request")
                return attempt, False

            suppressed = connection.execute(
                "SELECT 1 FROM suppressed_numbers WHERE to_number = ?", (request.to,)
            ).fetchone()
            if suppressed is not None:
                raise ValueError("destination is suppressed by an opt-out or wrong-number outcome")

            placeholders = ",".join("?" for _ in TERMINAL_CALL_STATUSES)
            active = connection.execute(
                f"""
                SELECT COUNT(*) FROM call_attempts
                WHERE launch_status IN ('reserved', 'unknown')
                   OR (call_status IS NULL OR call_status NOT IN ({placeholders}))
                """,
                tuple(TERMINAL_CALL_STATUSES),
            ).fetchone()[0]
            if active >= max_active_calls:
                raise RuntimeError(f"active outbound-call limit reached ({max_active_calls})")

            attempt_id = secrets.token_urlsafe(18)
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO call_attempts (
                    attempt_id, idempotency_key, to_number, recipient_name,
                    recipient_timezone, appointment_summary, consent_reference,
                    created_at, updated_at, launch_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
                """,
                (
                    attempt_id,
                    request.idempotency_key,
                    request.to,
                    request.recipient_name,
                    request.recipient_timezone,
                    request.appointment_summary,
                    request.consent_reference,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row), True

    @staticmethod
    def _bind_sid(connection: sqlite3.Connection, attempt_id: str, call_sid: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown outbound call attempt")
        existing = row["twilio_call_sid"]
        if existing is not None and existing != call_sid:
            raise ValueError("Twilio CallSid does not match the reserved attempt")
        if existing is None:
            connection.execute(
                "UPDATE call_attempts SET twilio_call_sid = ?, updated_at = ? WHERE attempt_id = ?",
                (call_sid, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return row

    def bind_twilio_call(self, attempt_id: str, call_sid: str, status: str = "queued") -> CallAttempt:
        if status not in CALL_STATUSES:
            status = "queued"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._bind_sid(connection, attempt_id, call_sid)
            effective_status = str(current["call_status"] or status)
            connection.execute(
                """
                UPDATE call_attempts
                SET launch_status = ?, call_status = COALESCE(call_status, ?), updated_at = ?
                WHERE attempt_id = ?
                """,
                (effective_status, effective_status, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def mark_launch_unknown(self, attempt_id: str, detail: str) -> CallAttempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown outbound call attempt")
            if row["twilio_call_sid"] is not None:
                return self._attempt(row)
            connection.execute(
                """
                UPDATE call_attempts SET launch_status = 'unknown', last_error = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (detail[:500], _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def record_status(self, attempt_id: str, call_sid: str, status: str, sequence: int) -> CallAttempt:
        if status not in CALL_STATUSES:
            raise ValueError(f"unsupported Twilio call status: {status}")
        if sequence < 0:
            raise ValueError("Twilio status sequence must be non-negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._bind_sid(connection, attempt_id, call_sid)
            previous_sequence = int(row["last_status_sequence"])
            if sequence < previous_sequence:
                return self._attempt(row)
            if sequence == previous_sequence:
                if row["call_status"] != status:
                    raise ValueError("one Twilio status sequence reported conflicting states")
                return self._attempt(row)
            connection.execute(
                """
                UPDATE call_attempts
                SET launch_status = ?, call_status = ?, last_status_sequence = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (status, status, sequence, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def record_answer(self, attempt_id: str, call_sid: str, answered_by: str | None) -> CallAttempt:
        classification = (answered_by or "unknown")[:80]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._bind_sid(connection, attempt_id, call_sid)
            if current["answered_by"] not in {None, classification}:
                raise ValueError("Twilio answer classification conflicts with the stored value")
            connection.execute(
                """
                UPDATE call_attempts SET answered_by = ?, updated_at = ? WHERE attempt_id = ?
                """,
                (classification, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def record_stream(self, attempt_id: str, call_sid: str, event: str,
                      error: str | None = None) -> CallAttempt:
        if event not in STREAM_STATUS_RANK:
            raise ValueError(f"unsupported Twilio stream event: {event}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._bind_sid(connection, attempt_id, call_sid)
            previous = current["stream_status"]
            if previous is not None and STREAM_STATUS_RANK[event] < STREAM_STATUS_RANK[previous]:
                return self._attempt(current)
            connection.execute(
                """
                UPDATE call_attempts SET stream_status = ?, last_error = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (event, (error or "")[:500] or None, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def claim_media(self, attempt_id: str, call_sid: str, stream_sid: str, *,
                    require_human: bool) -> CallAttempt:
        """Atomically admit one media stream for one eligible answered attempt."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._bind_sid(connection, attempt_id, call_sid)
            if row["answered_by"] is None:
                raise ValueError("outbound attempt has not passed through its answer webhook")
            if require_human and row["answered_by"] != "human":
                raise ValueError("outbound attempt did not pass human-only answer detection")
            if row["call_status"] in TERMINAL_CALL_STATUSES:
                raise ValueError("outbound attempt is already terminal")
            if row["conversation_outcome"] is not None or bool(row["opted_out"]):
                raise ValueError("outbound conversation is already complete")
            if row["media_stream_sid"] is not None:
                raise ValueError("a media stream has already claimed this outbound attempt")
            try:
                connection.execute(
                    """
                    UPDATE call_attempts SET media_stream_sid = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (stream_sid, _now(), attempt_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Twilio StreamSid is already bound to another attempt") from exc
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        assert row is not None
        return self._attempt(row)

    def verify_recipient(self, attempt_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown outbound call attempt")
            if bool(row["opted_out"]):
                raise ValueError("recipient verification cannot follow an opt-out")
            duplicate = bool(row["recipient_verified"])
            if row["conversation_outcome"] is not None and not duplicate:
                raise ValueError("recipient verification cannot follow a terminal call outcome")
            if not duplicate:
                connection.execute(
                    """
                    UPDATE call_attempts SET recipient_verified = 1, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (_now(), attempt_id),
                )
        return {
            "verified": True,
            "appointment_summary": row["appointment_summary"],
            "duplicate": duplicate,
        }

    def record_outcome(self, attempt_id: str, *, outcome: str,
                       callback_requested: bool) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"unsupported outbound call outcome: {outcome}")
        if not isinstance(callback_requested, bool):
            raise ValueError("callback_requested must be a boolean")
        if outcome != "reschedule_requested" and callback_requested:
            raise ValueError("callback_requested is only valid for reschedule_requested")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown outbound call attempt")
            if outcome in {"confirmed", "reschedule_requested"} and not row["recipient_verified"]:
                raise ValueError("recipient must be verified before recording this outcome")
            existing = row["conversation_outcome"]
            if outcome == "opted_out":
                connection.execute(
                    """
                    INSERT INTO suppressed_numbers (to_number, reason, created_at)
                    VALUES (?, 'opted_out', ?)
                    ON CONFLICT(to_number) DO UPDATE SET reason = excluded.reason
                    """,
                    (row["to_number"], _now()),
                )
                duplicate = bool(row["opted_out"])
                if not duplicate:
                    connection.execute(
                        """
                        UPDATE call_attempts
                        SET conversation_outcome = COALESCE(conversation_outcome, 'opted_out'),
                            opted_out = 1,
                            updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (_now(), attempt_id),
                    )
                result: dict[str, Any] = {
                    "recorded": True,
                    "outcome": "opted_out",
                    "callback_requested": False,
                    "duplicate": duplicate,
                }
                if existing not in {None, "opted_out"}:
                    result["prior_outcome"] = existing
                return result

            if bool(row["opted_out"]):
                raise ValueError("an opt-out is already recorded for this call")
            if existing is not None:
                same = (
                    existing == outcome
                    and bool(row["callback_requested"]) == callback_requested
                )
                if not same:
                    raise ValueError("call outcome is already recorded and cannot be overwritten")
                return {
                    "recorded": True,
                    "outcome": outcome,
                    "callback_requested": callback_requested,
                    "duplicate": True,
                }

            connection.execute(
                """
                UPDATE call_attempts
                SET conversation_outcome = ?, callback_requested = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (outcome, int(callback_requested), _now(), attempt_id),
            )
            if outcome == "wrong_number":
                connection.execute(
                    """
                    INSERT INTO suppressed_numbers (to_number, reason, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(to_number) DO UPDATE SET reason = excluded.reason
                    """,
                    (row["to_number"], outcome, _now()),
                )
        return {
            "recorded": True,
            "outcome": outcome,
            "callback_requested": callback_requested,
            "duplicate": False,
        }
