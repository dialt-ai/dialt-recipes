"""Run the cases against a live broker and score the policy agent from its events.

    uv run python -u examples/policy_agent/host.py [CASE_OR_DIR ...]   # DIALT_MODALITY=text|voice

Each case runs through `run_simulation`. The target's `mode.policy` is in the case document,
so the broker runs the policy agent; this host only reads the `policy_flag` events it emits.
The case's `policy` block says which rules must fire and be delivered (`expect`) and which must
not fire (`forbid`); those become checks next to the case's own. With no arguments both sets
run and a precision and recall over rules closes the report.

A real host does the same with far less: declare the policy in the start frame and listen for
`policy_flag`. Nothing here is needed to get the behaviour, only to prove it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dialt import load_cases
from dotenv import load_dotenv

from dialt_recipes import SimulationCase, run_simulation
from dialt_recipes.cli import _credentials

EVALS = Path(__file__).resolve().with_name("evals")


def policy_checks(policy: dict, flags: list[dict]) -> list[dict]:
    fired = {flag["rule"] for flag in flags}
    delivered = {flag["rule"] for flag in flags if flag.get("delivered")}
    return [
        *({"type": "policy_flag", "name": f"{rule} fired and was delivered",
           "pass": rule in delivered,
           "detail": "" if rule in delivered else
           ("raised, not delivered" if rule in fired else "not raised")}
          for rule in policy.get("expect", [])),
        *({"type": "policy_flag", "name": f"{rule} did not fire", "pass": rule not in fired,
           "detail": "" if rule not in fired else "raised"}
          for rule in policy.get("forbid", [])),
    ]


def flags_from(events: list[dict]) -> list[dict]:
    return [{key: value for key, value in event.items() if key not in {"side", "type"}}
            for event in events
            if event.get("side") == "target" and event.get("type") == "policy_flag"]


async def run_case(document: dict, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    case = SimulationCase.from_dict(document, modality=modality)
    policy = document.get("policy") or {}
    report = await run_simulation(url, api_key, case, modality=modality)
    flags = flags_from(report.events)
    checks = policy_checks(policy, flags)
    passed = report.passed and all(check["pass"] for check in checks)
    return passed, {
        "case": case.name, "modality": modality, "passed": passed,
        "termination_reason": report.termination_reason, "error": report.error,
        "flags": flags, "policy": policy,
        "checks": [*checks, *report.check_results],
        "transcript": report.transcript,
        "session_ids": {"target": report.target_session_id,
                        "simulator": report.simulator_session_id},
    }


def score(summaries: list[dict]) -> dict:
    """Precision and recall over (case, rule) pairs, from the cases' expect and forbid lists."""
    tp = fp = fn = 0
    for summary in summaries:
        fired = {flag["rule"] for flag in summary["flags"]}
        expect = set(summary["policy"].get("expect", []))
        forbid = set(summary["policy"].get("forbid", []))
        tp += len(fired & expect)
        fn += len(expect - fired)
        fp += len(fired & forbid)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"cases": len(summaries), "passed": sum(s["passed"] for s in summaries),
            "true_positives": tp, "false_positives": fp, "missed": fn,
            "precision": round(precision, 2), "recall": round(recall, 2)}


async def main(paths: list[Path]) -> int:
    load_dotenv()
    url, api_key = _credentials()
    modality = os.environ.get("DIALT_MODALITY", "text")
    summaries = []
    for path in paths:
        for document in load_cases(path):
            passed, summary = await run_case(document, url, api_key, modality)
            print(json.dumps(summary, indent=2))
            summaries.append(summary)
    print(json.dumps({"score": score(summaries)}))
    return 0 if all(summary["passed"] for summary in summaries) else 1


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [EVALS / "full_call", EVALS / "scripted"]
    raise SystemExit(asyncio.run(main(targets)))
