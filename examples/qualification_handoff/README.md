# Qualification and specialist handoff

The agent collects the caller's need, region, and timeframe naturally, clarifies uncertain
answers, reads the details back, and asks for consent. Corrections require a revised readback
before transfer. `record_as_you_go=False` is the default for this recipe.

The single `start_handoff(summary, qualification)` call supplies the confirmed details once,
at handoff. The host checks required fields before storing the snapshot and requesting the
transfer. This avoids a recording tool round after each answer. It does not establish that
model-provided values are truthful; the conversation evals inspect that behavior.

If the caller declines or hangs up, the transcript remains the source for later extraction;
this recipe does not automatically extract abandoned conversations.

Run the conversation cases in text, then voice:

```sh
uv sync --frozen
uv run dialt-sim examples/qualification_handoff/evals --modality text
uv run dialt-sim examples/qualification_handoff/evals --modality voice
```

Local runs execute deterministic checks. Hosted judge checks assess the conversation and
supported final details:

```sh
uv run dialt-evals push examples/qualification_handoff/evals --modality text --wait
```

`workflow.py` owns the plan and callback state. Run the accepted case against its application
callback with `uv run python -u examples/qualification_handoff/with_callbacks.py`.
The callback rejects incomplete snapshots and handles repeated requests idempotently.

For a workflow that needs live structured answers, enable `record_as_you_go` in the plan and
state, declare the plan's recording tools, and use its per-answer callback. That adds tool
rounds and response latency. Keep declarations and simulation fixtures consistent with the
selected mode.

For phone calls, reuse `examples/integrations/twilio`. Your application owns dialing, routing,
availability, transfer destinations, and one qualification state per call. Route the handoff
tool to that state and your transfer integration.

## Validation limit

The corresponding Jasper component eval found that Gemini 3.1 Flash Lite can assume values
when a caller changes a detail during the final readback. Required-field validation only
checks shape and completeness. Validate clarification and final values on your model before
using this recording-off flow for real transfers; the current change is a draft.
