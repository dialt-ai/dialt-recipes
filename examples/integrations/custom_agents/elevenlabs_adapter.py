"""Attach to an existing ElevenLabs Agent; its server tools remain unchanged."""

import asyncio
import os

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import AsyncAudioInterface, AsyncConversation


async def run(start, incoming, send):
    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    agent_id = os.environ["ELEVENLABS_AGENT_ID"]
    agent = await asyncio.to_thread(client.conversational_ai.agents.get, agent_id=agent_id)
    config = agent.conversation_config
    if (config.asr.user_input_audio_format != "pcm_16000"
            or config.tts.agent_output_audio_format != "pcm_16000"):
        await send({"type": "error", "message": "ElevenLabs input and output must use pcm_16000"})
        return
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
        client, agent_id, requires_auth=True, audio_interface=audio,
    )
    tasks = []
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

        tasks = [asyncio.create_task(feed()),
                 asyncio.create_task(conversation.wait_for_session_end())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await conversation.end_session()
