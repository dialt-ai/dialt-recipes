from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv
from dialt import DEFAULT_REALTIME_URL

from dialt_recipes import SimulationCase, run_simulation

from workflow import QualificationState, spoken_reference_pattern


async def main() -> None:
    load_dotenv()
    source = Path(__file__).with_name("evals") / "accepted_handoff.json"
    case = SimulationCase.from_dict(json.loads(source.read_text()))
    state = QualificationState()
    fixed_reference = case.fixtures["start_handoff"]["result"]["handoff_reference"]
    fixed_reference_pattern = spoken_reference_pattern(fixed_reference)

    case = replace(
        case,
        fixtures={
            "start_handoff": state.start_handoff,
        },
        checks=tuple(
            {**check, "value": spoken_reference_pattern(state.handoff_reference)}
            if check.get("type") == "regex" and check.get("value") == fixed_reference_pattern
            else check
            for check in case.checks
            if check.get("type") != "fixture_complete"
        ),
    )
    report = await run_simulation(
        os.environ.get("DIALT_URL") or DEFAULT_REALTIME_URL,
        os.environ["DIALT_API_KEY"],
        case,
        modality=os.environ.get("DIALT_MODALITY", "text"),
    )
    missing = [field for field in state.required_fields if field not in state.answers]
    application_check = {
        "type": "application_state",
        "name": "all qualification fields recorded",
        "pass": not missing,
        "detail": "" if not missing else f"missing: {', '.join(missing)}",
    }
    passed = report.passed and application_check["pass"]
    print(
        json.dumps(
            {
                "passed": passed,
                "qualification": state.answers,
                "application_events": state.events,
                "checks": [application_check, *report.check_results],
                "transcript": report.transcript,
                "session_ids": {
                    "target": report.target_session_id,
                    "simulator": report.simulator_session_id,
                },
            },
            indent=2,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
