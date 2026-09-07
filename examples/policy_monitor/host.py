"""Run the full-call cases with the monitor attached, as a real host would.

    uv run python -u examples/policy_monitor/host.py [CASE_OR_DIR ...]   # DIALT_MODALITY=text|voice

Each case runs through `run_simulation` with a `PolicyMonitor` observing the target session.
The case's `policy` block says which rules must fire (`expect`) and which must not (`forbid`);
those become application checks next to the case's own checks. Judge checks only run hosted,
and hosted runs cannot attach a monitor, so this runner is where the recipe is proven.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import PolicyMonitor  # noqa: E402


async def run_case(document: dict, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    case = SimulationCase.from_dict(document, modality=modality)
    policy = document.get("policy") or {}
    monitor = PolicyMonitor(url, api_key)
    try:
        report = await run_simulation(url, api_key, case, modality=modality,
                                      on_target_event=monitor.observe)
        try:
            await asyncio.wait_for(monitor.settle(), timeout=monitor.check_timeout_s)
        except TimeoutError:
            pass
    finally:
        await monitor.close()
    fired = {flag["rule"] for flag in monitor.flags if flag.get("rule")}
    delivered = {flag["rule"] for flag in monitor.flags if flag.get("delivered")}
    errors = [flag for flag in monitor.flags if flag.get("error")]
    application_checks = [
        *({"type": "policy_flag", "name": f"{rule} fired and was delivered",
           "pass": rule in delivered,
           "detail": "" if rule in delivered else ("raised, not delivered" if rule in fired else "not raised")}
          for rule in policy.get("expect", [])),
        *({"type": "policy_flag", "name": f"{rule} did not fire", "pass": rule not in fired,
           "detail": "" if rule not in fired else "raised"}
          for rule in policy.get("forbid", [])),
        {"type": "policy_flag", "name": "no monitor errors", "pass": not errors,
         "detail": "; ".join(str(flag.get("error")) for flag in errors)},
    ]
    passed = report.passed and all(check["pass"] for check in application_checks)
    return passed, {
        "case": case.name, "modality": modality, "passed": passed,
        "termination_reason": report.termination_reason, "error": report.error,
        "flags": monitor.flags, "policy_checks": monitor.checks,
        "checks": [*application_checks, *report.check_results],
        "transcript": report.transcript,
        "session_ids": {"target": report.target_session_id,
                        "simulator": report.simulator_session_id},
    }


async def main(paths: list[Path]) -> int:
    load_dotenv()
    url, api_key = _credentials()
    modality = os.environ.get("DIALT_MODALITY", "text")
    failed = 0
    for path in paths:
        for document in load_cases(path):
            passed, summary = await run_case(document, url, api_key, modality)
            print(json.dumps(summary, indent=2))
            failed += not passed
    return 1 if failed else 0


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [Path(__file__).with_name("evals") / "full_call"]
    raise SystemExit(asyncio.run(main(targets)))
