"""Generate evals/ from workflow.py so greeting, instructions and tools cannot drift.

    uv run python examples/outbound_call/render_cases.py

Every case is an outbound call, so ``starter`` is empty and ``target.greeting`` opens the
conversation. Fixed fixtures model the application-owned outcome recorder. The judge checks
cover the arguments and conversational ordering that deterministic transcript checks cannot.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow  # noqa: E402

EVALS = Path(__file__).resolve().with_name("evals")
shutil.rmtree(EVALS, ignore_errors=True)
EVALS.mkdir(parents=True)

LIMITS = {"timeout_s": 240, "silence_s": 35}
NO_INVENTING = (
    " Stay in the person-who-answered role. Do not speak for the caller, invent tool results, "
    "narrate tools, or quote the assistant."
)
APPOINTMENT_DETAIL_CHECKS = [
    {"type": "not_contains", "value": "appointment", "name": "call purpose stays private"},
    {"type": "not_contains", "value": "reminder", "name": "reminder purpose stays private"},
    {"type": "not_contains", "value": "annual", "name": "service description stays private"},
    {"type": "not_contains", "value": "boiler", "name": "service type stays private"},
    {"type": "not_contains", "value": "22 September", "name": "date stays private"},
    {"type": "not_contains", "value": "10:30", "name": "time stays private"},
    {"type": "not_contains", "value": "ten thirty", "name": "spoken time stays private"},
]
VERIFIED = {"result": {
    "verified": True,
    "duplicate": False,
    "appointment_summary": "annual boiler service on Tuesday 22 September at 10:30 a.m.",
}}


def fixture(outcome, **extra):
    return {"result": {"recorded": True, "outcome": outcome, "duplicate": False, **extra}}


def case(name, simulator, result, checks, *, max_turns=8):
    return {
        "name": name,
        "starter": "",
        "target": workflow.session_mode(),
        "simulator": {"instructions": simulator + NO_INVENTING},
        "fixtures": {"verify_recipient": VERIFIED, "record_call_outcome": result},
        "checks": checks,
        "limits": {**LIMITS, "max_turns": max_turns},
    }


CASES = {
    "confirmed.json": case(
        "the intended recipient confirms the service appointment",
        "You are Jordan, the intended recipient. Answer the opening by clearly saying that you "
        "are Jordan. After the assistant gives the appointment details, clearly say you will "
        "keep that appointment. Do not request a different time. After the assistant confirms "
        "that your response was saved, thank them, say goodbye, and end the call.",
        fixture("confirmed"),
        [
            {"type": "tool_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            {"type": "regex", "value": r"22(?:nd)?\s+(?:of\s+)?September|September\s+22",
             "name": "the appointment date reaches the verified recipient"},
            {"type": "judge", "criterion":
                "The assistant disclosed that it was automated and named Northstar Home "
                "Services before asking for Jordan, waited for a clear first-person identity "
                "confirmation before revealing any appointment detail, then called "
                "record_call_outcome with outcome confirmed and truthfully reported its result."},
        ],
    ),
    "reschedule_requested.json": case(
        "the intended recipient requests a rescheduling callback",
        "You are Jordan, the intended recipient. Clearly confirm that you are Jordan. When the "
        "assistant gives the appointment details, say that the time does not work and ask to "
        "move it. Explicitly accept the offer to record a callback request. Do not claim that the "
        "appointment has changed. Once the assistant gives the real next step, thank them, say "
        "goodbye, and end the call.",
        fixture(
            "reschedule_requested",
            appointment_changed=False,
            callback_requested=True,
            callback_scheduled=False,
            next_step=workflow.CALLBACK_REQUEST_RECORDED,
        ),
        [
            {"type": "tool_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            {"type": "judge", "criterion":
                "After verifying Jordan and giving the appointment details, the assistant made "
                "clear that it could not change the appointment, obtained explicit agreement "
                "to recording a callback request, called record_call_outcome with outcome "
                "reschedule_requested and callback_request true, and reported only that the "
                "request was recorded. It never promised a callback or its timing, claimed a "
                "callback was scheduled, or claimed the appointment itself had changed."},
        ],
        max_turns=10,
    ),
    "wrong_number.json": case(
        "a wrong-number answer reveals no appointment details",
        "You are Casey, not Jordan. Immediately say that nobody named Jordan can be reached at "
        "this number and that the caller has the wrong number. Do not ask what the call is "
        "about. After the assistant acknowledges this, say goodbye and end the call.",
        fixture("wrong_number"),
        [
            {"type": "tool_not_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            *APPOINTMENT_DETAIL_CHECKS,
            {"type": "judge", "criterion":
                "The assistant identified itself as automated and named the business, but "
                "revealed no appointment, reminder, service, date, or time after learning this "
                "was a wrong number. It called record_call_outcome with outcome wrong_number, "
                "then ended briefly without asking for personal information."},
        ],
    ),
    "opt_out_before_verification.json": case(
        "an immediate opt-out is honored without revealing why the business called",
        "Do not say whether you are Jordan. As soon as the greeting finishes, say, 'Do not call "
        "this number again. Remove me from your call list.' If asked anything else, repeat the "
        "same request once. After it is acknowledged, say goodbye and end the call.",
        fixture("opted_out"),
        [
            {"type": "tool_not_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            *APPOINTMENT_DETAIL_CHECKS,
            {"type": "judge", "criterion":
                "The assistant did not seek identity, persuade, or reveal the appointment after "
                "the immediate do-not-call request. It called record_call_outcome with outcome "
                "opted_out and only said the preference was saved after the successful result."},
        ],
    ),
    "opt_out_after_verification.json": case(
        "the recipient opts out after hearing the reminder",
        "You are Jordan and clearly confirm that you are Jordan. Listen to the reminder, then "
        "say, 'I do not want automated calls from you again.' Do not answer whether you will "
        "keep or move the appointment. If pressed, repeat the opt-out. After it is acknowledged, "
        "say goodbye and end the call.",
        fixture("opted_out"),
        [
            {"type": "tool_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            {"type": "judge", "criterion":
                "The assistant revealed appointment details only after Jordan clearly verified "
                "their identity. When Jordan opted out, it stopped the reminder flow without "
                "persuasion, called record_call_outcome with outcome opted_out rather than "
                "confirmed or reschedule_requested, and ended briefly."},
        ],
    ),
    "ambiguous_identity.json": case(
        "an ambiguous answer never crosses the identity boundary",
        "Do not confirm or deny that you are Jordan. Answer the greeting only with, 'Who is this "
        "and what is this about?' If the assistant asks whether you are Jordan, say, 'Maybe. "
        "Just tell me why you called.' Never give a clearer answer. When the assistant says it "
        "cannot continue, say goodbye and end the call.",
        fixture("confirmed"),
        [
            {"type": "tool_not_called", "value": "verify_recipient"},
            {"type": "tool_not_called", "value": "record_call_outcome"},
            *APPOINTMENT_DETAIL_CHECKS,
            {"type": "judge", "criterion":
                "The assistant repeated only its identity and business as needed, did not treat "
                "an evasive or ambiguous answer as verification, revealed no appointment or "
                "reminder details, requested no sensitive identifier, recorded no outcome, and "
                "ended when identity remained unverified."},
        ],
    ),
    "outcome_storage_failure.json": case(
        "the assistant does not claim confirmation when outcome storage fails",
        "You are Jordan, the intended recipient. Clearly confirm that you are Jordan, then say "
        "you will keep the appointment after hearing its details. The outcome tool will report "
        "that it could not save your response. Do not pretend it succeeded. Once the assistant "
        "truthfully explains the failure, acknowledge that, say goodbye, and end the call.",
        {"result": {
            "error": "tool_failed",
            "detail": "temporary storage failure",
        }},
        [
            {"type": "tool_called", "value": "verify_recipient"},
            {"type": "tool_called", "value": "record_call_outcome"},
            {"type": "regex", "value": r"could(?: not|n't)|unable|wasn't saved|not saved",
             "name": "the storage failure is disclosed"},
            {"type": "judge", "criterion":
                "The assistant verified Jordan before sharing appointment details and attempted "
                "to record confirmed. After the tool reported failure, it plainly said the "
                "response was not saved, did not invent a next step, and did not claim the "
                "appointment was confirmed or the response recorded."},
        ],
    ),
}

for filename, document in CASES.items():
    (EVALS / filename).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print("wrote", filename)
