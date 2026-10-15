"""Authenticated Dialt custom-agent v1 endpoint. No agent policy lives here."""

import argparse
import asyncio
import hmac
import importlib
import json
import logging
import os
from http import HTTPStatus

from dotenv import load_dotenv
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

AUDIO = {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}
log = logging.getLogger(__name__)


async def session(ws, run_agent):
    """One provider session per socket; stop/disconnect always cancels its owner."""
    agent = None
    ended = False
    send_lock = asyncio.Lock()

    async def send(value):
        nonlocal ended
        async with send_lock:
            if ended:
                return
            if isinstance(value, bytes):
                if len(value) % 2:
                    raise ValueError("unaligned provider PCM")
                for offset in range(0, len(value), 32000):
                    await ws.send(value[offset:offset + 32000])
            else:
                if value["type"] in ("end", "error"):
                    ended = True
                await ws.send(json.dumps(value))

    try:
        async with asyncio.timeout(30):
            start = json.loads(await ws.recv())
            if (start.get("type") != "start" or start.get("version") != 1
                    or start.get("audio") != AUDIO
                    or start.get("opening") not in ("agent", "caller")
                    or not isinstance(start.get("session_id"), str)
                    or not 0 < start.get("timeout_s", 0) <= 3600):
                raise ValueError("invalid start")
            # These minimal adapters use Dialt's audio-derived transcript. They do
            # not fabricate tool evidence or claim partial transcripts are final.
            if any(start.get("capabilities", {}).values()):
                raise ValueError("optional evidence is not supported")
            await send({"type": "ready", "version": 1})
            begin = json.loads(await ws.recv())
            if begin.get("type") == "stop":
                return
            if begin != {"type": "begin"}:
                raise ValueError("expected begin")

        # Allow provider cold start, but fail rather than silently lose speech or
        # block stop behind a stalled consumer. Each queue item is at most 20 ms.
        incoming = asyncio.Queue(maxsize=500)

        async def receive():
            async for value in ws:
                if isinstance(value, bytes):
                    if not value or len(value) > 32000 or len(value) % 2:
                        raise ValueError("invalid PCM frame")
                    if not ended:
                        for offset in range(0, len(value), 640):
                            incoming.put_nowait(value[offset:offset + 640])
                else:
                    event = json.loads(value)
                    if event.get("type") == "stop":
                        return
                    if event.get("type") != "playback_stopped":
                        raise ValueError("unexpected control")

        async with asyncio.timeout(start["timeout_s"] + 30):
            agent = asyncio.create_task(run_agent(start, incoming, send))
            receiver = asyncio.create_task(receive())
            try:
                done, _ = await asyncio.wait(
                    [agent, receiver], return_when=asyncio.FIRST_COMPLETED
                )
                if agent in done:
                    await agent  # Surface provider failure, not a successful end.
                    await send({"type": "end"})
                    await receiver  # Dialt drains final audio before stop.
                else:
                    await receiver
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
    except Exception as exc:
        # Provider exception strings can contain signed URLs, credentials or audio.
        log.warning("Session failed (%s)", type(exc).__name__)
        try:
            await ws.send(json.dumps({"type": "error", "message": "Adapter session failed"}))
        except ConnectionClosed:
            pass
    finally:
        if agent is not None:
            agent.cancel()
            await asyncio.gather(agent, return_exceptions=True)


def authenticate(token):
    def check(connection, request):
        values = request.headers.get_all("Authorization")
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), ("Bearer " + token).encode()):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        if request.path != "/":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
    return check


async def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("framework", choices=["livekit", "elevenlabs", "pipecat"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ["ADAPTER_TOKEN"]
    if len(token) < 32:
        raise ValueError("ADAPTER_TOKEN must contain at least 32 characters")
    run_agent = importlib.import_module(args.framework + "_adapter").run
    async with serve(lambda ws: session(ws, run_agent), "127.0.0.1", args.port,
                     process_request=authenticate(token), max_size=32000, max_queue=16):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
