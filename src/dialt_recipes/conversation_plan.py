from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlanField:
    key: str
    description: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").isalnum():
            raise ValueError("field keys must contain letters, numbers, or underscores")
        if not self.description.strip():
            raise ValueError("field descriptions cannot be empty")


@dataclass(frozen=True)
class ConversationPlan:
    """Evidence to collect naturally, with optional per-answer structured recording."""

    name: str
    objective: str
    fields: tuple[PlanField, ...]
    completion: str
    tool_name: str = "record_plan_field"
    record_as_you_go: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.objective.strip() or not self.completion.strip():
            raise ValueError("name, objective, and completion are required")
        if not isinstance(self.record_as_you_go, bool):
            raise ValueError("record_as_you_go must be a boolean")
        keys = [field.key for field in self.fields]
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("plans need at least one field and field keys must be unique")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConversationPlan":
        return cls(
            name=str(value["name"]),
            objective=str(value["objective"]),
            fields=tuple(PlanField(
                key=str(item["key"]), description=str(item["description"]),
                required=bool(item.get("required", True)),
            ) for item in value["fields"]),
            completion=str(value["completion"]),
            tool_name=str(value.get("tool_name", "record_plan_field")),
            record_as_you_go=value.get("record_as_you_go", True),
        )

    def instructions(self) -> str:
        evidence = "\n".join(
            f"- {field.key} ({'required' if field.required else 'optional'}): {field.description}"
            for field in self.fields
        )
        recording = (
            f"Whenever an answer is sufficiently supported, call {self.tool_name}. "
            "Update a field if later evidence changes it. Do not claim the interview is "
            "complete until every required field has been recorded."
            if self.record_as_you_go else
            "Keep track of supported answers and corrections in the conversation. "
            "Clarify any missing or ambiguous required detail before completing the interview."
        )
        return (
            f"You are conducting {self.name}.\n"
            f"Objective: {self.objective}\n\n"
            "Collect the evidence below through a natural conversation. Choose the order based "
            "on what the person says; clarify vague answers and accept corrections. Do not read "
            f"the field list aloud. {recording}\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Once complete: {self.completion}"
        )

    def tools(self) -> list[dict[str, Any]]:
        """Per-answer recording adds a tool/result model round to the response path."""
        return [self.tool()] if self.record_as_you_go else []

    def evidence_schema(self) -> dict[str, Any]:
        """A completion tool can collect the supported details once, at the end."""
        return {
            "type": "object",
            "properties": {item.key: {"type": "string", "description": item.description}
                           for item in self.fields},
            "required": [item.key for item in self.fields if item.required],
            "additionalProperties": False,
        }

    def validate_evidence(self, values: dict[str, Any]) -> dict[str, str]:
        """Validate shape and completeness atomically; evidence truth needs model evaluation."""
        if not isinstance(values, dict):
            raise ValueError("qualification must be an object")
        known = {item.key for item in self.fields}
        if set(values) - known:
            raise ValueError("qualification contains unknown fields")
        missing = [item.key for item in self.fields if item.required and item.key not in values]
        if missing:
            raise ValueError(f"qualification is incomplete; missing: {', '.join(missing)}")
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("qualification values must be non-empty text")
        return {key: value.strip() for key, value in values.items()}

    def tool(self) -> dict[str, Any]:
        keys = [field.key for field in self.fields]
        return {
            "name": self.tool_name,
            "description": "Record or correct one supported piece of evidence from the guided conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": keys},
                    "value": {"type": "string", "description": "Concise evidence in the user's terms."},
                },
                "required": ["field", "value"],
            },
            "read_only": False,
            "expected_duration": "instant",
            "status_label": "interview notes",
        }

    def record(self, answers: dict[str, str], args: dict[str, Any]) -> dict[str, Any]:
        key, value = args.get("field"), args.get("value")
        known = {field.key for field in self.fields}
        if key not in known or not isinstance(value, str) or not value.strip():
            raise ValueError("field must be known and value must be non-empty text")
        answers[str(key)] = value.strip()
        missing = [field.key for field in self.fields if field.required and field.key not in answers]
        return {"recorded": key, "missing_required": missing, "complete": not missing}
