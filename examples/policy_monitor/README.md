# Policy monitor beside the call

A second agent watches a live call and steers it. The conversational agent runs a clinic
appointment line and knows nothing about the policy. A `PolicyMonitor` in the host feeds the
call's transcript, a few lines at a time, to a policy session, and when that session raises a
flag the host injects the rule's instruction into the live session. The caller hears one agent;
the policy lives in a document the customer owns and can change without touching the prompt.

The policy session is an ordinary Dialt session in text mode. Its instructions are the rules
(`policy.py`, `CLINIC_RULES`), each with what to look for and what the agent should do, and its
only tool is `raise_flag(rule, evidence)`. Its text replies are ignored; only the tool calls
matter. So the monitor needs nothing beyond Dialt: no second model provider, no extra key.

## How a flag lands

Each rule names an action:

- `speak_now`: `inject_context(..., reply=True)`. The agent speaks to it at once, without
  waiting for the caller. For the emergency rule: tell the caller to hang up and call
  emergency services, then end the call.
- `next_turn`: `inject_context(..., reply=False)`. The note sits in the history and the agent
  acts on it at its next turn. For advice and complaints, where the agent has usually already
  replied to the line that triggered the rule and the correction shapes what comes next.

The broker refuses an injection while a reply is in flight, so the monitor retries every half
second, with one idempotency key per flag, and gives up after twenty seconds (`deliver_attempts`
times `deliver_interval_s`), recording the failure on the flag. Each rule is raised once per
call. One check runs at a time; lines that arrive during a check are batched into the next
one, so a slow check never queues up.

Timing to be clear about: the check starts when a line completes, and the agent starts its
reply at the same moment. A `next_turn` note therefore reaches the agent one turn late, and
even a `speak_now` note lands after the agent has begun answering. The monitor is a backstop.
A rule the agent must never break goes in the agent's prompt as well.

## Two eval sets

`evals/judge/` measures the judge alone: a hand-written dataset of short transcripts, each
with the rule that should fire and the 1-based line carrying the evidence, or `null` for a
call where nothing applies. Positives, near misses that must stay quiet (a past episode, a
relative's illness, a practical question, thanks) and a clean call. Lines are replayed one at
a time against a stub live session, so the score says what fired and how late:

```sh
uv sync --frozen
uv run python -u examples/policy_monitor/judge_eval.py
```

`evals/full_call/` runs whole calls with the monitor attached, through `host.py`, which
passes `PolicyMonitor.observe` to `run_simulation(on_target_event=...)`. Each case carries a
`policy` block naming the rules that must fire and the rules that must not; they become checks
next to the case's own. The cases are rendered from `workflow.py` by `render_cases.py`.

```sh
uv run python -u examples/policy_monitor/host.py                 # text, the default
DIALT_MODALITY=voice uv run python -u examples/policy_monitor/host.py
```

Hosted runs cannot attach a monitor, so the full-call set is `host.py`-only.

## On a phone call

Reuse `examples/integrations/twilio`: build one `PolicyMonitor` per call, open it from
`on_connected` (a connect must not sit in the bridge's event loop, which carries the audio),
and pass its `observe` as `BridgeHooks.on_event`:

```python
def call_hooks(url, api_key):
    monitor, live = PolicyMonitor(url, api_key), {}

    async def on_connected(session):
        live["session"] = session
        await monitor.start(session)

    async def on_event(event):
        await monitor.observe(event, live["session"])

    return monitor, BridgeHooks(execute_tool=..., on_connected=on_connected, on_event=on_event)
```

Close the monitor when the call ends; it closes the policy session. If the policy session
fails to connect or drops mid-call, the monitor marks itself dead and the call carries on
without it; `flags` records why.
