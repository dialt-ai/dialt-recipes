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
returns `handoff_requested`. Intake finishes its own turn: it tells the caller it is going to pass them
over and stops.

After the host sends the hand-off tool result, it calls `session.handoff_agent(...)`. The server
waits for the outgoing turn, then atomically folds its history and applies the specialist's
instructions, tools, voice and the handover note as incoming context. A caller who starts talking
as that acknowledgement arrives therefore reaches an agent that already has the confirmed details.

This recipe keeps its existing one-shot `set_tool_choice({"tool": "lookup_patient"})` after an
accepted switch: live evidence requires the specialist to look up the record before speaking
about an appointment. It then calls `inject_context("The agent handoff is complete.", reply=True)`
once. That is a lifecycle trigger, not prescribed spoken wording. If the caller has claimed the
floor, its acknowledgement may be rejected; the handoff remains applied, the agent still has the
note, and the host does not retry or infer idleness.

The host records separate `handoff_applied` and `reply` outcomes. An opener rejection is never a
reason to send another handoff, repeat the note, or attempt a second fold. `host.py` uses the
post-tool-result simulation hook, so its acknowledgement wait does not block event relaying.

Each agent has only its own instructions (`workflow.intake_instructions()`,
`workflow.specialist_instructions()`). Neither is told the other's rules, and the prompt does
not grow at the seam. The earlier design, both roles in one prompt with `set_tools` and
`set_voice` alone and a "you are now the specialist" note in the tool result, is what the
loan-servicing sessions of 2026-09-08 showed failing: the specialist's opening was generated as
the second half of intake's "connecting you now" sentence, under intake's prompt, and it
invented account facts at the seam.

Voices: `INTAKE_VOICE` (default `chime`) and `SPECIALIST_VOICE` (default `southern_us_female`)
are roster keys. The handoff acknowledgement confirms that the atomic switch applied; use the
recorded session's `prompt_config` when an audit needs the resolved voice configuration.

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
`host.py`, which starts each one as intake, requests the pass after its hand-off tool result is
sent, and reports whether the public handoff acknowledgement says it applied:

```sh
uv run python -u examples/agent_to_agent_handoff/host.py                # voice, the default
DIALT_MODALITY=text uv run python -u examples/agent_to_agent_handoff/host.py
```

The full-call cases are `host.py`-only by construction: hosted runs and `dialt-sim` cannot pass a
call, and their fixed hand-off result ends the call at the hand-off.

## On a phone call

Reuse `examples/integrations/twilio`. Build state per call and route `handoff_to_agent` to
`HandoffState.handoff`. `BridgeHooks.on_tool_result` runs after the bridge has sent that result,
in the tool task rather than its media/event loop:

```python
def call_hooks():
    """One HandoffState per call; never share it."""
    state = HandoffState()

    async def execute_tool(name, args):
        if name == "handoff_to_agent":
            return state.handoff(args)
        ...

    async def on_tool_result(name, args, result, outcome, verified, session):
        if name == "handoff_to_agent" and outcome == "succeeded" and verified:
            await state.pass_call_on(session)

    mode = DialtMode(voice=state.intake_voice, instructions=intake_instructions(),
                     tools=intake_tools(), greeting=GREETING)
    return mode, BridgeHooks(execute_tool=execute_tool, on_tool_result=on_tool_result)
```

Because the pass happens inside the Dialt session, Twilio sees one uninterrupted media stream.
Passing the caller to a human is a different operation and does not use this recipe: see the
permission-gated `request_human_handoff` tool in the Twilio example, which moves the call leg and
ends the session with `wrap_up`.
