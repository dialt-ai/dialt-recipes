"""An ordinary named LiveKit agent, independent of the Dialt adapter."""

import asyncio
import os

from dotenv import load_dotenv
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import cartesia, openai, silero

load_dotenv()
server = AgentServer()


@server.rtc_session(agent_name=os.environ.get("LIVEKIT_AGENT_NAME", "dialt-reference"))
async def entrypoint(ctx: JobContext):
    session = AgentSession(
        # This demo uses a virtual caller microphone, with no acoustic echo.
        aec_warmup_duration=0,
        stt=cartesia.STT(model="ink-whisper"),
        llm=openai.LLM(
            api_key=os.environ["TOGETHER_API_KEY"], base_url="https://api.together.xyz/v1",
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ),
        tts=cartesia.TTS(model="sonic-3"), vad=silero.VAD.load(),
    )
    control_tasks = set()

    def speech_done(handle):
        if handle.interrupted:
            task = asyncio.create_task(ctx.room.local_participant.publish_data(
                b'{"type":"interrupt"}', reliable=True, topic="dialt.control",
            ))
            control_tasks.add(task)
            task.add_done_callback(control_tasks.discard)

    @session.on("speech_created")
    def speech_created(event):
        # Normal speech completion never emits an interruption.
        event.speech_handle.add_done_callback(speech_done)

    async def flush_control():
        await asyncio.gather(*control_tasks, return_exceptions=True)

    ctx.add_shutdown_callback(flush_control)
    await session.start(room=ctx.room, agent=Agent(instructions=(
        "You are a helpful voice assistant. Give concise answers in one sentence. "
        "Your name is Liv. If asked for opening hours, say nine to five Monday to Friday."
    )))
    await ctx.connect()
    await session.say("Hello, I'm Liv. How can I help?")


if __name__ == "__main__":
    cli.run_app(server)
