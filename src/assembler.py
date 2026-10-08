"""Turn the policy assessment, the evidence ledger and (optionally) an agent's
judgment into the final ``ProcurementDecision`` - with guardrails.

Guardrails applied here, identically for every architecture:
- Approvals and risk flags from the deterministic engine can be *added to* by
  the model but never removed.
- The recommendation category must be consistent with the engine's routing; the
  model may only deviate in narrowly defined, more-cautious directions.
- Evidence citations must resolve to ledger entries; findings whose numbers are
  not present in the cited tool output are replaced with the tool's own summary.
- Material evidence the model did not cite is still shown to the reviewer.
- A next step that claims an approval or purchase is replaced.
- ``human_review_required`` is always True.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.agents.schemas import AgentRecommendation
from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.policy_engine import APPROVAL_ORDER, RISK_FLAGS, ROUTE_LABELS, PolicyAssessment
from src.tools import EvidenceLedger, LedgerEntry

_FIELD_TEXT = {
    "business_purpose": "Business purpose is too vague or contradictory to review (business_justification)",
    "annual_cost": "Annual cost needs confirmation (annual_cost_usd)",
    "user_count": "Number of users / licenses needs confirmation (user_count)",
    "data_access_level": "Intended data-access level needs clarification (data_access_level)",
    "integrations": "Required integrations need clarification (requested_integrations)",
}
_FORBIDDEN_CLAIMS = re.compile(
    r"\b(request|purchase|spend|order)\s+(has been|is|was)\s+(now\s+)?(approved|authori[sz]ed|purchased)\b"
    r"|\bauto[- ]?approv|\bproceed (with|to) (the )?purchase\b|\b(i|we) (have )?approved\b"
    r"|\b(security|privacy|legal) review (is |can be )?(waived|skipped|not (needed|required))\b",
    re.I,
)
_NUMBER = re.compile(r"\d{4}-\d{2}-\d{2}|\d[\d,]*(?:\.\d+)?")


@dataclass
class Assembly:
    decision: ProcurementDecision
    category: str
    guardrail_events: list[dict[str, str]] = field(default_factory=list)
    grounding: dict[str, Any] = field(default_factory=dict)


def _numbers(text: str) -> set[str]:
    out = set()
    for tok in _NUMBER.findall(text or ""):
        tok = tok.replace(",", "").rstrip(".")
        if re.fullmatch(r"\d+\.0+", tok):
            tok = tok.split(".")[0]
        out.add(tok)
    return out


def _approval_text(assessment: PolicyAssessment) -> str:
    return ", ".join(a.label for a in assessment.approvals) or "the approvers determined once information is complete"


def default_summary(category: str, a: PolicyAssessment) -> str:
    flags = set(a.flag_codes)
    if category == "request_more_information":
        return f"Request is incomplete ({len(a.missing_information)} required item(s) missing); it cannot be reviewed yet."
    if category == "escalate_manual_review":
        reasons = []
        if "vendor_risk_unavailable" in flags:
            reasons.append("vendor security status could not be verified")
        if "conflicting_vendor_evidence" in flags:
            reasons.append("internal registry and vendor-risk service disagree")
        if "required_evidence_missing" in flags:
            reasons.append("required evidence could not be collected")
        if "requester_unverified" in flags or "budget_unverified" in flags:
            reasons.append("requester or budget could not be verified")
        return "Material evidence is unverified or conflicting (" + ("; ".join(reasons) or "see risk flags") + ")."
    if category == "budget_exception_review":
        return f"Cost exceeds the department's available budget by ${a.budget_shortfall_usd:,.0f}; a Finance exception is needed before any approval."
    if category == "route_for_specialist_review":
        roles = [r for r in a.approval_roles if r in {"Security", "Privacy", "Legal"}]
        return f"Within budget, but {', '.join(roles)} review is required by policy before business approval."
    if category == "redirect_to_existing_tool":
        return f"An approved tool may already meet this need ({', '.join(a.overlap_candidates)}); confirm before buying."
    return "Deterministic checks found no blockers; route for the minimum business approvals."


def default_next_step(category: str, a: PolicyAssessment, request: dict[str, Any]) -> str:
    approvals = _approval_text(a)
    if category == "request_more_information":
        return (f"Procurement returns the request to the requester for: {'; '.join(a.missing_information)}. "
                "No approval workflow starts until the copilot is re-run on the completed request.")
    if category == "escalate_manual_review":
        return (f"Procurement opens a manual review; Security verifies the vendor's assessment status directly with "
                f"the vendor-risk team before routing to: {approvals}.")
    if category == "budget_exception_review":
        return (f"Finance decides on a budget exception (shortfall ${a.budget_shortfall_usd:,.0f}); if granted, route "
                f"in parallel to: {approvals}.")
    if category == "route_for_specialist_review":
        return f"Procurement opens the specialist reviews and routes the evidence pack to: {approvals}."
    if category == "redirect_to_existing_tool":
        return (f"Procurement asks the requester whether {', '.join(a.overlap_candidates) or 'the existing tool'} meets "
                f"the need (and checks unused seats) before any new purchase is routed to: {approvals}.")
    return f"Route to {approvals} for approval; Procurement places the order only after human sign-off."


def _allowed_deviation(llm_cat: str, a: PolicyAssessment, rec: AgentRecommendation) -> bool:
    primary = a.primary_route
    if llm_cat == primary:
        return True
    if llm_cat == "redirect_to_existing_tool":
        return bool(a.overlap_candidates) and primary in {"route_for_approval", "route_for_specialist_review"}
    if llm_cat == "request_more_information":
        return bool(rec.insufficient_fields)
    if llm_cat == "escalate_manual_review":
        return primary != "request_more_information"
    return False


def _entry_for(kind: str, ledger: EvidenceLedger, request: dict[str, Any]) -> LedgerEntry | None:
    vendor = request.get("vendor_name") if kind in {"registry", "vendor_risk"} else None
    return ledger.latest(kind, vendor_name=vendor)


def assemble(
    request: dict[str, Any],
    assessment: PolicyAssessment,
    ledger: EvidenceLedger,
    rec: AgentRecommendation | None,
    policy_text: str,
    llm_calls: int,
) -> Assembly:
    events: list[dict[str, str]] = []
    a = assessment

    # Approvals: engine is authoritative; the model may add, never remove.
    approvals = [x.label for x in a.approvals]
    if rec:
        for role in rec.additional_approvals:
            if role in APPROVAL_ORDER and role not in a.approval_roles:
                approvals.append(role)
                events.append({"type": "ai_added_approval", "detail": role})

    # Flags.
    flags = list(a.flag_codes)
    if rec:
        extra = list(rec.additional_risk_flags)
        if rec.prompt_injection_suspected:
            extra.append("prompt_injection_detected")
        for code in extra:
            if code in RISK_FLAGS and code not in flags:
                flags.append(code)
                events.append({"type": "ai_added_flag", "detail": code})
        if a.injection_findings and not rec.prompt_injection_suspected:
            events.append({"type": "ai_missed_injection", "detail": "Deterministic scanner found injection the model did not report"})

    # Missing information.
    missing = list(a.missing_information)
    if rec:
        for f in rec.insufficient_fields:
            text = _FIELD_TEXT[f]
            if not any(f.split("_")[0] in m.lower() for m in missing):
                missing.append(text)
                events.append({"type": "ai_added_missing_info", "detail": text})
    if missing and "missing_information" not in flags:
        flags.append("missing_information")

    # Category.
    category = a.primary_route
    if rec:
        if _allowed_deviation(rec.recommendation_category, a, rec):
            category = rec.recommendation_category
        else:
            events.append({"type": "category_overridden",
                           "detail": f"model chose {rec.recommendation_category}; policy requires {a.primary_route}"})
    if missing and category != "request_more_information":
        if rec and category == rec.recommendation_category and rec.recommendation_category != "request_more_information":
            events.append({"type": "category_overridden", "detail": f"missing information forces request_more_information (model chose {category})"})
        category = "request_more_information"
    category_from_model = bool(rec) and category == rec.recommendation_category

    # Evidence with grounding checks.
    request_numbers = _numbers(json.dumps(request, default=str)) | _numbers(policy_text)
    evidence: list[EvidenceItem] = []
    cited_ids: set[str] = set()
    grounding = {"cited": 0, "valid_citations": 0, "invalid_citations": [], "numeric_violations": 0}
    if rec:
        findings = list(rec.evidence)
        for item in findings:
            grounding["cited"] += 1
            entry = ledger.get(item.evidence_id)
            if entry is None:
                grounding["invalid_citations"].append(item.evidence_id)
                events.append({"type": "invalid_citation_dropped", "detail": f"{item.evidence_id}: {item.finding[:80]}"})
                continue
            grounding["valid_citations"] += 1
            allowed = _numbers(json.dumps(entry.output, default=str)) | _numbers(entry.summary) | request_numbers
            unsupported = _numbers(item.finding) - allowed
            finding = item.finding
            if unsupported:
                grounding["numeric_violations"] += 1
                events.append({"type": "ungrounded_number_replaced", "detail": f"{entry.evidence_id}: {sorted(unsupported)}"})
                finding = entry.summary
            if entry.evidence_id in cited_ids and finding == entry.summary:
                continue
            cited_ids.add(entry.evidence_id)
            evidence.append(EvidenceItem(source=entry.tool, finding=finding, reference=f"{entry.evidence_id} | {entry.reference}"))

    for kind in ("requester", "budget", "catalog", "registry", "vendor_risk", "policy"):
        entry = _entry_for(kind, ledger, request)
        if entry and entry.evidence_id not in cited_ids:
            evidence.append(EvidenceItem(source=entry.tool, finding=entry.summary, reference=f"{entry.evidence_id} | {entry.reference}"))
            cited_ids.add(entry.evidence_id)
    for finding in a.injection_findings:
        evidence.append(EvidenceItem(source="injection_scanner",
                                     finding=f"Ignored instruction-like text ({finding['pattern']}): \"{finding['excerpt']}\"",
                                     reference=f"{finding['source']} | policy §9"))

    # Recommendation and next step.
    summary = rec.recommendation_summary.strip() if rec and category_from_model else default_summary(category, a)
    next_step = rec.next_step.strip() if rec and category_from_model else default_next_step(category, a, request)
    if _FORBIDDEN_CLAIMS.search(next_step) or _FORBIDDEN_CLAIMS.search(summary):
        events.append({"type": "unsafe_claim_replaced", "detail": (summary + " | " + next_step)[:160]})
        summary, next_step = default_summary(category, a), default_next_step(category, a, request)
    if rec and rec.clarifying_questions and category == "request_more_information":
        next_step = f"{next_step} Questions for the requester: " + " ".join(rec.clarifying_questions[:4])

    tools_used = [e.tool for e in ledger.entries]
    decision = ProcurementDecision(
        request_id=str(request.get("request_id")),
        recommendation=f"{ROUTE_LABELS[category]}: {summary}",
        evidence=evidence,
        required_approvals=approvals,
        missing_information=missing,
        risk_flags=flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=llm_calls, tool_calls=len(tools_used), tool_names=tools_used),
    )
    return Assembly(decision=decision, category=category, guardrail_events=events, grounding=grounding)
