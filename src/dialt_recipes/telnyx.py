"""Telnyx Call Control media adapter over the maintained phone bridge engine.

Telnyx PCMU payloads and mark/clear semantics match the engine's audio contract.
Only the wire envelope differs: outgoing commands have no streamSid, and caller
media must be selected by track. Authentication and call control stay with the host.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .twilio import MAX_WS_MESSAGE_CHARS


def signature_is_valid(public_key: str, payload: bytes, timestamp: str | None,
                       signature: str | None) -> bool:
    """Verify exact HTTP body bytes or the configured stream URL, with a 5-minute window."""
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), timestamp.encode() + b"|" + payload)
    except (ValueError, binascii.Error, InvalidSignature):
        return False
    return True


async def receive_stream_start(websocket: Any) -> tuple[str, str, dict[str, Any]]:
    """Return stream ID, Call Control ID and format so the host can validate and clean up."""
    while True:
        raw = await websocket.receive_text()
        if len(raw) > MAX_WS_MESSAGE_CHARS:
            raise ValueError("Telnyx message too large")
        message = json.loads(raw)
        if message.get("event") == "start":
            start = message.get("start", {})
            stream_id = message.get("stream_id")
            call_id = start.get("call_control_id")
            if not isinstance(stream_id, str) or not stream_id or not isinstance(call_id, str) or not call_id:
                raise ValueError("Telnyx start requires stream_id and call_control_id")
            return stream_id, call_id, start.get("media_format", {})
        if message.get("event") in {"stop", "error"}:
            raise ConnectionError("Telnyx stream ended before start")


class TelnyxMediaSocket:
    """Adapt Telnyx wire messages to the shared engine without duplicating call state."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket

    async def send_json(self, message: dict[str, Any]) -> None:
        await self.websocket.send_json({k: v for k, v in message.items() if k != "streamSid"})

    async def iter_text(self):
        async for raw in self.websocket.iter_text():
            if len(raw) > MAX_WS_MESSAGE_CHARS:
                raise ValueError("Telnyx message too large")
            message = json.loads(raw)
            if message.get("event") == "error":
                raise ConnectionError("Telnyx reported a media stream error")
            if message.get("event") == "media" and message.get("media", {}).get("track") != "inbound":
                continue
            yield raw
