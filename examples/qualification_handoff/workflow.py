from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from dialt_recipes import ConversationPlan, PlanField

QUALIFICATION_PLAN = ConversationPlan(
    name="a specialist qualification conversation",
    objective="Understand the caller's need, region and timeframe before a specialist handoff.",
    fields=(PlanField("need", "The service or help the caller needs."),
            PlanField("region", "The caller's region."),
            PlanField("timeframe", "When the caller needs the service.")),
    completion="When all required details are supported, briefly read them back and ask "
               "whether the caller wants a specialist. Wait for confirmation before "
               "calling start_handoff. If their reply changes or casts doubt on a detail, "
               "resolve it and read back the revised details, then wait for confirmation "
               "again. Do not proceed to consent with unresolved required details.",
    tool_name="record_qualification", record_as_you_go=False,
)

_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
_SPOKEN_GAP = r"(?:[\s,.-]|dash)*"


def spoken_reference_pattern(reference: str) -> str:
    """A regex that matches `reference` as an agent reads it aloud: characters separated by
    spaces, hyphens, commas or the word "dash", and digits as numerals or words. A longer
    digit run does not match, so a different reference cannot pass."""
    parts = []
    for ch in reference.upper():
        if ch.isdigit():
            parts.append(f"(?:{ch}|{_DIGIT_WORDS[ch]})")
        elif ch.isalnum():
            parts.append(re.escape(ch))
    tail_digit = r"(?:\d|" + "|".join(_DIGIT_WORDS.values()) + r")\b"
    return _SPOKEN_GAP.join(parts) + f"(?!{_SPOKEN_GAP}{tail_digit})"


@dataclass
class QualificationState:
    """Application-owned state behind the recipe's two client tools."""

    required_fields: tuple[str, ...] = ("need", "region", "timeframe")
    record_as_you_go: bool = False
    answers: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    handoff_started: bool = False
    handoff_reference: str = "demo-handoff-001"

    def record(self, args: dict[str, Any]) -> dict[str, Any]:
        key = args.get("field")
        value = args.get("value")
        if key not in self.required_fields:
            raise ValueError("field must name a configured qualification field")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("value must be non-empty text")

        self.answers[str(key)] = value.strip()
        missing = [item for item in self.required_fields if item not in self.answers]
        self.events.append({
            "type": "qualification_recorded",
            "field": key,
            "complete": not missing,
        })
        return {
            "recorded": key,
            "missing_required": missing,
            "complete": not missing,
        }

    def start_handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"summary"} if self.record_as_you_go else {"summary", "qualification"}
        if set(args) != allowed:
            raise ValueError(f"handoff requires: {', '.join(sorted(allowed))}")
        snapshot = (dict(self.answers) if self.record_as_you_go else
                    QUALIFICATION_PLAN.validate_evidence(args["qualification"]))
        missing = [item for item in self.required_fields if item not in snapshot]
        if missing:
            raise ValueError(
                f"qualification is incomplete; missing: {', '.join(missing)}"
            )
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be non-empty text")
        if self.handoff_started:
            return {
                "handoff_requested": True,
                "handoff_reference": self.handoff_reference,
                "duplicate": True,
            }

        self.answers.update(snapshot)
        self.handoff_started = True
        self.events.append({
            "type": "handoff_requested",
            "summary": summary.strip(),
            "qualification": dict(self.answers),
        })
        return {
            "handoff_requested": True,
            "handoff_reference": self.handoff_reference,
        }
