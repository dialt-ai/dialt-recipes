# Dialt + Twilio outbound appointment reminder

This runnable recipe places one pre-authorized service-appointment reminder call through
Twilio and connects the answered call to one Dialt session. It is deliberately a one-call CLI,
not a campaign runner or bulk dialer.

The worked conversation identifies the business and that the caller is automated, confirms
that it reached the intended person before disclosing appointment details, and records one of
the bounded outcomes implemented by the application. A wrong number or opt-out ends the
conversation without further disclosure or persuasion. Recording is off.

## Ownership boundaries

- Your application owns the contact and appointment record, consent evidence, do-not-call
  checks, permitted countries and local calling time, the decision to launch, and the final
  business outcome.
- `dialer.py` validates one local call request, reserves its idempotency key in the state store,
  and asks Twilio to create the call. It never chooses recipients itself.
- `host.py` authenticates Twilio callbacks, resolves the opaque attempt ID to server-side state,
  creates a per-call workflow, and connects Twilio audio to the shared
  `dialt_recipes.twilio` transport.
- Twilio owns the outbound phone leg, answering-machine classification, call status, and Media
  Stream.
- Dialt owns the realtime voice conversation. The initial model context contains the business
  and recipient's first name, but no appointment facts. A host-bound verification tool releases
  the bounded appointment summary only after explicit recipient confirmation. The model cannot
  choose a phone number or launch another call.

Twilio's terminal `completed` status means that a phone leg connected; it does not mean that
the intended recipient was reached or that the reminder was confirmed. The recipe stores
Twilio transport status and the conversation's business outcome separately.

## Configure it

Live testing needs an **upgraded Twilio account**, not a trial: Twilio's
[trial restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice#blocked-verbs)
block `<Stream>`, which this recipe requires for the Dialt audio connection. Use a purchased,
Voice-capable number in that account and an opted-in destination you control.

From this directory:

```sh
cp env.example .env
uv sync --frozen
```

Set every uncommented value in `.env`. `TWILIO_FROM_NUMBER` must be an account-owned,
outbound-capable E.164 number. Put the host behind public HTTPS and set `PUBLIC_BASE_URL` to
that exact external origin, without a path, query string, or trailing slash. Twilio signature
validation uses the public URL, so proxies must preserve or correctly forward its scheme and
host.

The checked-in lock file and `[tool.uv.sources]` override intentionally run this example against
the surrounding repository checkout. If you copy only this directory elsewhere, remove the
`[tool.uv.sources]` section from `pyproject.toml`, run `uv lock` once to resolve the declared Git
dependency, and then use `uv sync --frozen`.

Before a live call, replace the demonstration business name in `workflow.py`. Keep that
server-authored value separate from the per-call JSON. The recipe only stores callback requests;
it has no scheduling queue or coordinator integration and must not promise a callback or timing.

The default `OUTBOUND_MACHINE_DETECTION=human-only` connects only an answer webhook whose
`AnsweredBy` value is exactly `human`; every other or missing classification receives immediate
hang-up TwiML without appointment details. AMD is
heuristic, not identity verification. Setting it to `off` also permits voicemail and other
answered endpoints to reach the agent; this recipe does not implement a separate approved
voicemail message.

`OUTBOUND_ALLOWED_COUNTRIES` is a comma-separated ISO alpha-2 allowlist. Keep it narrow. The
ring timeout is bounded by `OUTBOUND_RING_TIMEOUT_S`; `OUTBOUND_MAX_ACTIVE_CALLS` limits
concurrent non-terminal attempts; and `recipient_timezone` in each request is checked against
the configured local calling window (09:00–20:00 by default).

## Describe exactly one authorized call

Create an untracked `call.json` beside `dialer.py`:

```json
{
  "idempotency_key": "reminder-1",
  "to": "+15551234567",
  "recipient_name": "Taylor",
  "recipient_timezone": "America/Los_Angeles",
  "appointment_summary": "Your service appointment is Tuesday, September 15 at 2:00 PM.",
  "consent_reference": "crm-consent-abc123"
}
```

These fields have intentionally different trust boundaries:

- `idempotency_key` names this exact business attempt. Reusing it must return the existing
  attempt rather than place another call.
- `to` must be an E.164 number in an allowed country. It is sent to Twilio but never exposed as
  a model-selected tool argument.
- `recipient_name` should contain only the first name needed in the greeting. It is used to
  verify whom the agent reached and is not enough on its own to disclose the appointment.
- `recipient_timezone` must be an IANA timezone and controls the recipient-local calling-window
  check.
- `appointment_summary` is kept in server-side attempt state until the host accepts explicit
  identity confirmation; the verification tool then returns this bounded detail to the agent.
- `consent_reference` is an application-owned audit reference, not proof that consent exists.

The sample cannot query your CRM, jurisdictional suppression lists, or enterprise consent
system. Before adopting it, replace or extend the eligibility seam so that a non-empty
`consent_reference` is verified against your authoritative record and the current destination
is checked against all internal and required do-not-call/opt-out lists. Never copy an arbitrary
user-supplied string into this file and treat it as consent.

Both `.env` and `call.json` are ignored by git. They still contain secrets or personal data on
disk; restrict access and delete or retain them according to your policy.

## Start the callback host and launch the call

Start the server first:

```sh
uv run uvicorn host:app --env-file .env --host 0.0.0.0 --port 8000
```

In another terminal, from the same directory, preflight the one explicit request. This is the
default and does not contact Twilio:

```sh
uv run python -u dialer.py call.json
```

Review the preflight result, then add the explicit paid-call flag to originate exactly one call:

```sh
uv run python -u dialer.py call.json --place-call
```

With `--place-call`, the CLI validates eligibility and persists the attempt before contacting
Twilio. The destination comes only from `call.json`; the caller ID comes only from
`TWILIO_FROM_NUMBER`. It does not iterate a file, fetch a list, retry a terminal failure, or
schedule a campaign. If Twilio's create-call response is ambiguous, reconcile that attempt by
its stored idempotency record and Twilio Call SID before deciding whether a new business attempt
is appropriate. Blind retries can create duplicate calls.

## What the webhooks do

The dialer gives Twilio URLs under `PUBLIC_BASE_URL`; these routes are not operator APIs:

- `POST /twilio/calls/{attempt_id}/answer` validates Twilio's signature against the exact
  request URL; cross-checks `AccountSid`, `CallSid`, `From`, `To`, and outbound direction against
  the stored attempt; records `AnsweredBy`; and applies the configured AMD rule. It returns
  `<Connect><Stream>` only for an allowed answer, otherwise `<Hangup>`. The stream receives only
  the opaque attempt ID as a TwiML `<Parameter>`; names, phone numbers, consent references, and
  appointment details remain server-side.
- `POST /twilio/calls/{attempt_id}/status` authenticates and idempotently records Twilio call
  lifecycle callbacks (`initiated`, `ringing`, `answered`, and `completed`, including terminal
  failure statuses reported by Twilio). Its sequence handling makes duplicates safe and ignores
  older deliveries; callbacks must not redial or overwrite the separate business outcome.
- `POST /twilio/calls/{attempt_id}/fallback` authenticates the failed TwiML request, records the
  failure, plays a generic failure apology with no appointment details, and hangs up. It never
  launches a replacement call.
- `POST /twilio/streams/status` authenticates and records Media Stream lifecycle diagnostics.
  It does not start a Dialt session or retry a call.
- `WS /twilio/media` validates the Twilio handshake and the first `start` frame, cross-checks
  its `AccountSid`, opaque attempt ID, and `CallSid` against stored state, atomically claims the
  eligible answered attempt for one `StreamSid`, and then creates exactly one call-scoped Dialt
  bridge. Duplicate streams, terminal attempts, rejected AMD results, and unknown or mismatched
  calls are closed.

Every HTTP request and WebSocket upgrade must carry a valid Twilio signature. Do not place
personal data in callback paths, Stream URLs, or query strings; this host rejects callback
query strings entirely because the generated URLs do not use them. Status callbacks can be
duplicated and reordered, and a media connection can disappear without a clean final message;
the persisted lifecycle is designed around those facts.

## Conversation behavior

`workflow.py` is the source of truth for the greeting, instructions, two application tools, and
generated eval cases. The agent:

1. identifies the business and says it is automated;
2. asks for the intended recipient without revealing the appointment;
3. after a clear first-person confirmation, calls
   `verify_recipient({"recipient_confirmed": true})`; only a successful host result marks the
   attempt verified and returns the appointment summary;
4. gives that returned summary and asks whether the recipient will keep the appointment or wants
   a scheduling callback;
5. calls `record_call_outcome` with `confirmed` or `reschedule_requested` only after verification,
   then ends; and
6. at any point, records and ends immediately on `wrong_number` or `opted_out`, neither of which
   requires identity verification or releases appointment details.

Both tools are bound to the current attempt by the host. Neither schema accepts a phone number,
recipient ID, appointment ID, arbitrary destination, or free-text note. A reschedule outcome
must include the recipient's explicit `callback_request` choice. The store rejects confirmed or
reschedule outcomes unless the attempt is verified, rejects a conflicting second outcome, and
stores opt-outs in the local suppression list. An opt-out is always accepted and persisted even
if it arrives during the goodbye after a different business response was recorded; the earlier
response remains intact. A model tool call is only a request: host validation and storage
determine whether it succeeded.

For rescheduling, a successful result means only that the preference was saved locally:
`callback_requested` reflects the recipient's choice, `callback_scheduled` is false, and
`appointment_changed` is false. Your application must arrange any follow-up separately. Do not
change the agent's wording to promise follow-up until an actual scheduling integration confirms it.

This gate keeps appointment facts out of the initial model prompt and enforces durable ordering,
but it is not independent identity authentication: the host validates the tool payload and
trusts the agent's interpretation of what the person said. The evals test that behavior. Use a
stronger customer-owned verification method before disclosing sensitive medical, financial, or
account information.

## Test and evaluate without placing a phone call

The test suite mocks Twilio and Dialt and must remain credential-free:

```sh
uv run pytest -q
```

Regenerate cases after changing the workflow, then exercise the same prompts and tool contracts
in text before spending time on voice:

```sh
uv run python -u render_cases.py
uv run dialt-sim evals --modality text
uv run dialt-sim evals --modality voice
uv run dialt-evals push evals --modality text --wait
```

The cases check that confirmed and reschedule flows call `verify_recipient` before appointment
disclosure, while wrong-number, pre-verification opt-out, and ambiguous-identity flows never
receive the appointment facts. They also cover opt-out after verification and a failed outcome
write. The fixed failure fixture exercises the exact error payload visible to the agent; fixed
hosted fixtures themselves are delivered with successful simulator metadata, so the offline
bridge test separately verifies that a real write exception is emitted with `outcome=failed`
and `verified=false`.

`dialt-sim` and the offline tests do not originate a Twilio call. Hosted evals require a Dialt
key and may incur usage, but also do not dial a telephone number.

## Safeguards you must keep

- Call only a person who has given valid consent for this purpose, and retain an auditable
  reference to it. Consent requirements vary by call type and jurisdiction; have your own
  counsel review the production workflow.
- Check the destination against your current internal opt-out list and every applicable
  do-not-call list immediately before launch. Persist an opt-out before ending the call and
  suppress future attempts outside this recipe too.
- Enforce the recipient's local calling window and allowed geography server-side. Do not trust
  the model, Twilio status, or a stale batch selection to make that decision.
- Keep `TWILIO_FROM_NUMBER`, country permissions, rates, concurrency, and fraud controls in
  trusted configuration. Do not expose them as model arguments.
- Reserve a unique idempotency key transactionally before `calls.create`. Treat timeouts and
  disconnected responses as ambiguous until reconciled; they are not permission to call again.
- Leave recording disabled unless you separately implement lawful notice/consent, access
  controls, retention, and deletion. Twilio Media Streams and Dialt session handling may still
  process audio and transcripts; document and secure that data path.
- Apply least privilege to the Twilio credential, keep `.env` out of source control, rotate
  leaked secrets, and never print auth tokens or full call payloads in logs.

## State store and production deployment

`OUTBOUND_STATE_DB` defaults to `outbound_calls.sqlite3`. The SQLite store is useful for this
single-process reference and is ignored by git, but it can contain phone numbers, names,
appointment text, consent references, call SIDs, and outcomes. Protect it as sensitive data.
The default path is relative to the working directory: run the host and CLI from this directory,
or configure the same absolute `OUTBOUND_STATE_DB` path for both.

Do not use one local SQLite file behind multiple workers, containers, or hosts. A production
deployment needs a shared durable store with transactional unique constraints for idempotency,
atomic status/outcome updates, encryption and access controls, retention/deletion policy, and
an audit trail. Configure load balancing so media and callback lifecycle remain consistent with
your store and concurrency model.

## Pre-merge live test plan

Live calls are intentionally never part of the automated test suite. Keep the PR unmerged until
the following checks have been completed with the tester. Hosted text/voice evals and real calls
incur usage; arrange them explicitly. Record the commit tested, pass/fail, and sanitized evidence
in the PR. Keep credentials, phone numbers, appointment details, and recordings out of the PR.

1. Run the offline tests and the seven generated conversation cases in text, then voice. Review
   transcripts for disclosure ordering, tool failures, and unsupported promises, not just scores.
2. Use an upgraded Twilio account, a Voice-capable caller ID permitted for the destination
   country, and an explicitly opted-in test number you control. Use synthetic appointment facts.
   Configure the real recipient timezone, narrow country allowlist, and max active calls of one.
3. Expose the running host at the exact HTTPS `PUBLIC_BASE_URL` with WebSocket support. Run the
   CLI and host against the same database. Confirm recording stays off.
4. Run `uv run python -u dialer.py call.json` without `--place-call`. It should report validation
   only and create no call in Twilio. An invalid country or out-of-window recipient must fail.
5. Launch once with `--place-call`. Confirm automated/business disclosure, no appointment facts
   until first-person identity confirmation, two-way audio, interruption, and a clean hang-up.
   Say you will keep the appointment. Expect `recipient_verified=true` and
   `conversation_outcome=confirmed`; check Twilio status and Stream events independently.
6. Rerun the identical request, including after restarting the host/CLI against the same database.
   Expect the same attempt and Call SID, `created=false`, and no second phone call. Never change
   an idempotency key to retry an ambiguous create response; reconcile it first.
7. In separate authorized calls with new keys, request rescheduling and accept, then decline a
   callback request. Expect the corresponding `callback_requested` boolean, no appointment
   change, and no promised/scheduled callback or timing.
8. Test ambiguous identity and an answering machine. Ambiguous identity must reveal no appointment
   facts and record no business outcome. Under `human-only`, a non-human/unknown AMD result must
   hang up without starting Dialt. Also allow one call to time out without answering; it should
   have a terminal transport status and no confirmed business outcome. Document AMD misclassification.
9. Test wrong-number and opt-out behavior last (they suppress future calls). Use separately
   consented test destinations or isolated test databases; never remove a real suppression to
   make a test pass. Test opt-out before verification and during goodbye after confirmation:
   the latter must preserve `confirmed` while setting `opted_out=true`. A fresh-key attempt to
   that destination must then fail before contacting Twilio, including after a process restart.
10. Review Twilio's call/error logs and local attempt records. Confirm no unexpected duplicate
    calls or signature failures, separate transport/business outcomes, and correct suppression.
    Resolve any failures and record tester sign-off before merging.

Stop after this bounded smoke and review the stored personal data. Add batching, schedules,
automatic retries, voicemail drops, call recording, or additional countries only as separate,
reviewed application features.
