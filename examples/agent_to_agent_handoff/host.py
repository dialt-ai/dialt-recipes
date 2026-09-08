"""Run full-call cases locally with the real host behaviour: the session starts as intake with
only the hand-off tool declared; when `handoff_to_agent` lands the host resolves it, waits for
intake's turn to close, then passes the call (`HandoffState.pass_call_on`): the specialist's
instructions, tools and voice, and a note that makes it speak first. Each run confirms the
voice switch from the session's own `voice` event and that the note was accepted.

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

from dialt_recipes import HandoffBoundary, SimulationReport, run_simulation
from dialt_recipes.cli import _credentials, collect_cases

from workflow import HandoffState, intake_instructions, intake_tools


async def run_case(case, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    state, boundary = HandoffState(), HandoffBoundary()
    acks: list[dict] = []

    def handoff(args):
        result = state.handoff(args)
        boundary.landed = True
        return result

    async def on_target_event(event, session):
        if boundary.observe(event):
            acks.append(await state.pass_call_on(session))

    case = replace(
        case,
        target={**case.target, "instructions": intake_instructions(),
                "voice": state.intake_voice, "tools": intake_tools()},
        fixtures={**case.fixtures, "handoff_to_agent": handoff},
    )
    report: SimulationReport = await run_simulation(url, api_key, case, modality=modality,
                                                    on_target_event=on_target_event)
    switched = any(
        event.get("side") == "target" and event.get("type") == "voice"
        and event.get("voice") == state.specialist_voice
        for event in report.events
    )
    expects_handoff = any(check.get("type") == "tool_called"
                          and check.get("value") == "handoff_to_agent" for check in case.checks)
    application_checks = [
        {"type": "application_state", "name": "intake handed off with complete details",
         "pass": state.handed_off, "detail": "" if state.handed_off else "no handoff"},
        {"type": "application_state", "name": "the call was passed and the specialist's note accepted",
         "pass": bool(acks) and all(a.get("accepted") for a in acks),
         "detail": "" if acks else "the hand-off turn never closed"},
        {"type": "application_state", "name": f"session voice switched to {state.specialist_voice}",
         "pass": switched,
         "detail": "" if switched else ("no voice event confirmed the switch: is the key on the "
                                        "roster, and different from the intake voice?")},
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
