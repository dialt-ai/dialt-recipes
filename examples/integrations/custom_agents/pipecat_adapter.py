"""Use an application's Pipecat pipeline with a Dialt audio transport."""

import asyncio
import importlib
import os

from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner


async def run(start, incoming, send):
    ready = asyncio.Event()
    params = TransportParams(
        audio_in_enabled=True, audio_in_sample_rate=16000,
        audio_out_enabled=True, audio_out_sample_rate=16000,
        audio_out_auto_silence=False, audio_out_end_silence_secs=0,
    )

    class Input(BaseInputTransport):
        async def start(self, frame):
            await super().start(frame)
            await self.set_transport_ready(frame)
            ready.set()

    class Output(BaseOutputTransport):
        async def start(self, frame):
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def write_audio_frame(self, frame):
            await send(frame.audio)
            # BaseOutputTransport delegates playout timing to the transport.
            await asyncio.sleep(len(frame.audio) / 32000)
            return True

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InterruptionFrame):
                await send({"type": "interrupt"})

    source, output = Input(params), Output(params)
    module, name = os.environ["PIPECAT_FACTORY"].split(":", 1)
    # Factory owns STT/LLM/TTS, context, tools, turn handling and greeting.
    worker = await getattr(importlib.import_module(module), name)(source, output, start["opening"])
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def feed():
        await ready.wait()
        while True:
            await source.push_audio_frame(InputAudioRawFrame(
                audio=await incoming.get(), sample_rate=16000, num_channels=1,
            ))

    feeder = asyncio.create_task(feed())
    running = asyncio.create_task(runner.run())
    try:
        done, _ = await asyncio.wait([feeder, running], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        feeder.cancel()
        await worker.cancel()
        await asyncio.gather(feeder, running, return_exceptions=True)
