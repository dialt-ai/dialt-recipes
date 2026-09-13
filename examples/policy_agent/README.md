# Policy agent beside the call

A clinic appointment line with a policy it must keep: send a caller with urgent symptoms to
emergency services, never give clinical advice, take a complaint properly. The conversational
agent's instructions say nothing about any of that. The policy is declared once, as
`mode.policy` in the start frame, and Dialt runs a judge beside the session that watches the
transcript and steers the agent when a rule applies. The caller hears one agent; the policy
lives in a document the customer owns and can change without touching the prompt.

```python
mode = {"kind": "dialt", "instructions": ..., "tools": ..., "policy": {
    "subject": "a clinic appointment call",
    "rules": [
        {"id": "emergency", "when": "...", "action": "tool",
         "tool": {"name": "get_emergency_instructions", "arguments": {}}},
        {"id": "clinical_advice", "when": "...", "do": "...", "action": "next_turn"},
        {"id": "complaint", "when": "...", "do": "...", "action": "next_turn"},
    ]}}
```

`workflow.py` owns the rules. The judge detects `when` in one check; it does not plan actions
or run a second check of whether the reply was sufficient.

- `next_turn` adds quiet context without requesting another reply. Advice and complaint
  guidance asks the agent to supply only what is missing and continue from the caller's answer.
- `tool` calls a declared application tool with fixed arguments. Here a read-only tool returns
  the clinic's emergency routing instructions. Its ordinary completion result
  requests a response when the floor permits. The conversational agent voices the result;
  the tool does not supply a canned utterance or contact emergency services.

The examples use simulated tool results. A real clinic host supplies its own approved routing
instructions. Normal tool permissions, restrictions and deferred results apply. Use
`deferred: true, notify_on_complete: false` for tools such as silent audit logging. The host can design a
supervisor tool to return availability and prepare a transfer using the same contract.

The host gets `policy_flag` events; `tool_call_id` links an admitted policy action to normal
tool events. Admission does not prove success. Rules fire once per session by default and
admitted actions are not replayed by a transcript correction. Tool hosts still own idempotency.

The judge runs beside the conversational agent and can finish after its reply. Quiet guidance
avoids requesting a redundant response, but does not guarantee generated language never
repeats. Tool completion can request an intermediate response without interrupting current
speech. A late flag cannot undo an action or speech. Rules that must hold before speaking
belong in the primary instructions. Full contract: Dialt's `docs/policy-agent.md`.

## Evals

Both sets are rendered from `workflow.py` by `render_cases.py`, so each case carries the exact
prompt, tools and policy the recipe runs. Every case uses standard `policy_flag` checks for expected occurrences, delivery and forbidden
rules. The target enables `report_checks` so absence assertions require complete evidence.

`evals/scripted/` isolates the judge: the caller says fixed lines, one per turn, whatever the
agent says. Positives for each rule (chest pain now, cannot breathe, thoughts of self-harm, a
double dose, a child's temperature, stopping a statin, asking for the manager) and the near
misses that must stay quiet (a past episode, a relative's stroke, a practical question, thanks,
a clean reschedule). `evals/full_call/` runs natural calls where the caller has a goal and, in
three of the four, says something a rule covers.

`host.py` runs them against a live broker and prints the flags, shared SDK checks and individual
transcripts. The same checks run when the cases are pushed to hosted runs:

```sh
uv sync --frozen
uv run python -u examples/policy_agent/host.py                       # both sets, text
uv run python -u examples/policy_agent/host.py examples/policy_agent/evals/scripted
DIALT_MODALITY=voice uv run python -u examples/policy_agent/host.py
```

Hosted runs (`dialt-evals push`) evaluate the same policy assertions. Both runners wait for the
final policy completion watermark. The local host continues answering dispatched target tools
while final monitoring drains, with conversation relays stopped. Errors, incomplete monitoring and truncated event evidence
fail policy checks, including assertions that a rule did not fire.

## On a phone call

Nothing extra. `examples/integrations/twilio` builds a start-frame mode per call; add `policy`
to it and listen for `policy_flag` in `BridgeHooks.on_event` if the application wants a record
of each flag. There is no second session to open or close and no injection to retry: the broker
does both.


## Optional occurrence and enforcement example

`workflow.extended_mode()` leaves the basic clinic example unchanged and adds:

- A complaint flag for each matching source utterance.
- ASR correction rechecks and combined guidance from one completed check.
- An identity-mismatch rule blocking `reschedule_appointment`.
- A pending-check hold and failure block on that tool, enforced by Dialt before dispatch.

Run `host.py examples/policy_agent/evals/extended` for simultaneous rules, repeated complaints, an identity
mismatch and a spelling-correction near miss. The last case is a conversational correction, not a synthetic
ASR revision event. The platform's component tests exercise actual ASR revision identity, stale
judge results, restriction retraction and resumed state; the recipe does not imitate those internals.

For phone calls use the same extended mode with the Twilio bridge. Customer-specific approvals
still use the existing application permission interface. A policy flag does not authorize a tool,
and a correction cannot undo an external action already dispatched. Listen for `policy_error`
through the ordinary event hook to surface monitoring failures; no extra model credentials are
needed. SDK requirement: `dialt-sdk>=0.29.0`.

Live text checks on production, 2026-09-13, with SDK 0.29.0: advice, complaint and a practical
near miss passed. The emergency case gave the expected guidance and called the routing tool,
but failed the final monitoring assertion in both runs because the short call closed before
the judge settled. The first run also exposed the local host dropping a tool call during drain;
the host now returns its result. This does not make broker closure wait for the judge.
[Individual results](results/2026-09-13-tool-actions.json) retain both successful and failed runs.
These are service tests of detection and delivery, not a guarantee that every call is fully
monitored before hangup. Older results describe their recorded revision only.
