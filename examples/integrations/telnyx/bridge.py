"""Inbound Telnyx Call Control bridge. Application tools and policy belong here."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dialt import DEFAULT_REALTIME_URL, DialtMode
from dialt_recipes.telnyx import TelnyxMediaSocket, receive_stream_start, signature_is_valid
from dialt_recipes.twilio import BridgeHooks, run_call_bridge
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

logger = logging.getLogger("dialt_telnyx")


@dataclass(frozen=True)
class Settings:
    dialt_api_key: str
    telnyx_api_key: str
    telnyx_public_key: str
    public_base_url: str
    instructions: str
    dialt_url: str = DEFAULT_REALTIME_URL
    voice: str | None = None
    greeting: str | bool | None = None

    @property
    def stream_url(self) -> str:
        return self.public_base_url.replace("https://", "wss://", 1) + "/media"

    @classmethod
    def from_env(cls) -> Settings:
        required = ("DIALT_API_KEY", "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "PUBLIC_BASE_URL")
        values = {name: os.environ.get(name, "").strip() for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        base = values["PUBLIC_BASE_URL"].rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc
                or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin, e.g. https://voice.example.com")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(values["TELNYX_PUBLIC_KEY"], validate=True))
        path = Path(__file__).parent / os.environ.get("DIALT_INSTRUCTIONS_FILE", "instructions.md")
        instructions = path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise RuntimeError("The agent instructions file is empty")
        greeting = os.environ.get("DIALT_GREETING", "").strip() or None
        if greeting and greeting.lower() in {"false", "off", "none"}:
            greeting = False
        return cls(
            dialt_api_key=values["DIALT_API_KEY"], telnyx_api_key=values["TELNYX_API_KEY"],
            telnyx_public_key=values["TELNYX_PUBLIC_KEY"], public_base_url=base,
            instructions=instructions, dialt_url=os.environ.get("DIALT_URL") or DEFAULT_REALTIME_URL,
            voice=os.environ.get("DIALT_VOICE") or None, greeting=greeting,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


async def call_command(settings: Settings, call_id: str, action: str, **params) -> None:
    # Stable across webhook retries. Telnyx deduplicates commands by call and command_id.
    command_id = str(uuid5(NAMESPACE_URL, f"dialt:{call_id}:{action}"))
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"https://api.telnyx.com/v2/calls/{quote(call_id, safe='')}/actions/{action}",
            headers={"Authorization": f"Bearer {settings.telnyx_api_key}"},
            json={"command_id": command_id, **params},
        )
        response.raise_for_status()


def tool_manifest() -> list[dict]:
    """Declare your application's tools here."""
    return []


async def execute_tool(call_id: str, name: str, args: dict):
    """Implement application tools here. Raise ToolError for a caller-safe failure detail."""
    raise RuntimeError(f"No handler configured for tool {name!r}")


@asynccontextmanager
async def lifespan(_app):
    get_settings()
    yield


app = FastAPI(title="Dialt Telnyx bridge", lifespan=lifespan)


@app.get("/healthz")
async def health() -> dict[str, str]:
    """Readiness means startup configuration passed; this makes no paid API calls."""
    return {"status": "ok"}


@app.post("/voice")
async def voice(request: Request) -> Response:
    settings = get_settings()
    raw = await request.body()
    if not signature_is_valid(settings.telnyx_public_key, raw,
                              request.headers.get("telnyx-timestamp"),
                              request.headers.get("telnyx-signature-ed25519")):
        raise HTTPException(403, "invalid Telnyx signature")
    event = json.loads(raw)["data"]
    payload = event["payload"]
    if event["event_type"] == "call.initiated" and payload.get("direction") == "incoming":
        try:
            await call_command(
                settings, payload["call_control_id"], "answer", stream_url=settings.stream_url,
                stream_track="inbound_track", stream_codec="PCMU",
                stream_bidirectional_mode="rtp", stream_bidirectional_codec="PCMU",
                stream_bidirectional_target_legs="self",
            )
        except httpx.HTTPError:
            # A non-2xx lets Telnyx retry rather than acknowledging a lost answer command.
            logger.exception("Telnyx answer failed")
            raise HTTPException(503, "Telnyx answer failed") from None
    elif event["event_type"] == "streaming.failed":
        logger.error("Telnyx streaming failed")
    return Response(status_code=200)


@app.websocket("/media")
async def media(websocket: WebSocket) -> None:
    settings = get_settings()
    if not signature_is_valid(settings.telnyx_public_key, settings.stream_url.encode(),
                              websocket.headers.get("telnyx-timestamp"),
                              websocket.headers.get("telnyx-signature-ed25519")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    call_id = None
    try:
        async with asyncio.timeout(10):
            stream_id, call_id, media_format = await receive_stream_start(websocket)
        if media_format != {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}:
            raise ValueError("Telnyx stream requires mono PCMU at 8000 Hz")

        async def run_tool(name, args):
            return await execute_tool(call_id, name, args)

        # Call Control IDs can exceed Dialt's 64-character session ID limit. Hash the full ID.
        session_id = hashlib.sha256(call_id.encode()).hexdigest()
        await run_call_bridge(
            TelnyxMediaSocket(websocket), stream_id, session_id, settings=settings,
            mode=DialtMode(instructions=settings.instructions, voice=settings.voice,
                           greeting=settings.greeting, end_call=True,
                           tools=tool_manifest() or None),
            hooks=BridgeHooks(execute_tool=run_tool),
        )
    except (WebSocketDisconnect, ConnectionError):
        pass
    except Exception:
        logger.exception("Telnyx bridge failed")
    finally:
        # Closing a Call Control stream alone does not hang up the phone call.
        if call_id:
            try:
                await call_command(settings, call_id, "hangup")
            except httpx.HTTPStatusError as exc:
                errors = exc.response.json().get("errors", [])
                if not errors or any(str(error.get("code")) != "90018" for error in errors):
                    logger.exception("Telnyx hangup failed")
            except httpx.HTTPError:
                logger.exception("Telnyx hangup failed")
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
