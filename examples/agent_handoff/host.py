"""Run full-call cases locally with the real host behaviour: intake starts with only the
hand-off tool declared, and when `handoff_to_agent` lands the host declares the specialist's
tools and switches the voice on the live target session. Each run confirms the switch from the
session's own `voice` event.

    uv run python -u examples/agent_handoff/host.py [CASE_OR_DIR ...]

Defaults to `evals/full_call`. `DIALT_MODALITY` selects text or voice (default voice: the
broker applies `set_tools` and `set_voice` on the voice path today, and this run is the check
that the swap actually happened).
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
from dialt_recipes.cli import collect_cases

from workflow import HandoffState, intake_tools


async def run_case(case, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    state = HandoffState()

    async def handoff(args, session):
        return await state.handoff_on(session, args)

    case = replace(
        case,
        target={**case.target, "voice": state.intake_voice, "tools": intake_tools()},
        fixtures={**case.fixtures, "handoff_to_agent": handoff},
    )
    report: SimulationReport = await run_simulation(url, api_key, case, modality=modality)
    switched = any(
        event.get("side") == "target" and event.get("type") == "voice"
        and event.get("voice") == state.specialist_voice
        for event in report.events
    )
    application_checks = [
        {"type": "application_state", "name": "intake handed off with complete details",
         "pass": state.handed_off, "detail": "" if state.handed_off else "no handoff"},
        {"type": "application_state", "name": f"session voice switched to {state.specialist_voice}",
         "pass": switched,
         "detail": "" if switched else "no voice event confirmed the switch (is the key on the roster?)"},
    ]
    passed = report.passed and all(check["pass"] for check in application_checks)
    return passed, {
        "case": case.name, "modality": modality, "passed": passed,
        "termination_reason": report.termination_reason, "error": report.error,
        "handoff": {"summary": state.summary, "details": state.details},
        "checks": [*application_checks, *report.check_results],
        "transcript": report.transcript,
        "session_ids": {"target": report.target_session_id,
                        "simulator": report.simulator_session_id},
    }


async def main(paths: list[Path]) -> int:
    load_dotenv()
    url = os.environ.get("DIALT_URL", "wss://dialt.com/ws")
    api_key = os.environ["DIALT_API_KEY"]
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
