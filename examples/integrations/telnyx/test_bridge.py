from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dialt import DialtSession
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dialt_recipes.telnyx import signature_is_valid

spec = importlib.util.spec_from_file_location("telnyx_bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
# Dataclasses resolve postponed annotations through the module registry.
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture
def host(monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    settings = bridge.Settings(
        dialt_api_key="fake-dialt", telnyx_api_key="fake-telnyx", telnyx_public_key=public,
        public_base_url="https://voice.example.com", instructions="You are an AI store assistant.",
    )
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)
    requests = []
    response_status = [200]

    def api(request):
        requests.append(request)
        return httpx.Response(response_status[0], json={"data": {"result": "ok"}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(api), **kwargs))

    def headers(payload, timestamp=None):
        stamp = str(int(time.time())) if timestamp is None else str(timestamp)
        return {
            "telnyx-timestamp": stamp,
            "telnyx-signature-ed25519": base64.b64encode(
                private.sign(stamp.encode() + b"|" + payload)).decode(),
        }

    with TestClient(bridge.app) as client:
        yield SimpleNamespace(client=client, settings=settings, headers=headers,
                              requests=requests, response_status=response_status)


def test_webhook_authentication_answer_and_retry(host):
    assert host.client.get("/healthz").json() == {"status": "ok"}
    raw = json.dumps({"data": {"event_type": "call.initiated", "payload": {
        "direction": "incoming", "call_control_id": "v3:call/with+tokens",
    }}}).encode()
    assert host.client.post("/voice", content=raw).status_code == 403
    assert host.client.post("/voice", content=raw + b" ", headers=host.headers(raw)).status_code == 403
    assert host.client.post("/voice", content=raw, headers=host.headers(raw, time.time() - 301)).status_code == 403
    assert host.requests == []
    for _ in range(2):
        assert host.client.post("/voice", content=raw, headers=host.headers(raw)).status_code == 200
    first, second = [json.loads(r.content) for r in host.requests]
    assert first == second  # Duplicate delivery must reuse command_id.
    assert first == {
        "command_id": first["command_id"], "stream_url": "wss://voice.example.com/media",
        "stream_track": "inbound_track", "stream_codec": "PCMU",
        "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
        "stream_bidirectional_target_legs": "self",
    }
    assert host.requests[0].headers["authorization"] == "Bearer fake-telnyx"
    assert host.requests[0].url.raw_path == b"/v2/calls/v3%3Acall%2Fwith%2Btokens/actions/answer"
    host.response_status[0] = 500
    assert host.client.post("/voice", content=raw, headers=host.headers(raw)).status_code == 503


def test_non_incoming_events_do_not_answer(host):
    for event, direction in (("call.initiated", "outgoing"), ("call.answered", "incoming"),
                             ("call.hangup", "incoming")):
        raw = json.dumps({"data": {"event_type": event, "payload": {"direction": direction}}}).encode()
        assert host.client.post("/voice", content=raw, headers=host.headers(raw)).status_code == 200
    assert host.requests == []


def test_websocket_authentication_binds_exact_url(host):
    for payload in (b"wss://evil.example.com/media", b"https://voice.example.com/media"):
        with pytest.raises(WebSocketDisconnect):
            with host.client.websocket_connect("/media", headers=host.headers(payload)):
                pass
    assert host.requests == []


@pytest.mark.parametrize("encoding,rate,channels", [("PCMA", 8000, 1), ("PCMU", 16000, 1), ("PCMU", 8000, 2)])
def test_rejects_unsupported_media(host, encoding, rate, channels):
    with host.client.websocket_connect("/media", headers=host.headers(host.settings.stream_url.encode())) as ws:
        ws.send_json({"event": "start", "stream_id": "stream-1", "start": {
            "call_control_id": "call-1", "media_format": {
                "encoding": encoding, "sample_rate": rate, "channels": channels,
            },
        }})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert host.requests[-1].url.path == "/v2/calls/call-1/actions/hangup"


def test_audio_interruption_late_marks_drain_and_hangup(host, monkeypatch):
    """Exercise provider wire frames through the real transport engine, without paid calls."""
    sessions = []

    class Session:
        async def __aenter__(self):
            self.caller_audio = asyncio.Event()
            self.reports = []
            self.input = []
            return self

        async def __aexit__(self, *args):
            pass

        async def send_audio(self, pcm):
            self.input.append(pcm)
            self.caller_audio.set()

        async def send_client_event(self, name, **fields):
            self.reports.append((name, fields))

        async def events(self):
            samples = np.full(1600, 0.125, dtype=np.float32)
            yield SimpleNamespace(type="audio", audio=samples, data={})
            await self.caller_audio.wait()
            yield SimpleNamespace(type="interrupted", audio=None, data={"clear": True, "barge_seq": 7})
            yield SimpleNamespace(type="audio", audio=samples[:800], data={})

    async def connect(*args, **kwargs):
        assert kwargs["output_sample_rate"] == 8000
        assert len(kwargs["session_id"]) == 64
        session = Session()
        sessions.append(session)
        return session

    monkeypatch.setattr(DialtSession, "connect", connect)
    with host.client.websocket_connect("/media", headers=host.headers(host.settings.stream_url.encode())) as ws:
        ws.send_json({"event": "connected"})
        ws.send_json({"event": "start", "stream_id": "stream-1", "start": {
            "call_control_id": "v3:" + "a" * 100, "media_format": {
                "encoding": "PCMU", "sample_rate": 8000, "channels": 1,
            },
        }})
        marks, media = [], []
        while len(marks) < 2:
            msg = ws.receive_json()
            assert "streamSid" not in msg
            (marks if msg["event"] == "mark" else media).append(msg)
        assert len(media) == 10
        # G.711 encoding of a positive 4096-ish PCM16 sample is 0xAF.
        assert all(base64.b64decode(m["media"]["payload"]) == b"\xaf" * 160 for m in media)
        # Outbound-track data must never feed back into Dialt as caller speech.
        ws.send_json({"event": "media", "media": {"track": "outbound", "payload": "!!!"}})
        ws.send_json({"event": "media", "media": {
            "track": "inbound", "payload": base64.b64encode(b"\xff" * 1600).decode(),
        }})
        assert ws.receive_json() == {"event": "clear"}
        # Telnyx returns queued marks on clear. They must not revive discarded playback.
        for mark in reversed(marks):
            ws.send_json(mark)
        replacement = []
        while True:
            msg = ws.receive_json()
            if msg["event"] == "mark":
                ws.send_json(msg)
                break
            replacement.append(msg)
        assert len(replacement) == 5
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert len(sessions[0].input) == 1
    assert len(sessions[0].input[0]) > 1600  # Decoded/resampled PCM16, not original PCMU.
    assert sessions[0].reports == [("playback_stopped", {
        "remaining_ms": 0, "discarded_ms": 200, "barge_seq": 7,
    })]
    assert host.requests[-1].url.path.endswith("/actions/hangup")


def test_settings_fail_fast_and_greeting_controls(monkeypatch, tmp_path):
    for name in ("DIALT_API_KEY", "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="Missing required"):
        bridge.Settings.from_env()
    private = Ed25519PrivateKey.generate()
    for name, value in {
        "DIALT_API_KEY": "fake", "TELNYX_API_KEY": "fake",
        "TELNYX_PUBLIC_KEY": base64.b64encode(private.public_key().public_bytes_raw()).decode(),
        "PUBLIC_BASE_URL": "https://voice.example.com",
    }.items():
        monkeypatch.setenv(name, value)
    instructions = tmp_path / "instructions.md"
    instructions.write_text("You are an AI assistant.")
    monkeypatch.setenv("DIALT_INSTRUCTIONS_FILE", str(instructions))
    monkeypatch.setenv("DIALT_GREETING", "off")
    assert bridge.Settings.from_env().greeting is False
    for origin in ("http://voice.example.com", "https://user@voice.example.com", "https://voice.example.com/path"):
        monkeypatch.setenv("PUBLIC_BASE_URL", origin)
        with pytest.raises(RuntimeError, match="HTTPS origin"):
            bridge.Settings.from_env()


def test_signature_rejects_malformed_or_future_headers(host):
    public = host.settings.telnyx_public_key
    assert not signature_is_valid(public, b"{}", "not-a-time", "!!!")
    headers = host.headers(b"{}", int(time.time()) + 301)
    assert not signature_is_valid(public, b"{}", headers["telnyx-timestamp"], headers["telnyx-signature-ed25519"])
