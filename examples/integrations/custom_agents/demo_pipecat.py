"""Small real Pipecat agent; replace this factory with your own pipeline."""

import os

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.llm import OpenAILLMService


async def build(source, output, opening):
    context = LLMContext()
    aggregator = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
    ))
    stt = CartesiaSTTService(api_key=os.environ["CARTESIA_API_KEY"])
    llm = OpenAILLMService(
        api_key=os.environ["TOGETHER_API_KEY"], base_url="https://api.together.xyz/v1",
        settings=OpenAILLMService.Settings(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            system_instruction=(
                "You are a helpful voice assistant. Give concise answers in one sentence. "
                "Your name is Pip. If asked for opening hours, say nine to five Monday to Friday."
            ),
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            model="sonic-3", voice="f786b574-daa5-4673-aa0c-cbe3e8534c02",
        ),
    )
    worker = PipelineWorker(Pipeline([
        source, stt, aggregator.user(), llm, tts, output, aggregator.assistant(),
    ]), params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000))

    @worker.event_handler("on_pipeline_started")
    async def started(worker, frame):
        if opening == "agent":
            await worker.queue_frames([TTSSpeakFrame("Hello, I'm Pip. How can I help?")])

    return worker
