# Agent hand-off on one call

Two agent personas share one phone call: an obviously synthetic intake voice takes the caller's
name, account reference and reason, then a warm, human-sounding specialist voice picks the call
up and helps. It is one Dialt session with one conversation history. Nothing is passed between
sessions and no second connection is opened.

The hand-off is a tool. Intake calls `handoff_to_agent(summary, details)` once it has read the
details back. The host validates the details, declares the specialist's tools with `set_tools`,
switches the session voice with `set_voice`, and returns the handover note as the tool result. The model continues the same call as the
specialist, with everything intake collected still in its context. Both roles are in the
session instructions from the start (`workflow.instructions()`), so the hand-off changes nothing
about the prompt: the tool result is the boundary, and the voice change lands on the
specialist's first reply.

The phase boundary is also a tool contract. The host starts the session with only the hand-off
tool declared (`intake_tools()`), so however the caller front-loads their details, intake cannot
look the account up or act as the specialist early: its only move is the hand-off. When it
lands, the host declares the full manifest with `set_tools` and switches the voice. With every
tool declared from the start, the model skipped the hand-off in two of three runs when a caller
gave everything in one breath, and answered as the specialist itself.

Voices: `INTAKE_VOICE` (default `chime`) and `SPECIALIST_VOICE` (default `southern_us_female`)
are roster keys. An unknown key is ignored by the server and the call carries on in the intake
voice; `host.py` reports whether the session confirmed the switch.

## Two case sets

`evals/intake/` declares only the hand-off tool, exactly as a real host starts the call, and the
simulated caller hangs up once they hear they are being passed on. These are honest runs of the
intake persona, hosted or local, with judge criteria for the readback, a corrected reference,
and a caller who asks for a person:

```sh
uv sync --frozen
uv run dialt-sim examples/agent_handoff/evals/intake --modality text
uv run dialt-evals push examples/agent_handoff/evals/intake --modality text --wait
```

`evals/full_call/` declares every tool and runs the whole call. They are for `host.py`, which
starts each one with the intake manifest, swaps in the specialist's tools and switches the voice
when the hand-off lands, and reports whether the session confirmed the switch:

```sh
uv run python -u examples/agent_handoff/host.py                # voice, the default
DIALT_MODALITY=text uv run python -u examples/agent_handoff/host.py
```

Hosted runs and `dialt-sim` cannot swap a manifest mid-call, so a full-call case run through
either exercises the instructions alone, and the model may skip the hand-off. `host.py` defaults
to voice because the deployed broker applied `set_tools` and `set_voice` only on the voice path
when this recipe was written; text mode works once the broker change that handles both frames in
text sessions is deployed.

Two things the runs showed, kept here because they shape the design. With every tool declared
from the start, the model answered as the specialist without handing off in two of three runs.
And when the post-hand-off agent had no tools at all (an intake case run past the hand-off), it
invented a due date and narrated a payment change rather than saying it could not see the
account. Both are why the specialist's tools arrive with the hand-off and never before.

## On a phone call

Reuse `examples/integrations/twilio`. Keep the session from `on_connected`, and route
`handoff_to_agent` to `HandoffState.handoff_on(session, args)`:

```python
state, live = HandoffState(), {}

async def on_connected(session):
    live["session"] = session

async def execute_tool(name, args):
    if name == "handoff_to_agent":
        return await state.handoff_on(live["session"], args)
    ...

mode = DialtMode(voice=state.intake_voice, instructions=instructions(), tools=intake_tools())
hooks = BridgeHooks(execute_tool=execute_tool, on_connected=on_connected)
```

Because the switch happens inside the Dialt session, Twilio sees one uninterrupted media stream:
no language or voice change on the Twilio side, no second leg.

## Why not two sessions

A second session would need the first one's history replayed and would open an audio gap at the
seam. A voice switch keeps the history, keeps the endpointer state, and costs nothing the caller
can hear. Changing the instructions mid-call is also unnecessary: with both roles declared up
front, the tool result tells the model which role it is in.
