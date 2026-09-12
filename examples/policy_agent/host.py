"""Run the cases against a live broker and score the policy agent from its events.

    uv run python -u examples/policy_agent/host.py [CASE_OR_DIR ...]   # DIALT_MODALITY=text|voice

Each case uses the same standard policy_flag checks locally and hosted. The shared SDK
reducer checks occurrence revisions and complete monitoring evidence; this host only prints
flags, checks and the individual transcript for review.

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


def flags_from(events: list[dict]) -> list[dict]:
    return [{key: value for key, value in event.items() if key not in {"side", "type"}}
            for event in events
            if event.get("side") == "target" and event.get("type") == "policy_flag"]


async def run_case(document: dict, url: str, api_key: str, modality: str) -> tuple[bool, dict]:
    case = SimulationCase.from_dict(document, modality=modality)
    report = await run_simulation(url, api_key, case, modality=modality)
    flags = flags_from(report.events)
    passed = report.passed
    return passed, {
        "case": case.name, "modality": modality, "passed": passed,
        "termination_reason": report.termination_reason, "error": report.error,
        "flags": flags,
        "checks": report.check_results,
        "transcript": report.transcript,
        "session_ids": {"target": report.target_session_id,
                        "simulator": report.simulator_session_id},
    }


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
    print(json.dumps({"cases": len(summaries), "passed": sum(s["passed"] for s in summaries)}))
    return 0 if all(summary["passed"] for summary in summaries) else 1


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [EVALS / "full_call", EVALS / "scripted"]
    raise SystemExit(asyncio.run(main(targets)))
