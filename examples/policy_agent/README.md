# Policy guidance

A clinic appointment line defines each condition and action once in `workflow.py`:
urgent symptoms, clinical advice, and complaints. Dialt includes those rules in the
agent's instructions and monitors completed caller and assistant turns in parallel.
Both settings are enabled here; developers can enable either independently.

```python
policy = {
    "include_instructions": True,
    "background_guidance": True,
    "rules": [{
        "id": "emergency",
        "when": "The caller reports urgent symptoms now.",
        "do": "Call get_emergency_instructions and relay the result, then end the call."
    }]
}
```

The conversational agent speaks and calls ordinary application tools. The background
model only injects quiet guidance identifying the caller or assistant turn it reviewed.
Late guidance does not request a second reply. The agent continues from completed steps
and the caller's latest decision. Tool hosts own permissions, consent, results and idempotency.
The emergency tool returns simulated routing information; it does not contact emergency services.

`extended_mode()` adds an identity-mismatch rule. The same policy contract tracks revisions
and repeated conditions without additional developer settings. Guidance cannot guarantee a
hard stop or undo an action already taken. A short call may end before monitoring settles;
errors and incomplete checks remain visible, not counted as successful monitoring.

The API contract lives in [Dialt's policy design](https://github.com/dialt-ai/dialt/blob/main/docs/policy-agent.md).
This recipe requires `dialt-sdk>=0.35.0`.

## Run the cases

`render_cases.py` generates the fixtures from the current workflow. `evals/scripted/`
checks fixed caller lines and near misses; `evals/full_call/` uses natural conversations;
`evals/extended/` covers repeated complaints, multiple conditions, identity concerns and
name corrections. The host uses the shared SDK policy evidence checks and prints individual
transcripts. A missing final monitoring watermark fails an absence assertion.

```sh
uv sync --frozen
uv run python -u examples/policy_agent/render_cases.py
uv run python -u examples/policy_agent/host.py examples/policy_agent/evals/scripted
uv run python -u examples/policy_agent/host.py examples/policy_agent/evals/full_call
uv run python -u examples/policy_agent/host.py examples/policy_agent/evals/extended
```

For phone calls, pass the same mode to `examples/integrations/twilio`. Listen for native
`policy_flag`, `policy_error` and `policy_settled` events through the ordinary event hook.
No separate model credentials or application injection loop are needed.

The live host checks monitoring through the final turn for continuing calls. If the target
explicitly hangs up before the monitor finishes, rule checks remain unassessed and a separate
boundary check verifies that incompleteness was reported. Observed forbidden flags and other
monitoring failures still fail. Speech, tool and semantic checks remain required.
