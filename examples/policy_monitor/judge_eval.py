"""Measure the policy judge on its own: replay scripted transcripts, score the flags.

    uv run python -u examples/policy_monitor/judge_eval.py [CASE_OR_DIR ...]

Each case in `evals/judge/` is a short transcript and what should happen: `expect` names the
rule and the 1-based line that carries the evidence, or is null for a call where nothing
applies. Lines are fed one at a time and the monitor settles after each, so the score says
which rule fired and how many lines after the evidence it came. The live session is a stub
that accepts every injection; nothing is steered here, only judged.

Reports per case, then precision, recall and mean lateness over the set. Exits 1 on any miss
or false positive.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from dialt_recipes.cli import _credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import CLINIC_RULES, PolicyMonitor  # noqa: E402

RULE_IDS = {rule.id for rule in CLINIC_RULES}


class AcceptingLive:
    """Stands in for the live session: records what would have been injected."""

    def __init__(self) -> None:
        self.injected: list[dict] = []

    async def inject_context(self, text, *, role, reply, message_id):
        self.injected.append({"text": text, "reply": reply})
        return {"accepted": True}


def load(paths: list[Path]) -> list[tuple[Path, dict]]:
    files = []
    for path in paths:
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    cases = []
    for file in files:
        document = json.loads(file.read_text())
        expect = document.get("expect")
        if expect is not None and (expect.get("rule") not in RULE_IDS
                                   or not 1 <= int(expect.get("line", 0)) <= len(document["transcript"])):
            raise SystemExit(f"{file}: expect must name a rule and a line in the transcript")
        cases.append((file, document))
    return cases


async def score(document: dict, url: str, api_key: str) -> dict:
    monitor = PolicyMonitor(url, api_key)
    live = AcceptingLive()
    await monitor.start(live)
    try:
        for who, text in document["transcript"]:
            monitor.note(who, text)
            await asyncio.wait_for(monitor.settle(), timeout=monitor.check_timeout_s + 5)
    finally:
        await monitor.close()
    fired = [flag for flag in monitor.flags if flag.get("rule")]
    expect = document.get("expect")
    hit = next((flag for flag in fired if expect and flag["rule"] == expect["rule"]), None)
    false_positives = [flag["rule"] for flag in fired if not (expect and flag["rule"] == expect["rule"])]
    return {
        "case": document["name"],
        "expected": expect["rule"] if expect else None,
        "hit": hit is not None,
        "lateness": (hit["after_line"] - expect["line"]) if hit else None,
        "false_positives": false_positives,
        "errors": [flag["error"] for flag in monitor.flags if flag.get("error")],
        "checks": monitor.checks,
    }


async def main(paths: list[Path]) -> int:
    load_dotenv()
    url, api_key = _credentials()
    results = [await score(document, url, api_key) for _, document in load(paths)]
    expected = [r for r in results if r["expected"]]
    hits = [r for r in expected if r["hit"]]
    fps = sum(len(r["false_positives"]) for r in results)
    for r in results:
        verdict = ("hit" if r["hit"] else "MISS") if r["expected"] else ("clean" if not r["false_positives"] else "FALSE POSITIVE")
        late = f" late by {r['lateness']}" if r["lateness"] else ""
        extra = f" false positives: {r['false_positives']}" if r["false_positives"] else ""
        errors = f" errors: {r['errors']}" if r["errors"] else ""
        print(f"{verdict:15s} {r['case']}  (expected {r['expected']}{late}{extra}{errors})")
    precision = len(hits) / (len(hits) + fps) if (hits or fps) else 1.0
    recall = len(hits) / len(expected) if expected else 1.0
    lateness = [r["lateness"] for r in hits]
    print(f"\nprecision {precision:.2f}  recall {recall:.2f}  "
          f"mean lateness {sum(lateness) / len(lateness) if lateness else 0:.1f} lines  "
          f"({len(results)} cases, {sum(r['checks'] for r in results)} policy turns)")
    return 0 if len(hits) == len(expected) and fps == 0 and not any(r["errors"] for r in results) else 1


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [Path(__file__).with_name("evals") / "judge"]
    raise SystemExit(asyncio.run(main(targets)))
