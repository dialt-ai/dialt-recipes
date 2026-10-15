"""Attach to an existing ElevenLabs Agent; its server tools remain unchanged."""

import asyncio
import os

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import AsyncAudioInterface, AsyncConversation


async def run(start, incoming, send):
    ready = asyncio.Event()

    class Audio(AsyncAudioInterface):
        callback = None

        async def start(self, input_callback):
            self.callback = input_callback
            ready.set()

        async def stop(self):
            self.callback = None

        async def output(self, audio):
            await send(audio)

        async def interrupt(self):
            await send({"type": "interrupt"})

    audio = Audio()
    conversation = AsyncConversation(
        ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"]),
        os.environ["ELEVENLABS_AGENT_ID"], requires_auth=True, audio_interface=audio,
    )
    feeder = None
    try:
        await conversation.start_session()

        async def feed():
            await ready.wait()
            pending = bytearray()
            while True:
                pending.extend(await incoming.get())
                # ElevenLabs recommends 250 ms input chunks.
                while len(pending) >= 8000:
                    if audio.callback is None:
                        return
                    await audio.callback(bytes(pending[:8000]))
                    del pending[:8000]

        feeder = asyncio.create_task(feed())
        await conversation.wait_for_session_end()
    finally:
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        await conversation.end_session()
