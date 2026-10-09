# Dialt recipes

Build voice assistants with the public [`dialt-sdk`](https://pypi.org/project/dialt-sdk/) and [`@dialt/sdk`](https://www.npmjs.com/package/@dialt/sdk). These runnable examples show how to connect tools, hand off between assistants, conduct research interviews and evaluate conversations.

Dialt is a conversational voice engine. You can tune how your voice assistants sound, speak and act. Model training on your data is currently arranged with the [Dialt team](mailto:hello@dialt.com?subject=Training%20with%20Dialt). These recipes cover integration and application workflows.

Dialt owns conversation. Recipes own application policy and orchestration. There is no parallel simulator client and no eval-only conversation engine. A
simulated user is another Dialt session. Text and voice select different I/O on the same
session primitive.

## Integrations

[`examples/integrations/twilio`](examples/integrations/twilio) is the maintained inbound Twilio
Media Streams bridge. It is a standalone uv project with configuration instructions and offline
tests, including the optional customer-controlled human handoff.

```sh
cd examples/integrations/twilio
cp env.example .env
uv sync --frozen
uv run pytest -q
```

## Install

```sh
uv sync --frozen
cp .env.example .env
```

Python recipes require `dialt-sdk>=0.43.0`; the browser example uses `@dialt/sdk@0.53.0`.
These releases include breaking alpha cleanup; see the [migration guide](https://dialt.com/docs/api/migration/)
when updating an existing integration. The lockfiles pin the versions tested here.
They use the versioned API at `api.dialt.com`, including `/v1/evals` for local result reporting.

## Instructions carry the role

Every Dialt session needs `instructions`, and Dialt rejects a session without them. Dialt's
platform prompt covers what holds for any speaker on a call: the words are spoken aloud, turns
get interrupted, and the call's facts. It also carries the register of the selected voice. It
states no role. Your instructions say who the agent is, whose side it is on, what it does and
the policy it follows, including habits such as asking one short clarifying question when a
request is unclear. `voice` selects a voice: its TTS voice, register line and listening route
together. Every recipe here states its agent's role in `instructions`, and a simulated caller
gets its role from `simulator.instructions` in the same way.

## Qualification and specialist handoff

[`examples/qualification_handoff`](examples/qualification_handoff) collects configurable qualification fields, handles corrections, obtains explicit consent, and calls a customer-owned specialist handoff. It includes text and voice eval cases plus Python callback seams, and reuses the maintained Twilio bridge for live calls.

## Agent-to-agent hand-off on one call

[`examples/agent_to_agent_handoff`](examples/agent_to_agent_handoff) puts two agents on one
call: an obviously synthetic intake voice takes the patient's details and calls a hand-off tool.
The host binds the specialist configuration to that result with `continue_with`, atomically
recording the result, preventing an old-prompt answer, and applying the specialist's instructions,
tools, voice, context and first-reply tool choice. A clinic appointment line is the worked
example, with eval cases rendered from the workflow, a local host that confirms the continuation,
and a Twilio wiring sketch.

## Policy agent beside the call

[`examples/policy_agent`](examples/policy_agent) declares `mode.policy` on a clinic appointment
line: three rules, each with what to look for, what the agent is told when it applies, and
whether that is spoken at once or picked up at the agent's next turn. Dialt runs the judge
beside the session; the host only listens for `policy_flag`. Two eval sets rendered from the
workflow, scripted single-judgement calls and natural full calls, scored from the flags by a
local host with a precision and recall over rules.

## Guided customer-research assistant

[`examples/guided_customer_research`](examples/guided_customer_research) is a non-trivial guided
interview. A `ConversationPlan` declares the evidence to collect; an optional client tool records
answers. The model still handles wording, clarification, corrections, order and transitions
naturally.

Run the terminal version in text mode:

```sh
uv run dialt-guided examples/guided_customer_research/plan.json
```

The browser example supports text and voice with the same plan. Recording is off by default; enable it to show structured evidence live.
Serve the repository directory, open the example, and paste a short-lived scoped session key (not
a persistent `ck_` or `dk_` account key).

## Evals: run cases locally, then push them

A case is one JSON file, the same document the hosted evals API accepts: `name`, `starter` (or
`target.greeting`, when the agent opens the call and the simulated user answers it), `target`
(`instructions`, `tools`, `end_call`), `simulator` (`instructions`), `fixtures`, `checks` and
`limits`. Both sides are Dialt sessions, so `target.instructions` and `simulator.instructions` are
required and each states that side's role. [`examples/simulations/appointment_booking.json`](examples/simulations/appointment_booking.json)
is a complete one. Field reference: the [evals guide](https://dialt.com/docs/api/evals/).

The agent ends a call by calling the managed `end_call` tool, which `target.end_call` (default
true) gives it; set it to false for an agent that must never hang up, or to
`{"when": "the caller confirms the booking is complete"}` to state the condition in the
agent's own terms. The simulated user always has it. Either call is recorded as a tool call named `end_call`, so `completed` means the agent
ended the call and `simulator_ended` means the simulated user did.

Run a case, or every case in a directory, locally. Two Dialt sessions talk to each other: the
target agent and a simulated caller. Text forwards committed utterances; voice gives each session
a virtual microphone that streams for the whole call (the other side's audio at real time, line
noise in between, exactly as a phone line would) and never touches a speaker or microphone.

```sh
uv run dialt-sim examples/simulations/ --modality text
uv run dialt-sim examples/simulations/appointment_booking.json --modality voice
```

The deterministic checks (`contains`, `not_contains`, `regex`, `tool_called`,
`fixture_complete`, `max_turns`) run here with the same rules as hosted runs; `judge` checks
need the judge model and are reported as skipped locally. An undeclared tool fails closed, as it
does hosted, so a local pass means the case declares every tool the agent uses.

Push the same files and run them hosted. Cases are matched by name, so a re-push updates the
hosted case instead of duplicating it; the run appears on the
[Evals dashboard](https://dialt.com/evals).

```sh
uv run dialt-evals push examples/simulations/ --modality text --wait
uv run dialt-evals push examples/simulations/ --modality text --targets dialt dialt-smart --wait
```

`push` checks the run before starting it: every case against every target, with every problem
listed at once, and settings a target ignores printed as warnings. `--dry-run` stops there. An
external comparison (`--targets dialt openai-live`) reserves external credit per attempt; the
check quotes it, and the run starts only with `--accept-charge`. External models accept a shorter
`limits.timeout_s` than Dialt; `client.list_targets()` shows each target's `capabilities`, and the
[evals guide](https://dialt.com/docs/api/evals/#preflight) explains them. With `--wait`, a failed
attempt prints its failure code and `correlation_id`; quote that ID when reporting a problem.

Local simulations explicitly default the assistant to Circuit and the simulated caller to Irish
Male (`classic`), preserving case-specific voices. A `starter` is the caller's greeting in both
text and voice, so both sides receive it through the conversation relay; it is limited to 300
characters. Local simulations use Fast for the assistant and Smart for the simulated caller.
Hosted comparisons select assistant tiers through `--targets`; tier selection belongs to the run,
so case documents do not set `target.brain` or `simulator.brain`.

With `--wait` the command exits non-zero unless the run passes, so it can gate a CI job in your
agent's repository: put `DIALT_API_KEY` in a secret and run it on every change to the agent or
its cases. Without `--wait` it returns as soon as the run is queued.

To answer tool calls with your own Python instead of fixed fixtures, build the case in code; see
[`examples/simulations/with_callbacks.py`](examples/simulations/with_callbacks.py). Hosted runs
accept fixed and `field_store` fixtures only.

## Tests

```sh
uv run pytest
```

The default suite is offline and secret-free. The manual GitHub workflow runs a bounded live text
or blackholed-voice smoke when `DIALT_API_KEY` is configured.

Licensed under the [Apache License 2.0](LICENSE).
