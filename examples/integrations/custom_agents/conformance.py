"""Exercise an adapter's real WebSocket; not a simulator or an agent quality eval."""

import argparse
import asyncio
import json
import os
import time
import uuid
import wave

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from server import AUDIO


async def check(url, token, *, duration=15, opening="agent", pcm=b"", output=None,
                input_delay=None):
    # Authentication must fail before a provider session is allocated.
    try:
        async with connect(url, additional_headers={"Authorization": "Bearer invalid"}):
            raise ValueError("adapter accepted invalid token")
    except InvalidStatus as exc:
        if exc.response.status_code not in (401, 403):
            raise ValueError("unexpected authentication response") from None

    metrics = {"authenticated": True, "audio_bytes": 0, "interrupts": 0,
               "discarded_ms": 0, "first_audio_s": None, "ended": False}
    recorded = bytearray()
    pending = bytearray()
    async with connect(url, additional_headers={"Authorization": "Bearer " + token},
                       max_size=32000) as ws:
        await ws.send(json.dumps({
            "type": "start", "version": 1, "audio": AUDIO,
            "session_id": str(uuid.uuid4()), "opening": opening, "timeout_s": duration,
            "capabilities": {"transcripts": False, "tool_events": False},
        }))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if ready.get("type") != "ready" or ready.get("version") != 1:
            raise ValueError("expected ready version 1")
        try:
            await asyncio.wait_for(ws.recv(), 0.2)
        except TimeoutError:
            pass
        else:
            raise ValueError("adapter emitted output before begin")
        started = time.monotonic()
        await ws.send('{"type":"begin"}')

        async def feed():
            # Let an agent greeting finish before playing the optional input.
            delay = input_delay if input_delay is not None else (5 if opening == "agent" else 0)
            offset = int(delay * 50) * 640
            for index in range(duration * 50):
                position = index * 640 - offset
                chunk = pcm[position:position + 640] if position >= 0 else b""
                await ws.send(chunk.ljust(640, b"\0"))
                await asyncio.sleep(max(0, started + (index + 1) * 0.02 - time.monotonic()))

        async def play():
            while True:
                recorded.extend(pending[:640])
                del pending[:640]
                await asyncio.sleep(0.02)

        feeder = asyncio.create_task(feed())
        player = asyncio.create_task(play())
        try:
            while time.monotonic() - started < duration:
                remaining = duration - (time.monotonic() - started)
                try:
                    value = await asyncio.wait_for(ws.recv(), remaining)
                except TimeoutError:
                    break
                if isinstance(value, bytes):
                    if not value or len(value) % 2 or len(value) > 32000 or metrics["ended"]:
                        raise ValueError("invalid or post-end PCM")
                    if metrics["first_audio_s"] is None and any(value):
                        metrics["first_audio_s"] = round(time.monotonic() - started, 3)
                    metrics["audio_bytes"] += len(value)
                    pending.extend(value)
                    if len(pending) > 30 * 32000:
                        raise ValueError("more than 30 seconds of pending output")
                else:
                    event = json.loads(value)
                    if event["type"] == "interrupt":
                        metrics["interrupts"] += 1
                        discarded_ms = len(pending) / 32
                        pending.clear()
                        metrics["discarded_ms"] += discarded_ms
                        await ws.send(json.dumps({"type": "playback_stopped",
                                                  "discarded_ms": discarded_ms}))
                    elif event["type"] == "end":
                        metrics["ended"] = True
                    elif event["type"] == "error":
                        raise ValueError("adapter reported error")
                    else:
                        raise ValueError("unexpected event with evidence disabled")
            if metrics["ended"]:
                async with asyncio.timeout(31):
                    while pending:
                        await asyncio.sleep(0.02)
            await ws.send('{"type":"stop"}')
            await asyncio.wait_for(ws.wait_closed(), 10)
        finally:
            feeder.cancel()
            player.cancel()
            await asyncio.gather(feeder, player, return_exceptions=True)
    if metrics["first_audio_s"] is None:
        raise ValueError("no non-silent agent audio received")
    if opening == "agent" and metrics["first_audio_s"] > 8:
        raise ValueError("agent missed the 8-second greeting window")
    if output:
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(recorded)
    return metrics


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--opening", choices=["agent", "caller"], default="agent")
    parser.add_argument("--input-wav")
    parser.add_argument("--output-wav")
    parser.add_argument("--input-delay", type=float, help="seconds before caller audio; use 1 for barge-in")
    args = parser.parse_args()
    if not 8 <= args.duration <= 300:
        parser.error("duration must be between 8 and 300 seconds")
    if args.input_delay is not None and not 0 <= args.input_delay < args.duration:
        parser.error("input-delay must be nonnegative and less than duration")
    pcm = b""
    if args.input_wav:
        with wave.open(args.input_wav, "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                parser.error("input WAV must be PCM16 mono 16000 Hz")
            pcm = wav.readframes(wav.getnframes())
    if args.opening == "caller" and not pcm:
        parser.error("caller opening requires --input-wav")
    try:
        metrics = asyncio.run(check(args.url, os.environ["ADAPTER_TOKEN"], duration=args.duration,
                                    opening=args.opening, pcm=pcm, output=args.output_wav,
                                    input_delay=args.input_delay))
    except Exception as exc:
        # Never dump websocket exceptions, headers, or signed provider URLs.
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"passed": True, **metrics}))


if __name__ == "__main__":
    main()
