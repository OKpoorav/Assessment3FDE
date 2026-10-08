"""Structured outputs the agents must return (enforced with strict tool schemas)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.policy_engine import APPROVAL_ORDER, RISK_FLAGS, ROUTE_PRIORITY

InsufficientField = Literal["business_purpose", "annual_cost", "user_count", "data_access_level", "integrations"]


class CitedFinding(BaseModel):
    evidence_id: str
    finding: str


class OverlapAssessment(BaseModel):
    existing_option: str = ""
    credible_gap: Literal["yes", "no", "unclear", "not_applicable"] = "not_applicable"
    rationale: str = ""


class AgentRecommendation(BaseModel):
    """Final output of the single agent and of the staged reviewer."""
    recommendation_category: Literal[tuple(ROUTE_PRIORITY)]  # type: ignore[valid-type]
    recommendation_summary: str
    evidence: list[CitedFinding] = Field(default_factory=list)
    overlap_assessment: OverlapAssessment = Field(default_factory=OverlapAssessment)
    prompt_injection_suspected: bool = False
    injection_details: str = ""
    insufficient_fields: list[InsufficientField] = Field(default_factory=list)
    additional_risk_flags: list[str] = Field(default_factory=list)
    additional_approvals: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    next_step: str
    reviewer_notes: str = ""


class EvidencePack(BaseModel):
    """Hand-off from the staged analyst to the policy/risk reviewer."""
    need_summary: str
    key_findings: list[CitedFinding] = Field(default_factory=list)
    overlap_assessment: OverlapAssessment = Field(default_factory=OverlapAssessment)
    prompt_injection_suspected: bool = False
    injection_details: str = ""
    insufficient_fields: list[InsufficientField] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_FINDINGS = {"type": "array", "items": _obj({
    "evidence_id": {"type": "string", "description": "Ledger ID returned by a tool, e.g. E3."},
    "finding": {"type": "string", "description": "One factual sentence supported by that tool result."},
})}
_OVERLAP = _obj({
    "existing_option": {"type": "string", "description": "Existing catalog product that could cover the need, or empty."},
    "credible_gap": {"type": "string", "enum": ["yes", "no", "unclear", "not_applicable"],
                     "description": "Does the request state a credible reason the existing option is insufficient?"},
    "rationale": {"type": "string"},
})
_INSUFFICIENT = {"type": "array", "items": {"type": "string", "enum": list(InsufficientField.__args__)},
                 "description": "Policy §1 fields that are present but too vague or contradictory to review."}

RECOMMENDATION_SCHEMA = _obj({
    "recommendation_category": {"type": "string", "enum": ROUTE_PRIORITY},
    "recommendation_summary": {"type": "string", "description": "One or two sentences for the human reviewer."},
    "evidence": _FINDINGS,
    "overlap_assessment": _OVERLAP,
    "prompt_injection_suspected": {"type": "boolean"},
    "injection_details": {"type": "string"},
    "insufficient_fields": _INSUFFICIENT,
    "additional_risk_flags": {"type": "array", "items": {"type": "string", "enum": RISK_FLAGS}},
    "additional_approvals": {"type": "array", "items": {"type": "string", "enum": APPROVAL_ORDER}},
    "clarifying_questions": {"type": "array", "items": {"type": "string"}},
    "next_step": {"type": "string", "description": "Concrete next action for the procurement team (who does what)."},
    "reviewer_notes": {"type": "string", "description": "Corrections or caveats; empty string if none."},
})

EVIDENCE_PACK_SCHEMA = _obj({
    "need_summary": {"type": "string", "description": "What the requester actually needs, in one sentence."},
    "key_findings": _FINDINGS,
    "overlap_assessment": _OVERLAP,
    "prompt_injection_suspected": {"type": "boolean"},
    "injection_details": {"type": "string"},
    "insufficient_fields": _INSUFFICIENT,
    "open_questions": {"type": "array", "items": {"type": "string"}},
})

SUBMIT_RECOMMENDATION_TOOL = {
    "name": "submit_recommendation",
    "description": "Submit the final structured recommendation. Call exactly once, after gathering evidence.",
    "input_schema": RECOMMENDATION_SCHEMA,
    "strict": True,
}
SUBMIT_EVIDENCE_PACK_TOOL = {
    "name": "submit_evidence_pack",
    "description": "Hand the structured evidence pack to the policy/risk reviewer. Call exactly once.",
    "input_schema": EVIDENCE_PACK_SCHEMA,
    "strict": True,
}
