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
        {"id": "emergency", "when": "...", "do": "...", "action": "speak_now"},
        {"id": "clinical_advice", "when": "...", "do": "...", "action": "next_turn"},
        {"id": "complaint", "when": "...", "do": "...", "action": "next_turn"},
    ]}}
```

`workflow.py` has the full rules. Each names what to look for (`when`), what the agent is told
when it applies (`do`), and how that lands (`action`):

- `speak_now`: the instruction is injected and a reply is requested when the floor is available. For the emergency rule: tell the caller to hang up and call emergency
  services, then end the call.
- `next_turn`: the instruction is injected quietly and the agent acts on it at its next turn.
  For advice and complaints, where the agent has usually already replied to the line that
  triggered the rule and the correction shapes what comes next.

The host gets a `policy_flag` event for each rule raised (`rule`, `action`, `evidence`,
`delivered`, `reply_started`), and the session record carries the policy, every flag and a
summary. Each rule is raised once per call, across reconnects. The contract, timing included,
is in the Dialt docs (`docs/policy-agent.md`).

Timing to be clear about: the check starts when a line completes, and the agent starts its
reply at the same moment. A `next_turn` note therefore shapes the reply after the one that
triggered it, and even a `speak_now` note lands after the agent has begun answering. The policy
agent is a backstop. A rule the agent must never break goes in the agent's instructions as well.

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
final policy completion watermark. Errors, incomplete monitoring and truncated event evidence
fail policy checks, including assertions that a rule did not fire.

Reference run on production, text mode, 2026-09-07: 16 of 16 cases, every expected rule raised
and delivered, nothing raised on the five quiet cases. One finding is baked into the cases:
urgent symptoms are, by the rules' own wording, also a question about whether symptoms are
serious, so the judge raised `clinical_advice` next to `emergency` on two of the four emergency
calls. The emergency instruction is the one spoken and it ends the call, so emergency cases
forbid only `complaint`. Two rules that overlap in `when` will co-fire; write them so they do not
if that matters.

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
needed. SDK requirement: `dialt-sdk>=0.28.0`.

Release check on dev, text mode, 2026-09-12: all 12 scripted cases passed the shared SDK
assertions. [Individual results](results/2026-09-12-dev-scripted.json) retain flags, checks and
transcripts. These checks establish detection and delivery; they do not prove that every
subsequent conversational response follows the guidance.
