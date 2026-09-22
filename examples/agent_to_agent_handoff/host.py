"""Run full-call cases locally with the real host behaviour: the session starts as intake with
only the hand-off tool declared; after `handoff_to_agent`'s result is sent, the host requests an
acknowledged server-owned pass (`HandoffState.pass_call_on`). The specialist's instructions,
tools and voice are applied atomically; then a one-shot lifecycle trigger asks it to speak. Each
run checks the public handoff acknowledgement that confirms the handoff applied.

    uv run python -u examples/agent_to_agent_handoff/host.py [CASE_OR_DIR ...]

Defaults to `evals/full_call`. `DIALT_MODALITY` selects voice (default) or text.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from dialt_recipes import SimulationReport, run_simulation
from dialt_recipes.cli import _credentials, collect_cases

from workflow import HandoffState, intake_instructions, intake_tools


async def run_case(case, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    state = HandoffState()
    acks: list[dict] = []

    def handoff(args):
        return state.handoff(args)

    async def on_target_tool_result(name, args, result, outcome, verified, session):
        if name == "handoff_to_agent" and outcome == "succeeded" and verified:
            acks.append(await state.pass_call_on(session))

    case = replace(
        case,
        target={**case.target, "instructions": intake_instructions(),
                "voice": state.intake_voice, "tools": intake_tools()},
        fixtures={**case.fixtures, "handoff_to_agent": handoff},
    )
    report: SimulationReport = await run_simulation(url, api_key, case, modality=modality,
                                                    on_target_tool_result=on_target_tool_result)
    expects_handoff = any(check.get("type") == "tool_called"
                          and check.get("value") == "handoff_to_agent" for check in case.checks)
    application_checks = [
        {"type": "application_state", "name": "intake handed off with complete details",
         "pass": state.handed_off, "detail": "" if state.handed_off else "no handoff"},
        {"type": "application_state", "name": "the call was passed to the specialist",
         "pass": bool(acks) and all(a["switched"] for a in acks),
         "detail": "" if acks else "the hand-off turn never closed"},
    ] if expects_handoff else []
    passed = report.passed and all(check["pass"] for check in application_checks)
    return passed, {
        "case": case.name, "modality": modality, "passed": passed,
        "termination_reason": report.termination_reason, "error": report.error,
        "handoff": {"summary": state.summary, "details": state.details, "events": state.events},
        "checks": [*application_checks, *report.check_results],
        "transcript": report.transcript,
        "session_ids": {"target": report.target_session_id,
                        "simulator": report.simulator_session_id},
    }


async def main(paths: list[Path]) -> int:
    load_dotenv()
    url, api_key = _credentials()
    modality = os.environ.get("DIALT_MODALITY", "voice")
    failed = 0
    for case in collect_cases(paths):
        passed, summary = await run_case(case, url, api_key, modality)
        print(json.dumps(summary, indent=2))
        failed += not passed
    return 1 if failed else 0


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [Path(__file__).with_name("evals") / "full_call"]
    raise SystemExit(asyncio.run(main(targets)))
