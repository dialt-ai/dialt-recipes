"""Test-only assessment of monitoring when the target deliberately hangs up."""
from dialt.policy import PolicyEvidence


def check_policy_at_hangup(case, report):
    """Keep incomplete rule checks unknown; separately verify truthful closure evidence.

    Continuing calls still require the SDK's complete policy evidence. A target hangup
    can cancel the final check by design. Already-observed forbidden flags still fail.
    This changes test assessment only, never the conversation or recorded events.
    """
    checks = list(report.check_results)
    policy_checks = [c for c in case.checks if c.get("type") == "policy_flag"]
    if not policy_checks or report.termination_reason != "completed" or report.error:
        return checks
    events = [e for e in report.events if e.get("side") == "target"]
    ended = any(e.get("type") == "tool_call" and e.get("name") == "end_call" for e in events)
    errors = [e for e in events if e.get("type") == "policy_error"]
    evidence = PolicyEvidence()
    for event in events:
        evidence.record(event)
    if evidence.version <= 0 or not ended or not errors or any(e.get("reason") != "connection_closed" for e in errors):
        return checks
    # A closure marker cannot override an already-complete healthy final watermark.
    without_close = PolicyEvidence()
    for event in events:
        if event.get("type") != "policy_error":
            without_close.record(event)
    if without_close.complete or without_close.failed:
        return checks
    known_violation = False
    for check in policy_checks:
        maximum = check.get("max_count")
        count = sum(flag.get("rule") == check["value"] and flag.get("status") != "retracted"
                    for flag in evidence.occurrences.values())
        if maximum is not None and count > maximum:
            known_violation = True
    if known_violation:
        return checks
    checks = [{**check, "pass": None, "skipped": True,
               "detail": "Unassessed: target hangup ended monitoring before the final revision settled."}
              if check.get("type") == "policy_flag" else check for check in checks]
    checks.append({"type": "policy_completion", "name": "unfinished monitoring is reported at hangup",
                   "pass": True, "status": "incomplete_at_hangup"})
    return checks
