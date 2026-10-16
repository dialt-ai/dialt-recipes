# Dialt + Telnyx inbound bridge

This recipe connects an inbound Telnyx Call Control phone call to one Dialt session. Telnyx owns the phone number and call; this server owns media transport and application tools; Dialt owns conversation.

Deploy this bridge with your own Telnyx account, phone number and Dialt API key. Replace the example hostname with your own deployment's HTTPS origin. This recipe provides no shared phone number or shared hosted bridge, and you should never point your number at another operator's deployment.

## Run it

From this directory:

```sh
cp env.example .env
uv sync --frozen
uv run uvicorn bridge:app --env-file .env --host 0.0.0.0 --port 8000
```

Set `DIALT_API_KEY`, `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` and `PUBLIC_BASE_URL` in `.env`. Keep persistent API keys on the server. `PUBLIC_BASE_URL` is the external HTTPS origin with no path, query or credentials, for example `https://voice.example.com`. The reverse proxy must support WebSocket upgrades to `/media` and preserve Telnyx signature headers.

Get the account's base64 Ed25519 public key from Telnyx Mission Control's API Keys page or authenticated `GET https://api.telnyx.com/v2/public_key` (`data.public`). It is separate from the API key. The bridge verifies signatures over exact webhook bytes and the configured WebSocket URL and rejects timestamps outside five minutes. Keep the host clock synchronized.

1. Ensure the phone number is active and its purchase/verification is complete.
2. Create a **Voice API / Call Control application** in Mission Control with webhook URL `https://voice.example.com/voice`, method POST, API v2. This recipe uses Call Control, not TeXML or a Telnyx AI assistant.
3. Assign that application to the number's **Connection/Application** field.
4. Deploy this server with HTTPS/WSS reachable by Telnyx.
5. Call the number and check greeting, caller speech, reply audio, interruption, caller hangup and agent hangup. These calls incur normal Telnyx and Dialt usage charges.

The webhook answers incoming `call.initiated` events and starts bidirectional streaming in the same command. A deterministic `command_id` makes webhook retries reuse the same answer command within Telnyx's deduplication window. API failures return 503 so Telnyx can retry. A production host needing durable processing should queue authenticated events and enforce event-ID deduplication across workers before returning 200. Telnyx's command deduplication is time-bounded.

## Conversation and tools

Edit `instructions.md` for the application's role. Set `DIALT_INSTRUCTIONS_FILE` to use another file; relative paths resolve against this directory. Missing or empty instructions fail startup. Optional `DIALT_VOICE` selects a Dialt voice. `DIALT_GREETING` is the interruptible first assistant turn; `false`, `off` or `none` disables it.

Add tools in `tool_manifest()` and `execute_tool()`. Tools run on the server. Raise `dialt_recipes.twilio.ToolError` for a short caller-safe failure detail; other exception text stays in host logs. No human transfer destination or dialer is included in this inbound reference.

## Media contract

The bridge requests and validates mono PCMU at 8 kHz. It decodes and resamples inbound caller audio to Dialt's 16 kHz input, requests native 8 kHz assistant output, and sends base64 PCMU payloads in 20 ms frames. These are raw codec payloads, without WAV or RTP packet headers. Outgoing Telnyx frames contain no Twilio `streamSid` field. Only the inbound track is forwarded, avoiding assistant-audio feedback.

`dialt_recipes.telnyx.TelnyxMediaSocket` adapts Telnyx's wire envelope to the existing engine in `dialt_recipes.twilio`. Pacing, 100 ms playback marks, clear handling, tool dispatch and bounded playback drain have one implementation. A successful send is not playback evidence: marks acknowledge playback, and a clear invalidates pending marks before the returned acknowledgements arrive. Telnyx documents that media can arrive out of order; this reference forwards arrival order and does not implement a jitter/reordering buffer.

The session enables Dialt's managed `end_call` tool so the assistant can end the call when asked. After playback drains, the host explicitly hangs up via Call Control. Closing the media socket alone would leave the phone call alive. Transport failures also attempt hangup. The caller may already have hung up; Telnyx error 90018 and an already-disconnected media socket are accepted in that case.

## Persistent deployment on Railway

Build `examples/integrations/telnyx/Dockerfile` from the repository root. The image installs the frozen production dependencies, runs one Uvicorn worker as an unprivileged user and contains no credentials. The root `.railwayignore` limits CLI uploads to the Telnyx service and its package sources; the Dockerfile's own ignore file also excludes secrets, virtual environments and tests from the build context.

Create one service in the intended Railway project/environment. Set the Dockerfile path above, one replica, a 0.5 vCPU / 0.25 GB memory limit, sleep disabled, restart policy `ALWAYS`, health check `/healthz`, 40-second drain and zero overlap. These are low-volume reference limits; qualify concurrent calls before raising traffic. This bridge needs no volume, GPU or database connection.

Generate a Railway domain targeting port 8000. Set `PUBLIC_BASE_URL` to its exact HTTPS origin, `PORT=8000` and the three credentials described above as service variables. Deploy from the repository root with a project token in `RAILWAY_TOKEN`:

```sh
railway up --project <project-id> --environment <environment-id> --service <service-id> --detach
```

This uploads the local source, including unpushed edits. Record the deployment ID and preserve the deployed source in Git before handing off maintenance. CLI uploads have no automatic GitHub branch trigger. A redeploy replaces live WebSockets, so release while the line is idle; the drain timeout does not provide seamless call migration.

Require deployment `SUCCESS`, one running instance, `GET /healthz` returning `200 {"status":"ok"}`, unsigned `POST /voice` returning 403 and an unsigned `/media` WebSocket upgrade returning 403. `/healthz` checks process/configuration readiness without calling Dialt or Telnyx and exposes no credentials. Monitor actual CPU/memory after startup. Routing and heard-audio qualification still require a real call after assigning the Telnyx application.

## Test it

```sh
uv run pytest -q
```

Offline tests need no credentials. A read-only balance request proves API authentication only. It does not qualify number routing, media delivery or caller-heard audio. Complete the real-call checks above before production use.

Protocol references: [media streaming](https://developers.telnyx.com/docs/voice/programmable-voice/media-streaming), [answer call](https://developers.telnyx.com/api-reference/call-commands/answer-call), and [webhook verification](https://developers.telnyx.com/docs/development/api-fundamentals/webhooks/receiving-webhooks).
