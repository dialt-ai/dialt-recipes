# Test an existing voice agent with a Dialt caller

These reference adapters expose the same authenticated WebSocket for **LiveKit Agents**,
**ElevenLabs Agents**, and **Pipecat**. Dialt supplies the simulated caller and scoring; your
agent retains its prompt, models, tools, credentials and application state. There is no
framework-specific behavior in Dialt and no replacement conversation engine here.

The adapters are deliberately audio-only: use `transcripts:false, tool_events:false`.
Dialt derives the transcript from caller-side audio recognition and caller utterances, with
explicit provenance. These examples neither execute tools on Dialt's behalf nor fabricate
tool evidence. Existing server-side agent tools continue to run in their original application.

## Install and run

This is a standalone Python 3.11+ uv project. Select only the framework you need:

```sh
cd examples/integrations/custom_agents
cp env.example .env
# Fill .env with your adapter token and provider configuration.
uv sync --frozen --extra livekit
uv run --extra livekit python server.py livekit --port 8765
# Or:
uv run --extra elevenlabs python server.py elevenlabs --port 8765
uv run --extra pipecat python server.py pipecat --port 8765
```

Generate a random `ADAPTER_TOKEN` of at least 32 characters, store it in `.env` or your secret
manager, and never commit it. The server listens on loopback. Put an HTTPS reverse proxy in
front of it that preserves `Authorization` and WebSocket upgrades. For a temporary dev test,
`cloudflared tunnel --url http://127.0.0.1:8765` provides a public HTTPS hostname; use its
`wss://` equivalent as the adapter URL. The bearer check still protects the tunnel. Keep
the server and tunnel supervised. An Amp portal requires browser authentication and is
**not** a suitable endpoint for the hosted Dialt worker.

Use a dedicated test agent and sandbox tools. Real agent tools may have real side effects.
Scope provider credentials, limit exposure/concurrency at your proxy, and disable provider
debug logging if conversations contain sensitive data. Provider SDKs may log transcripts.
The bridge reports generic errors rather than exposing signed URLs or credential-bearing
provider exceptions.

### LiveKit Agents

Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `LIVEKIT_AGENT_NAME`.
Run your existing **named** agent worker. The bridge creates an isolated room, publishes
the caller microphone, and explicitly dispatches that agent on `begin`. It subscribes only
to agent-participant audio, converts it to mono 16 kHz, and deletes its dispatch and room on
stop/disconnect. Do not use unnamed auto-dispatched agents in the same project: they can
join before explicit dispatch and introduce an unintended second agent.

The agent must publish the standard Agents SDK `lk.agent.state` attribute. RTC contains
continuous silence and Opus comfort noise; the bridge forwards audio only during `speaking`
and a 200 ms packet tail. The bridge does not infer a barge-in event from a normal speech
ending. LiveKit handles its own interruption; this minimal RTC bridge has no explicit
`interrupt` event, so it is not suitable for measuring exact remote playback cancellation.
Use matching case opening behavior: the bridge does not override an existing agent's greeting.

For a real, small agent using Cartesia STT/TTS and Together's Llama model:

```sh
uv sync --frozen --all-extras
# Set CARTESIA_API_KEY, TOGETHER_API_KEY and LIVEKIT_* in .env.
uv run --all-extras python demo_livekit.py dev
# In another shell (LIVEKIT_AGENT_NAME=dialt-reference):
uv run --all-extras python server.py livekit
```

`demo_livekit.py` is an ordinary LiveKit agent, not an eval-only implementation. It greets
first and answers opening-hours questions with nine to five Monday through Friday.

### ElevenLabs Agents

Set `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID` for an existing agent. Configure that
agent's **input and output as PCM16, mono, 16 kHz**. The public `AsyncConversation` /
`AsyncAudioInterface` SDK supplies audio and interruption callbacks. The adapter waits for
`begin` before initiating the provider session, then batches caller input into the SDK's
recommended 250 ms chunks. It does not override the prompt, voice, first message or tools.

Use server tools/webhooks configured in ElevenLabs. If your agent uses client tools, pass
your application's existing `ClientTools` implementation to `AsyncConversation` in
`elevenlabs_adapter.py`; tools cannot be recovered from an agent ID alone. Match your case's
opening behavior to the configured first message. A key with TTS permissions alone may
return `401 missing_permissions` for Agents APIs; that is not proof the key is invalid.
Both an accessible agent ID and permission to start its conversation are required.

### Pipecat

Set `PIPECAT_FACTORY=your_module:build`. The module must be importable in this Python
environment. Its async factory receives `(source, output, opening)` and returns a
`PipelineWorker` containing those transport processors around your existing STT, context
aggregators, LLM, tools and TTS. Construct new per-call state. The factory and pipeline run
only after `begin`; use the pipeline-start callback for an agent greeting. Do not greet
before the pipeline starts. The output transport handles resampling to 16 kHz, paces
playout and relays Pipecat interruption frames; continuous silence generation is disabled.

`demo_pipecat.py` is a complete small pipeline with Silero VAD, Cartesia STT/TTS and Together
Llama. It demonstrates the factory and both opening modes:

```sh
uv sync --frozen --all-extras
# Set CARTESIA_API_KEY, TOGETHER_API_KEY and PIPECAT_FACTORY=demo_pipecat:build.
uv run --all-extras python server.py pipecat
```

## Developer conformance check

Run against the actual endpoint before asking Dialt to call it:

```sh
uv run python conformance.py wss://your-adapter.example/ --duration 15
# Include a recorded question, played five seconds after begin:
uv run python conformance.py wss://your-adapter.example/ \
  --input-wav question-16k-mono.wav --output-wav response.wav
# For an agent configured to wait for the caller:
uv run python conformance.py wss://your-adapter.example/ \
  --opening caller --input-wav question-16k-mono.wav
```

The token comes from `ADAPTER_TOKEN`, never a command-line argument. The command exits
nonzero on failed authentication enforcement, invalid handshake, pre-begin output, invalid
PCM, provider error, output after end, missing audio, a greeting later than eight seconds,
or failure to close after stop. JSON output reports time from `begin` to first audio,
audio byte count and observed interruptions. `ended:false` is normal when the caller stops
an otherwise ongoing agent. The optional WAV concatenates received speech, not a wall-clock
recording. This is a wire/audio smoke check, **not proof of answer quality or tool behavior**;
listen to the output and run hosted evals as well. It does not exercise optional evidence.

Offline tests use real loopback WebSockets and no credentials:

```sh
uv run --extra pipecat pytest -q
```

## Dialt run and eval case

Use the public Dialt SDK/API's run-level custom-agent configuration, not a new simulator:

```json
{
  "targets": ["custom"],
  "custom_agent": {
    "url": "wss://your-adapter.example/",
    "label": "My existing voice agent",
    "token": "<read from your secret manager>",
    "transcripts": false,
    "tool_events": false
  }
}
```

These are the custom-target fields to merge into your usual run request, not a complete
request. Only one custom agent is allowed per run; it can be compared with builtin targets.
`cases/opening_hours.json` exercises the demos' configured policy. Case target instructions
describe the builtin comparison agent; they do not reconfigure the custom agent. The
simulated caller and rubric stay inside Dialt. Use the case greeting for an agent-first
run; use `starter` instead when your agent is configured to wait for the caller.

## Wire contract v1

Hosted URLs must be public `wss://` endpoints on port 443, with no URL credentials, query
or fragment. Dialt DNS-pins connections and rejects private/reserved destinations. The
upgrade includes `Authorization: Bearer <adapter token>`.

1. Dialt sends `start` with `version:1`, `audio:{encoding:"pcm_s16le",sample_rate:16000,
   channels:1}`, a unique `session_id`, `opening:"agent"|"caller"`, `timeout_s`, and
   `capabilities:{transcripts:false,tool_events:false}`. No target prompt, rubric, or caller
   script is transmitted.
2. Adapter sends `ready` with `version:1` within 30 seconds. This means the bridge is
   prepared, not that the provider has greeted. It must wait for Dialt's `begin` before
   launching/greeting. Cold starts count toward the **eight-second greeting window**.
3. Binary frames in either direction are PCM16 little-endian mono 16 kHz, maximum
   **32,000 bytes (one second)**. Dialt input is paced in 20 ms frames including silence.
   Output is speech audio, not a continuous silent stream. Dialt allows at most **30 seconds
   of pending output**. These bridges buffer at most ten seconds of caller input while
   the provider starts; overflow fails rather than silently dropping speech.
4. Adapter `interrupt` clears pending playback. Dialt acknowledges with
   `playback_stopped` and `discarded_ms`. Pipecat/ElevenLabs have already cancelled local
   output through their native interruption handling; the acknowledgement needs no further
   action. LiveKit's minimal RTC limitation is described above.
5. Adapter `end` follows final audio. Keep the socket open for playback drain until Dialt
   sends `stop`. `error` carries a sanitized `message`. Stop/disconnect tears down provider
   resources; a provider exception is not reported as successful end.

For applications extending these examples with evidence, Dialt permits at most **2 MB total
JSON evidence**. A complete `transcript` has `role:user|assistant`, `text`, and `turn_id`
unique **per role**. Never label a partial transcript complete. `tool_call` has `id`, `name`
and object `args`; `tool_result` must match an outstanding call's **id and name exactly once**,
and includes JSON `result`, `outcome:succeeded|failed|unknown`, and boolean `verified`.
Evidence does not authorize Dialt to execute tools. Advertise these capabilities only when
your adapter actually provides them; these audio-only examples reject requests enabling them.
