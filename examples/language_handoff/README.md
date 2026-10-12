# Language handoff

A minimal browser voice assistant with one `transfer_language` tool and three voices:

| Language | Assistant | Voice |
| --- | --- | --- |
| English | Alex | `us_male` |
| Spanish | Sofía | `mexican_female` |
| Portuguese | Clara | `brazilian_female` |

The assistant transfers when the caller speaks another supported language or asks to switch.
Quoted phrases and translation requests stay with the current assistant. Unsupported languages
do not transfer. Each incoming assistant can transfer back. The conversation stays on one
session, retaining its history. The schematic highlights the active assistant as the call changes.

You can also enable `multilingual: true` on a single assistant and keep one voice for every
language, with no transfer tool. The selected voice retains its accent throughout the call.
This recipe adds handoffs so each language gets a different voice and accent.

## Run locally

Requires Node 22.18+; no build step or framework.

```sh
cd examples/language_handoff
npm ci
cp .env.example .env
# Set DIALT_API_KEY in .env.
npm start
```

Open http://localhost:3000, start the conversation and allow the microphone. Speak to begin;
there is no automatic greeting. Headphones help prevent the microphone picking up playback.
The plain UI uses Dialt's Manrope font, ink and orange from the
[design kit](https://github.com/dialt-ai/dialt/blob/main/docs/dialt-design-system.md).

## How it works

1. `public/agents.js` enables `mode.multilingual: true` at session start. This lets the listening
   pipeline understand all three languages and permits cross-language voice handoffs.
2. The assistant calls `transfer_language({ language: "es" })`, for example. Application code
   maps the language to instructions, tools and a voice.
3. `sendToolResult(id, result, { continueWith: { instructions, tools, voice, context } })`
   atomically records the tool result, applies the incoming configuration and begins its reply.
   The handler waits for `status: "applied"`. Rejections show Dialt's reason and leave the call
   connected. There is no close/reconnect.

`server.js` exchanges the persistent `.env` key for a short-lived scoped session credential.
Only the scoped key reaches the browser. `.env` is ignored and the server binds to loopback;
this is a local demo. A public deployment needs its own authenticated credential endpoint.

References: [Multilingual mode and handoff](https://dialt.com/docs/api/websocket/),
[Browser SDK](https://dialt.com/docs/api/browser/).

## Check

```sh
npm test
```

Offline tests cover multilingual start frames through the pinned SDK, all three handoffs,
rejections, timeouts, unsupported/same-language tool calls, and credential isolation.

For a live check, ask a simple question in English, then say “¿Puedes hablar español?”,
“Podemos falar português?” and “Can we speak English again?”. Check that each voice changes,
the matching assistant is highlighted, and it remembers the original question. Try a translation
request and an unsupported language to check that neither triggers a handoff. End the call
with **End conversation**.
