"""Authenticated Twilio callbacks and Media Stream host for one outbound Dialt call."""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from dialt import DialtMode
from dialt_recipes.twilio import (
    BridgeHooks,
    connect_stream_twiml,
    receive_stream_start,
    run_call_bridge,
    twilio_signature_is_valid,
)
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from settings import Settings
from store import CallAttempt, CallStore
from workflow import (
    CALLBACK_REQUEST_RECORDED,
    DEFAULT_VOICE,
    DEMO_REMINDER,
    ReminderContext,
    greeting,
    instructions,
    tool_manifest,
    validate_outcome,
    validate_verification,
)


logger = logging.getLogger("dialt_outbound_call")
HANGUP_TWIML = "<Response><Hangup /></Response>"
FALLBACK_TWIML = (
    "<Response><Say>Sorry, this automated call cannot continue right now.</Say><Hangup /></Response>"
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_store() -> CallStore:
    store = CallStore(get_settings().state_db)
    store.initialize()
    return store


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_store()
    yield


app = FastAPI(title="Dialt outbound Twilio call", lifespan=lifespan)


def _valid_http_signature(request: Request, form: dict[str, Any]) -> bool:
    if request.url.query:
        return False
    settings = get_settings()
    external_url = settings.http_url(request.url.path)
    return twilio_signature_is_valid(
        settings.twilio_auth_token,
        external_url,
        form,
        request.headers.get("x-twilio-signature"),
    )


def _attempt_for_callback(attempt_id: str, form: dict[str, Any]) -> tuple[CallAttempt, str]:
    settings = get_settings()
    attempt = get_store().get(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="unknown outbound call attempt")
    call_sid = str(form.get("CallSid") or "")
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
        raise HTTPException(status_code=400, detail="missing or invalid Twilio CallSid")
    expected = {
        "AccountSid": settings.twilio_account_sid,
        "From": settings.twilio_from_number,
        "To": attempt.to_number,
    }
    for field, value in expected.items():
        if form.get(field) != value:
            raise HTTPException(status_code=409, detail=f"Twilio {field} does not match the attempt")
    direction = form.get("Direction")
    if direction not in {None, "outbound-api"}:
        raise HTTPException(status_code=409, detail="Twilio callback is not for an outbound API call")
    if attempt.twilio_call_sid not in {None, call_sid}:
        raise HTTPException(status_code=409, detail="Twilio CallSid does not match the attempt")
    return attempt, call_sid


def _form_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@app.post("/twilio/calls/{attempt_id}/answer")
async def answer_call(attempt_id: str, request: Request) -> Response:
    form = dict(await request.form())
    if not _valid_http_signature(request, form):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    _, call_sid = _attempt_for_callback(attempt_id, form)
    answered_by = str(form.get("AnsweredBy") or "unknown")
    try:
        get_store().record_answer(attempt_id, call_sid, answered_by)
    except (KeyError, ValueError) as exc:
        raise _form_error(exc) from exc

    settings = get_settings()
    if settings.machine_detection == "human-only" and answered_by != "human":
        logger.info("Outbound call did not pass human-only AMD attempt_id=%s result=%s",
                    attempt_id, answered_by)
        return Response(HANGUP_TWIML, media_type="application/xml")

    twiml = connect_stream_twiml(
        settings,
        media_path="/twilio/media",
        status_path="/twilio/streams/status",
        stream_parameters={"attempt_id": attempt_id},
    )
    return Response(twiml, media_type="application/xml")


@app.post("/twilio/calls/{attempt_id}/status", status_code=204)
async def call_status(attempt_id: str, request: Request) -> Response:
    form = dict(await request.form())
    if not _valid_http_signature(request, form):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    _, call_sid = _attempt_for_callback(attempt_id, form)
    try:
        sequence = int(str(form.get("SequenceNumber", "")))
        get_store().record_status(attempt_id, call_sid, str(form.get("CallStatus") or ""), sequence)
    except (KeyError, ValueError) as exc:
        raise _form_error(exc) from exc
    return Response(status_code=204)


@app.post("/twilio/calls/{attempt_id}/fallback")
async def call_fallback(attempt_id: str, request: Request) -> Response:
    form = dict(await request.form())
    if not _valid_http_signature(request, form):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    _, call_sid = _attempt_for_callback(attempt_id, form)
    detail = str(form.get("ErrorCode") or form.get("ErrorUrl") or "Twilio TwiML fallback")
    try:
        get_store().record_stream(attempt_id, call_sid, "stream-error", detail)
    except (KeyError, ValueError) as exc:
        raise _form_error(exc) from exc
    return Response(FALLBACK_TWIML, media_type="application/xml")


@app.post("/twilio/streams/status", status_code=204)
async def stream_status(request: Request) -> Response:
    form = dict(await request.form())
    if not _valid_http_signature(request, form):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    settings = get_settings()
    if form.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(status_code=409, detail="Twilio AccountSid does not match")
    call_sid = str(form.get("CallSid") or "")
    attempt = get_store().get_by_twilio_call_sid(call_sid)
    if attempt is None:
        raise HTTPException(status_code=404, detail="unknown Twilio call")
    try:
        get_store().record_stream(
            attempt.attempt_id,
            call_sid,
            str(form.get("StreamEvent") or ""),
            str(form.get("StreamError") or "") or None,
        )
    except (KeyError, ValueError) as exc:
        raise _form_error(exc) from exc
    return Response(status_code=204)


@app.websocket("/twilio/media")
async def media_stream(websocket: WebSocket) -> None:
    settings = get_settings()
    if websocket.url.query:
        await websocket.close(code=1008)
        return
    if not twilio_signature_is_valid(
        settings.twilio_auth_token,
        settings.websocket_url("/twilio/media"),
        {},
        websocket.headers.get("x-twilio-signature"),
    ):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    attempt: CallAttempt | None = None
    try:
        start = await receive_stream_start(websocket)
        stream_sid = str(start["streamSid"])
        call_sid = str(start["callSid"])
        if not re.fullmatch(r"MZ[0-9a-fA-F]{32}", stream_sid):
            raise ValueError("invalid Twilio StreamSid")
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
            raise ValueError("invalid Twilio CallSid")
        if start.get("accountSid") != settings.twilio_account_sid:
            await _close_websocket(websocket, 1008)
            return
        parameters = start.get("customParameters")
        attempt_id = parameters.get("attempt_id") if isinstance(parameters, dict) else None
        attempt = get_store().get(str(attempt_id or ""))
        if attempt is None or attempt.twilio_call_sid != call_sid:
            await _close_websocket(websocket, 1008)
            return
        try:
            attempt = get_store().claim_media(
                attempt.attempt_id,
                call_sid,
                stream_sid,
                require_human=settings.machine_detection == "human-only",
            )
        except (KeyError, ValueError):
            logger.warning("Rejected ineligible outbound media stream attempt_id=%s", attempt.attempt_id)
            await _close_websocket(websocket, 1008)
            return
        await _run_bridge(websocket, stream_sid, call_sid, attempt, settings)
    except (KeyError, ValueError):
        logger.exception("Twilio sent an invalid outbound media start")
        await _close_websocket(websocket, 1003)
    except (WebSocketDisconnect, ConnectionError):
        return
    except Exception as exc:
        logger.exception("Outbound Twilio bridge failed attempt_id=%s",
                         attempt.attempt_id if attempt else "unknown")
        if attempt is not None and attempt.twilio_call_sid is not None:
            get_store().record_stream(
                attempt.attempt_id, attempt.twilio_call_sid, "stream-error", str(exc)
            )
        await _close_websocket(websocket, 1011)
    else:
        await _close_websocket(websocket, 1000)


async def _close_websocket(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass


async def _run_bridge(websocket: WebSocket, stream_sid: str, call_sid: str,
                      attempt: CallAttempt, settings: Settings) -> None:
    context = ReminderContext(
        business_name=DEMO_REMINDER.business_name,
        recipient_first_name=attempt.recipient_name,
        appointment_summary=attempt.appointment_summary,
    )
    mode = DialtMode(
        voice=settings.voice or DEFAULT_VOICE,
        instructions=instructions(context),
        greeting=greeting(context),
        tools=tool_manifest(),
    )

    async def execute_tool(name: str, args: dict[str, Any]) -> Any:
        if name == "verify_recipient":
            validate_verification(args)
            return get_store().verify_recipient(attempt.attempt_id)
        if name == "record_call_outcome":
            canonical = validate_outcome(args)
            outcome = str(canonical["outcome"])
            callback_requested = bool(canonical.get("callback_request", False))
            result = get_store().record_outcome(
                attempt.attempt_id,
                outcome=outcome,
                callback_requested=callback_requested,
            )
            if outcome == "reschedule_requested":
                result["appointment_changed"] = False
                result["callback_scheduled"] = False
                result["next_step"] = (
                    CALLBACK_REQUEST_RECORDED
                    if callback_requested else "No scheduling callback was requested."
                )
            return result
        raise RuntimeError(f"No handler configured for tool {name!r}")

    await run_call_bridge(
        websocket,
        stream_sid,
        call_sid,
        settings=settings,
        mode=mode,
        hooks=BridgeHooks(execute_tool=execute_tool),
    )
