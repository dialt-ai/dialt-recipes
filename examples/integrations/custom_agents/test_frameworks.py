"""Provider lifecycle regressions at the adapters' public run boundary."""

import asyncio
from types import SimpleNamespace as NS

import pytest


async def test_elevenlabs_feeder_failure_and_format_validation(monkeypatch):
    pytest.importorskip("elevenlabs")
    import elevenlabs_adapter as adapter

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "test-agent")
    config = NS(asr=NS(user_input_audio_format="pcm_16000"),
                tts=NS(agent_output_audio_format="pcm_16000"))
    monkeypatch.setattr(adapter, "ElevenLabs", lambda **kwargs: NS(
        conversational_ai=NS(agents=NS(get=lambda **kwargs: NS(conversation_config=config))),
    ))
    closed = asyncio.Event()

    class Conversation:
        def __init__(self, *args, audio_interface, **kwargs):
            self.audio = audio_interface

        async def start_session(self):
            async def failed_upload(pcm):
                raise RuntimeError("provider upload failed")
            await self.audio.start(failed_upload)

        async def wait_for_session_end(self):
            await asyncio.Future()

        async def end_session(self):
            closed.set()

    monkeypatch.setattr(adapter, "AsyncConversation", Conversation)
    incoming = asyncio.Queue()
    await incoming.put(bytes(8000))
    output = []

    async def send(value):
        output.append(value)

    with pytest.raises(RuntimeError, match="upload failed"):
        await asyncio.wait_for(adapter.run({}, incoming, send), 1)
    assert closed.is_set()
    assert output == []

    config.tts.agent_output_audio_format = "pcm_44100"
    await adapter.run({}, incoming, send)
    assert output == [{"type": "error", "message": "ElevenLabs input and output must use pcm_16000"}]


@pytest.mark.parametrize("cleanup_failure", [None, "disconnect", "source", "dispatch"])
async def test_livekit_drains_final_packets_and_cleans_resources(monkeypatch, cleanup_failure):
    pytest.importorskip("livekit")
    import livekit_adapter as adapter

    for key in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL", "LIVEKIT_AGENT_NAME"):
        monkeypatch.setenv(key, "test-only")
    resources = {"room": True, "dispatch": True, "client_closed": False}
    participant = NS(identity="agent", kind=adapter.rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
                     attributes={"lk.agent.state": "speaking"})
    frames = [b"\x01\x02" * 160, b"\x03\x04" * 160]

    class Room:
        def __init__(self):
            self.handlers = {}
            self.local_participant = NS(publish_track=self.publish)

        def on(self, event):
            def register(handler):
                self.handlers[event] = handler
                return handler
            return register

        async def publish(self, *args):
            pass

        async def connect(self, *args):
            self.handlers["track_subscribed"](
                NS(kind=adapter.rtc.TrackKind.KIND_AUDIO), None, participant,
            )
            # The participant leaves before the queued final frames arrive.
            self.handlers["participant_disconnected"](participant)

        async def disconnect(self):
            if cleanup_failure == "disconnect":
                raise RuntimeError("disconnect failed")

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            if cleanup_failure == "source":
                raise RuntimeError("source close failed")

    class Stream:
        def __init__(self, **kwargs):
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.01)
            if self.index == len(frames):
                raise StopAsyncIteration
            value = frames[self.index]
            self.index += 1
            return NS(frame=NS(data=value))

        async def aclose(self):
            pass

    async def create_dispatch(request):
        return NS(id="dispatch")

    async def delete_dispatch(*args):
        resources["dispatch"] = False
        if cleanup_failure == "dispatch":
            raise RuntimeError("dispatch delete failed")

    async def delete_room(request):
        resources["room"] = False

    async def close_client():
        resources["client_closed"] = True

    monkeypatch.setattr(adapter.api, "LiveKitAPI", lambda: NS(
        agent_dispatch=NS(create_dispatch=create_dispatch, delete_dispatch=delete_dispatch),
        room=NS(delete_room=delete_room), aclose=close_client,
    ))
    monkeypatch.setattr(adapter.rtc, "Room", Room)
    monkeypatch.setattr(adapter.rtc, "AudioSource", Source)
    monkeypatch.setattr(adapter.rtc, "AudioStream", Stream)
    monkeypatch.setattr(adapter.rtc.LocalAudioTrack, "create_audio_track", lambda *args: None)
    output = []

    async def send(value):
        output.append(value)

    if cleanup_failure:
        with pytest.raises(RuntimeError):
            await adapter.run({}, asyncio.Queue(), send)
    else:
        await adapter.run({}, asyncio.Queue(), send)
    assert output == frames
    assert resources == {"room": False, "dispatch": False, "client_closed": True}
