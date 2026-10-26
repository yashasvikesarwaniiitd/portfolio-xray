"""Tool registry: the domain tools a ToolPort provides, plus the control tools the loop
itself implements.

Control tools are how the model drives its own process — stating a plan, proposing a
finding, declaring it is done. Modelling them as tools rather than as parsed prose means
the model's intentions arrive as structured data that the loop can act on and the verifier
can reject, instead of text someone has to regex.
"""
from __future__ import annotations

UPDATE_PLAN = {
    "name": "update_plan",
    "description":
        "State or revise your investigation plan. Call this first, and again whenever the "
        "evidence makes your current plan wrong — revising a plan is expected, not a failure.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 6,
                "description": "2-5 short steps, each one concrete thing you will find out",
            },
            "note": {"type": "string",
                     "description": "optional: why you are revising, if this is a revision"},
        },
        "required": ["steps"],
    },
}

RECORD_FINDING = {
    "name": "record_finding",
    "description":
        "Record one conclusion, with the evidence IDs its numbers came from. The finding is "
        "checked in code: every number you state must appear in the evidence you cite, and "
        "the claim must not recommend an action. If it fails, you get the exact reason and "
        "can record a corrected version.",
    "input_schema": {
        "type": "object",
        "properties": {
            "claim": {"type": "string",
                      "description": "one plain-English sentence stating what the numbers show"},
            "evidence_ids": {
                "type": "array", "items": {"type": "string"}, "minItems": 1,
                "description": "evidence IDs backing this claim, e.g. [\"E1\", \"E3\"]",
            },
        },
        "required": ["claim", "evidence_ids"],
    },
}

FINISH = {
    "name": "finish",
    "description":
        "End the investigation. Call this when the move is explained, or when the tools "
        "cannot tell you more. The summary must rest only on findings that were accepted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "plain-English explanation built from accepted findings"},
            "open_questions": {
                "type": "array", "items": {"type": "string"},
                "description": "questions the reader should ask themselves — never advice",
            },
            "unresolved": {
                "type": "array", "items": {"type": "string"},
                "description": "what you could NOT determine, and why (unpriced holdings, "
                               "missing data, unexplained residual)",
            },
        },
        "required": ["summary"],
    },
}

CONTROL_SCHEMAS = [UPDATE_PLAN, RECORD_FINDING, FINISH]
CONTROL_NAMES = frozenset(s["name"] for s in CONTROL_SCHEMAS)


class ToolRegistry:
    """Presents one tool list to the model and tells the loop which half a call belongs to."""

    def __init__(self, domain_tools):
        self.domain = domain_tools

    def schemas(self) -> list[dict]:
        return list(self.domain.schemas()) + CONTROL_SCHEMAS

    @staticmethod
    def is_control(name: str) -> bool:
        return name in CONTROL_NAMES

    def execute(self, name: str, args: dict):
        """Domain tools only — control tools are handled inside the loop, which owns the
        state they mutate."""
        return self.domain.execute(name, args)
