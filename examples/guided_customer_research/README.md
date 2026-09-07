# Guided customer-research assistant

This assistant investigates how small support teams handle urgent escalations. The plan asks for
evidence, not a questionnaire script: Dialt chooses the order, follows useful threads and
clarifies ambiguity. When recording is enabled, `record_plan_field` is an ordinary client tool, so the application, not the
prompt, owns completion state.

The terminal example is the quickest way to try it:

```sh
uv run dialt-guided examples/guided_customer_research/plan.json
```

For the browser example, serve the repository, open this directory, and enter a short-lived scoped
session credential. Voice opens the microphone; text uses `sendText()` and no media pipeline.

## Optional live recording

The browser has a **Record answers as you go** checkbox. It defaults to the plan's
`record_as_you_go` setting (false when omitted). Each recorded answer adds a tool/result model
round and increases response latency.

Set `"record_as_you_go": false` in a JSON plan, or pass `record_as_you_go=False` to
`ConversationPlan`, to omit the recording tool and keep the conversation natural. The model
still collects and clarifies the required evidence. Structured `answers` stays empty and
`GuidedAssistant.complete` stays false because it describes recorded state, not model-inferred
conversation completion. Details remain in the transcript for later extraction; no extractor
is run automatically. A completion action can instead collect them once, as shown in the
qualification-handoff recipe.

To enable live structured recording, check the box or set `record_as_you_go` to true.
With the default off, this general interview keeps details in its transcript. The handoff
recipe separately saves its final details with the transfer request.
