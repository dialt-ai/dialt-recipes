"""Dispatch an existing named LiveKit agent into an isolated evaluation room."""

import asyncio
import os
import time
import uuid

from livekit import api, rtc


async def run(start, incoming, send):
    room_name = "dialt-eval-" + uuid.uuid4().hex
    client = api.LiveKitAPI()
    room = rtc.Room()
    source = rtc.AudioSource(16000, 1, queue_size_ms=100)
    readers = set()
    dispatch = None
    failure = asyncio.get_running_loop().create_future()

    async def output(track, participant):
        stream = rtc.AudioStream(track=track, sample_rate=16000, num_channels=1)
        last_speaking = float("-inf")
        try:
            async for event in stream:
                pcm = bytes(event.frame.data)
                # Opus comfort noise isn't digital silence. Agents SDK state is
                # the speech boundary; retain a short tail for in-flight packets.
                if participant.attributes.get("lk.agent.state") == "speaking":
                    last_speaking = time.monotonic()
                if time.monotonic() - last_speaking < 0.2:
                    await send(pcm)
        finally:
            await stream.aclose()

    def completed(task):
        readers.discard(task)
        if not task.cancelled() and task.exception() and not failure.done():
            failure.set_exception(task.exception())

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            task = asyncio.create_task(output(track, participant))
            readers.add(task)
            task.add_done_callback(completed)

    @room.on("participant_disconnected")
    def left(participant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not failure.done():
            failure.set_result(None)

    async def input_audio():
        while True:
            pcm = await incoming.get()
            await source.capture_frame(rtc.AudioFrame(pcm, 16000, 1, len(pcm) // 2))

    feeder = None
    try:
        token = (api.AccessToken().with_identity("dialt-caller")
                 .with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt())
        await room.connect(os.environ["LIVEKIT_URL"], token)
        track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        dispatch = await client.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            room=room_name, agent_name=os.environ["LIVEKIT_AGENT_NAME"],
        ))
        feeder = asyncio.create_task(input_audio())
        done, _ = await asyncio.wait([feeder, failure], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        tasks = list(readers) + ([feeder] if feeder else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await room.disconnect()
        await source.aclose()
        try:
            if dispatch:
                await client.agent_dispatch.delete_dispatch(dispatch.id, room_name)
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        finally:
            await client.aclose()
