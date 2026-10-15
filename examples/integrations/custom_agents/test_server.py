import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from server import AUDIO, authenticate, session

TOKEN = "test-only-token-with-at-least-32-characters"
START = {"type": "start", "version": 1, "audio": AUDIO, "session_id": "test",
         "opening": "agent", "timeout_s": 5,
         "capabilities": {"transcripts": False, "tool_events": False}}


@asynccontextmanager
async def endpoint(agent):
    async with serve(lambda ws: session(ws, agent), "127.0.0.1", 0,
                     process_request=authenticate(TOKEN), max_size=32000) as server:
        yield "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])


async def test_auth_begin_pcm_interrupt_and_drain():
    started = asyncio.Event()
    heard = []
    pcm = b"\x01\x02" * 17000

    async def agent(start, incoming, send):
        started.set()
        heard.append(await incoming.get())
        await send(pcm)
        await send({"type": "interrupt"})
        await send(b"\x03\x04")

    async with endpoint(agent) as url:
        with pytest.raises(InvalidStatus) as error:
            async with connect(url):
                pass
        assert error.value.response.status_code == 401
        async with connect(url, additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
            await ws.send(json.dumps(START))
            assert json.loads(await ws.recv()) == {"type": "ready", "version": 1}
            assert not started.is_set()
            await ws.send('{"type":"begin"}')
            await ws.send(b"\x05\x06" * 320)
            assert await ws.recv() == pcm[:32000]
            assert await ws.recv() == pcm[32000:]
            assert json.loads(await ws.recv()) == {"type": "interrupt"}
            await ws.send('{"type":"playback_stopped","discarded_ms":100}')
            assert await ws.recv() == b"\x03\x04"
            assert json.loads(await ws.recv()) == {"type": "end"}
            # Connection stays usable after end, until Dialt acknowledges drain.
            await ws.ping()
            await ws.send('{"type":"stop"}')
    assert heard == [b"\x05\x06" * 320]


@pytest.mark.parametrize("disconnect", [False, True])
async def test_stop_and_disconnect_cancel_provider(disconnect):
    stopped = asyncio.Event()
    started = asyncio.Event()

    async def agent(start, incoming, send):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async with endpoint(agent) as url:
        async with connect(url, additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
            await ws.send(json.dumps(START))
            await ws.recv()
            await ws.send('{"type":"begin"}')
            await started.wait()
            if disconnect:
                await ws.close()
            else:
                await ws.send('{"type":"stop"}')
            await asyncio.wait_for(stopped.wait(), 2)


@pytest.mark.parametrize("override", [{"version": 2}, {"audio": {}},
    {"capabilities": {"tool_events": True}}])
async def test_reject_invalid_or_unsupported_start(override):
    async def agent(*args):
        pytest.fail("must not launch")

    async with endpoint(agent) as url:
        async with connect(url, additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
            await ws.send(json.dumps(START | override))
            assert json.loads(await ws.recv())["type"] == "error"


@pytest.mark.parametrize("error", [RuntimeError("secret signed provider URL"),
                                  ConnectionClosedError(None, None)])
async def test_provider_error_is_sanitized(error):
    async def agent(*args):
        raise error

    async with endpoint(agent) as url:
        async with connect(url, additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
            await ws.send(json.dumps(START))
            await ws.recv()
            await ws.send('{"type":"begin"}')
            assert json.loads(await ws.recv()) == {
                "type": "error", "message": "Adapter session failed",
            }


async def test_conformance_cli_contract():
    from conformance import check

    async def agent(start, incoming, send):
        await send(b"\x01\x02" * 320)

    async with endpoint(agent) as url:
        result = await check(url, TOKEN, duration=1)
    assert result["authenticated"]
    assert result["audio_bytes"] == 640
    assert result["ended"]


async def test_pipecat_real_pipeline_audio_and_cleanup(monkeypatch):
    import sys
    import types

    pytest.importorskip("pipecat")
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat_adapter import run

    class Echo(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputAudioRawFrame):
                await self.push_frame(OutputAudioRawFrame(
                    audio=frame.audio, sample_rate=16000, num_channels=1,
                ))
            else:
                await self.push_frame(frame, direction)

    async def build(source, output, opening):
        return PipelineWorker(Pipeline([source, Echo(), output]),
                              params=PipelineParams(audio_in_sample_rate=16000,
                                                    audio_out_sample_rate=16000))

    module = types.ModuleType("test_pipeline")
    module.build = build
    monkeypatch.setitem(sys.modules, "test_pipeline", module)
    monkeypatch.setenv("PIPECAT_FACTORY", "test_pipeline:build")
    # 40 ms matches the transport chunk size and must survive byte-for-byte.
    pcm = b"\x03\x02" * 640
    async with endpoint(run) as url:
        async with connect(url, additional_headers={"Authorization": "Bearer " + TOKEN}) as ws:
            await ws.send(json.dumps(START))
            assert json.loads(await ws.recv())["type"] == "ready"
            await ws.send('{"type":"begin"}')
            await ws.send(pcm)
            assert await asyncio.wait_for(ws.recv(), 3) == pcm
            await ws.send('{"type":"stop"}')
            await asyncio.wait_for(ws.wait_closed(), 3)
