# Agent-to-agent hand-off on one call

Two agents share one phone call: an obviously synthetic intake voice takes the patient's name,
date of birth and reason for calling, then a warm, human-sounding scheduling specialist picks
the call up and helps. It is one Dialt session with one conversation. Nothing is passed between
sessions and no second connection is opened, so the caller hears no gap at the seam. The
example is a clinic appointment line; swap the plan fields, the specialist's tools and the two
role texts for your own domain and the mechanics stay the same.

## How the pass works

The hand-off is a tool intake calls once the caller has confirmed the read-back:
`handoff_to_agent(summary, details, caller_confirmed)`. The host validates it (a call with
`caller_confirmed` false is refused, so a hand-off cannot land on an unconfirmed read-back) and
returns `handoff_complete`. Intake finishes its own turn: it tells the caller it is passing them
over and stops.

When that turn has closed, the host passes the call with `dialt_recipes.pass_call_to`, four
frames on the same session:

1. `set_instructions(specialist, new_speaker=True)`: the specialist's instructions replace
   intake's, and the broker folds everything said so far into a transcript the specialist holds.
   It reads intake's lines as a previous agent's, not as its own turns to continue.
2. `set_tools(specialist_tools)`: the lookup and reschedule tools arrive; the hand-off tool does
   not carry over.
3. `set_tool_choice({"tool": "lookup_patient"})`: the specialist's first turn must be the
   lookup. Every rule the specialist has depends on the record, and left to itself the model
   sometimes stated appointment or account facts it had never fetched (loan-servicing dev runs,
   2026-09-08). This is the API's own "the next turn must use this tool" contract, not a prompt
   rule; the host sets it back to `"auto"` when the first reply has closed.
4. `set_voice(specialist_voice)`: the voice changes from the specialist's first word.
5. `inject_context(note, reply=True)`: the host's one-line note (who was passed, why) makes the
   specialist speak first.

The broker refuses the fold while a reply is in flight, so the pass has to land between replies.
`dialt_recipes.HandoffBoundary` reads the session's own `turn`, `done` and `working` events and
returns `"pass"` when intake's hand-off turn has closed, whether that turn spoke a bridge and an
answer, closed after its bridge, or answered without a bridge, and `"release"` when the
specialist's first reply has closed. `host.py` shows the wiring.

Each agent has only its own instructions (`workflow.intake_instructions()`,
`workflow.specialist_instructions()`). Neither is told the other's rules, and the prompt does
not grow at the seam. The earlier design, both roles in one prompt with `set_tools` and
`set_voice` alone and a "you are now the specialist" note in the tool result, is what the
loan-servicing sessions of 2026-09-08 showed failing: the specialist's opening was generated as
the second half of intake's "connecting you now" sentence, under intake's prompt, and it
invented account facts at the seam.

Voices: `INTAKE_VOICE` (default `chime`) and `SPECIALIST_VOICE` (default `southern_us_female`)
are roster keys. An unknown key is ignored by the server and the call carries on in the intake
voice; `host.py` reports whether the session confirmed the switch.

## What each agent is told

Intake: the collection plan, that it is automated, and that the hand-off is its only tool. The
phase boundary is also a tool contract: the session starts with only `handoff_to_agent`
declared (`intake_tools()`), so however the caller front-loads their details, intake cannot look
the patient up or act as the specialist early. With every tool declared from the start, the model
skipped the hand-off in two of three runs when a caller gave everything in one breath.

Specialist: three rules that came out of live calls, each with a case behind it. It is an
automated agent and there is no one else to transfer to (without this the model presented the
specialist as human and invented transfers). A patient's appointments are discussed only with
the patient (without this the specialist read another person's appointment out to whoever gave
the details). A change has happened only when the tool result says so.

## Two case sets

Both sets are rendered from `workflow.py` by `render_cases.py`, so the cases always carry the
prompt and tools the recipe runs; re-render after editing either. Every case starts as intake;
the specialist's instructions arrive with the pass and are never in a case document.

`evals/intake/` declares only the hand-off tool, exactly as a real host starts the call. The
fixed hand-off result tells the assistant no specialist follows and to end the call, so the
judge sees a complete intake and nothing else. These run hosted or locally:

```sh
uv sync --frozen
uv run dialt-sim examples/agent_to_agent_handoff/evals/intake --modality text
uv run dialt-evals push examples/agent_to_agent_handoff/evals/intake --modality text --wait
```

`evals/full_call/` declares every tool for its fixtures and runs the whole call. They are for
`host.py`, which starts each one as intake, passes the call when the hand-off turn closes, and
reports whether the pass was accepted and the voice confirmed:

```sh
uv run python -u examples/agent_to_agent_handoff/host.py                # voice, the default
DIALT_MODALITY=text uv run python -u examples/agent_to_agent_handoff/host.py
```

The full-call cases are `host.py`-only by construction: hosted runs and `dialt-sim` cannot pass a
call, and their fixed hand-off result ends the call at the hand-off.

## On a phone call

Reuse `examples/integrations/twilio`. Build the state and boundary per call, keep the session
from `on_connected`, feed every event to the boundary from `on_event`, and route
`handoff_to_agent` to `HandoffState.handoff`:

```python
def call_hooks():
    """One HandoffState, one boundary and one live session per call; never share them."""
    state, boundary, live = HandoffState(), HandoffBoundary(), {}

    async def on_connected(session):
        live["session"] = session

    async def on_event(event):
        phase = boundary.observe(event)
        if phase == "pass":
            await state.pass_call_on(live["session"])
        elif phase == "release":
            await state.release_on(live["session"])

    async def execute_tool(name, args):
        if name == "handoff_to_agent":
            result = state.handoff(args)
            boundary.landed = True
            return result
        ...

    mode = DialtMode(voice=state.intake_voice, instructions=intake_instructions(),
                     tools=intake_tools(), greeting=GREETING)
    return mode, BridgeHooks(execute_tool=execute_tool, on_connected=on_connected,
                             on_event=on_event)
```

Because the pass happens inside the Dialt session, Twilio sees one uninterrupted media stream.
Passing the caller to a human is a different operation and does not use this recipe: see the
permission-gated `request_human_handoff` tool in the Twilio example, which moves the call leg and
ends the session with `wrap_up`.
