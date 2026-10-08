"""Deterministic procurement policy engine (policy version 2026.09).

This module owns every rule that can be expressed as code: required fields,
budget arithmetic, approval thresholds, Security / Privacy / Legal triggers,
vendor-assessment freshness, cross-source conflicts and prompt-injection
patterns. It never calls an LLM or the network; it only reads the outputs of the
evidence tools. Unchecked or failed evidence is treated as *unverified*, never
as favourable.

Section numbers in ``policy_ref`` refer to ``data/procurement_policy.md``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from src.data_access import norm

REVIEW_VALIDITY_DAYS = 365

APPROVAL_ORDER = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]
SPECIALIST_ROLES = {"Security", "Privacy", "Legal"}

# Recommendation categories, most to least blocking.
ROUTE_PRIORITY = [
    "request_more_information",
    "escalate_manual_review",
    "budget_exception_review",
    "route_for_specialist_review",
    "redirect_to_existing_tool",
    "route_for_approval",
]
ROUTE_LABELS = {
    "request_more_information": "Request more information",
    "escalate_manual_review": "Escalate for manual review",
    "budget_exception_review": "Finance budget-exception review",
    "route_for_specialist_review": "Route for Security / Privacy / Legal review",
    "redirect_to_existing_tool": "Review existing tool before buying",
    "route_for_approval": "Route for business approval",
}

RISK_FLAGS = [
    "missing_information",
    "budget_insufficient",
    "budget_unverified",
    "existing_tool_overlap",
    "security_review_required",
    "privacy_review_required",
    "legal_review_required",
    "vendor_review_expired",
    "conflicting_vendor_evidence",
    "vendor_risk_unavailable",
    "new_vendor",
    "high_vendor_risk",
    "data_classification_mismatch",
    "restricted_use_product",
    "prompt_injection_detected",
    "requester_unverified",
    "required_evidence_missing",
]

# Data-access levels ---------------------------------------------------------------

_SECURITY_DATA = {
    "source_code": "source code access",
    "confidential_documents": "confidential documents",
    "employee_pii": "employee PII",
    "customer_pii": "customer PII",
    "credentials": "credentials / secrets",
    "secrets": "credentials / secrets",
}
_PII_DATA = {"employee_pii", "customer_pii"}
_LOW_RISK_DATA = {"none", "public", "internal", "internal_documents", "internal_marketing", "internal_only"}
_UNKNOWN_DATA = {"", "unknown", "tbd", "n/a", "not sure", "unspecified"}

# Integrations that imply a sensitive data class or production access.
_INTEGRATION_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bproduction\b|\bprod\b|cloud account|\baws\b|\bgcp\b|\bazure\b|kubernetes", re.I),
     "production", "production or cloud-account integration"),
    (re.compile(r"\bgit\b|github|gitlab|bitbucket|source|code repo", re.I), "source_code", "source code access"),
    (re.compile(r"secret|credential|vault|password|\biam\b", re.I), "credentials", "credentials / secrets"),
    (re.compile(r"\bcrm\b|salesforce|hubspot|helpdesk|zendesk|ticket", re.I), "customer_pii", "customer PII"),
    (re.compile(r"\bhris\b|workday|payroll|bamboohr|\bhr\b", re.I), "employee_pii", "employee PII"),
]

# Prompt-injection patterns ----------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction override", re.compile(r"\b(ignore|disregard|forget|bypass|override|skip)\b[^.]{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|reviews?|checks?|guidelines?|prompt)\b", re.I)),
    ("fabricated approval", re.compile(r"\b(cfo|ceo|vp|finance|security|legal|privacy|procurement|manager)[- ]?(approved|signed[- ]off|pre[- ]?cleared|cleared)\b", re.I)),
    ("fabricated approval", re.compile(r"\b(already|pre)[- ]?(approved|cleared|authori[sz]ed)\b", re.I)),
    ("approval demand", re.compile(r"\bapprove (it|this|the request|immediately|now)\b|\bauto[- ]?approve\b|\bmark (it|this|security|the request)\b[^.]{0,30}\b(approved|not required)", re.I)),
    ("role manipulation", re.compile(r"\b(you are now|act as|system prompt|developer message|new instructions|note to (the )?(ai|assistant|copilot|reviewer|model))\b", re.I)),
    ("control suppression", re.compile(r"\bdo not (flag|escalate|route|review|mention|report)\b|\bno (security|privacy|legal) review (is )?(needed|required)\b", re.I)),
    ("secret exfiltration", re.compile(r"\b(reveal|print|show|expose|send)\b[^.]{0,40}\b(api key|secret|password|token|credentials?)\b", re.I)),
]


def scan_for_injection(texts: dict[str, str | None]) -> list[dict[str, str]]:
    """Return injection findings for each free-text field (deterministic heuristics)."""
    findings: list[dict[str, str]] = []
    for source, text in texts.items():
        if not text:
            continue
        for label, pattern in _INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append({"source": source, "pattern": label, "excerpt": match.group(0)[:120]})
    # One finding per (source, excerpt) is enough.
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for f in findings:
        unique.setdefault((f["source"], f["excerpt"].lower()), f)
    return list(unique.values())


# Output types -----------------------------------------------------------------------

@dataclass
class Approval:
    role: str
    reason: str
    policy_ref: str
    approver: str | None = None

    @property
    def label(self) -> str:
        return f"{self.role} ({self.approver})" if self.approver else self.role


@dataclass
class Flag:
    code: str
    reason: str
    policy_ref: str


@dataclass
class PolicyAssessment:
    request_id: str
    approvals: list[Approval] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    triggered_routes: list[str] = field(default_factory=list)
    primary_route: str = "route_for_approval"
    approval_tier: str = ""
    budget_shortfall_usd: float | None = None
    injection_findings: list[dict[str, str]] = field(default_factory=list)
    overlap_candidates: list[str] = field(default_factory=list)
    vendor_assessment: dict[str, Any] = field(default_factory=dict)
    unchecked_evidence: list[str] = field(default_factory=list)

    @property
    def approval_roles(self) -> list[str]:
        return [a.role for a in self.approvals]

    @property
    def flag_codes(self) -> list[str]:
        return [f.code for f in self.flags]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["approval_labels"] = [a.label for a in self.approvals]
        return out


class _Builder:
    def __init__(self, request_id: str) -> None:
        self.a = PolicyAssessment(request_id=request_id)

    def approve(self, role: str, reason: str, ref: str, approver: str | None = None) -> None:
        for existing in self.a.approvals:
            if existing.role == role:
                if reason not in existing.reason:
                    existing.reason = f"{existing.reason}; {reason}"
                return
        self.a.approvals.append(Approval(role, reason, ref, approver))

    def flag(self, code: str, reason: str, ref: str) -> None:
        for existing in self.a.flags:
            if existing.code == code:
                if reason not in existing.reason:
                    existing.reason = f"{existing.reason}; {reason}"
                return
        self.a.flags.append(Flag(code, reason, ref))

    def missing(self, item: str) -> None:
        if item not in self.a.missing_information:
            self.a.missing_information.append(item)


# Helpers ------------------------------------------------------------------------------

def financial_tier(amount: float) -> tuple[str, list[str]]:
    """Policy section 4 approval thresholds for an annualised amount."""
    if amount <= 1000:
        return "Up to $1,000", ["Manager"]
    if amount <= 10000:
        return "$1,000.01 - $10,000", ["Department Head", "Procurement"]
    if amount <= 25000:
        return "$10,000.01 - $25,000", ["Department Head", "Finance", "Procurement"]
    return "Above $25,000", ["Department Head", "Finance", "CFO", "Procurement"]


def classify_data_level(level: str | None) -> tuple[str, str | None]:
    """Return (class, description) where class is unknown | low | sensitive | production | unrecognised."""
    value = norm(level).replace(" ", "_").replace("-", "_")
    if value in _UNKNOWN_DATA:
        return "unknown", None
    if value in _SECURITY_DATA:
        return "sensitive", _SECURITY_DATA[value]
    if "production" in value or value.startswith("prod_"):
        return "production", "production access / production data"
    if any(k in value for k in ("pii", "personal", "confidential", "secret", "credential", "source", "health", "financial")):
        return "sensitive", value.replace("_", " ")
    if value in _LOW_RISK_DATA or value.startswith("internal"):
        return "low", None
    return "unrecognised", value


def integration_implications(integrations: list[str] | None) -> list[tuple[str, str, str]]:
    """Return (integration, implied_class, description) for integrations matching a sensitive rule."""
    found: list[tuple[str, str, str]] = []
    for integration in integrations or []:
        for pattern, implied, desc in _INTEGRATION_RULES:
            if pattern.search(str(integration)):
                found.append((str(integration), implied, desc))
                break
    return found


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm_status(value: Any) -> str | None:
    v = norm(str(value)) if value else ""
    if not v or v in {"unknown", "none"}:
        return None
    if v in {"approved", "current", "completed", "passed"}:
        return "approved"
    if v in {"expired", "lapsed"}:
        return "expired"
    if v in {"pending", "not_completed", "not completed", "in progress", "in_progress", "not started"}:
        return "not_completed"
    if v in {"rejected", "failed"}:
        return "rejected"
    return v


def _money(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}".replace(".00", "")


# Main entry point ----------------------------------------------------------------------

def evaluate_policy(request: dict[str, Any], facts: dict[str, Any], reference_date: date) -> PolicyAssessment:
    """Apply the deterministic policy to a request and the evidence gathered so far.

    ``facts`` maps evidence kinds (requester, budget, catalog, registry, vendor_risk)
    to the corresponding tool outputs. Missing kinds are treated as unchecked.
    """
    b = _Builder(str(request.get("request_id") or "UNKNOWN"))
    a = b.a

    requester = facts.get("requester")
    budget = facts.get("budget")
    catalog = facts.get("catalog")
    registry = facts.get("registry")
    risk = facts.get("vendor_risk")

    for kind in ("requester", "budget", "catalog", "registry", "vendor_risk"):
        if facts.get(kind) is None:
            a.unchecked_evidence.append(kind)
    if a.unchecked_evidence:
        b.flag("required_evidence_missing", f"Evidence not collected: {', '.join(a.unchecked_evidence)}", "§10")

    # §1 Required information ----------------------------------------------------------
    cost = request.get("annual_cost_usd")
    users = request.get("user_count")
    data_level = request.get("data_access_level")
    integrations = request.get("requested_integrations")
    data_class, data_desc = classify_data_level(data_level)

    if requester is not None and requester.get("status") != "found":
        b.missing("Requester and department (requester not found in employee directory)")
        b.flag("requester_unverified", f"Requester '{request.get('requester_id')}' not found", "§1")
    elif not request.get("requester_id"):
        b.missing("Requester and department")
    if not (request.get("product_name") or "").strip() or not (request.get("vendor_name") or "").strip():
        b.missing("Product and vendor name")
    if cost is None or (isinstance(cost, (int, float)) and cost < 0):
        b.missing("Annual cost or a reasonable annual estimate (annual_cost_usd)")
    if users is None or (isinstance(users, (int, float)) and users <= 0):
        b.missing("Number of users / licenses (user_count)")
    if not (request.get("business_justification") or "").strip():
        b.missing("Business purpose (business_justification)")
    if data_class == "unknown":
        b.missing("Intended data-access level (data_access_level)")
    if integrations is None:
        b.missing("Required integrations (requested_integrations; use an empty list if none)")
    if a.missing_information:
        b.flag("missing_information", f"{len(a.missing_information)} required field(s) missing", "§1")

    # §9 Untrusted content ----------------------------------------------------------------
    texts: dict[str, str | None] = {
        "request.business_justification": request.get("business_justification"),
        "request.product_name": request.get("product_name"),
        "request.vendor_name": request.get("vendor_name"),
        "request.category": request.get("category"),
    }
    for i, integ in enumerate(integrations or []):
        texts[f"request.requested_integrations[{i}]"] = str(integ)
    if registry and registry.get("status") == "found":
        texts["vendor_registry.notes"] = registry.get("notes")
    if risk and risk.get("status") == "ok":
        texts["vendor_risk.notes"] = (risk.get("record") or {}).get("notes")
    a.injection_findings = scan_for_injection(texts)
    if a.injection_findings:
        sources = sorted({f["source"] for f in a.injection_findings})
        b.flag("prompt_injection_detected",
               f"Instruction-like text in untrusted data ({', '.join(sources)}) was ignored", "§9")

    # §4 Financial approvals ----------------------------------------------------------------
    manager_name = dept_head_name = None
    if requester and requester.get("status") == "found":
        mgr = requester.get("manager") or {}
        head = requester.get("department_head") or {}
        manager_name = f"{mgr['name']}, {mgr['employee_id']}" if mgr.get("name") else None
        dept_head_name = f"{head['name']}, {head['employee_id']}" if head.get("name") else None

    if isinstance(cost, (int, float)) and cost >= 0:
        tier, roles = financial_tier(float(cost))
        a.approval_tier = tier
        for role in roles:
            approver = manager_name if role == "Manager" else dept_head_name if role == "Department Head" else None
            b.approve(role, f"Annual amount {_money(cost)} falls in tier '{tier}'", "§4", approver)
    else:
        a.approval_tier = "Undetermined - annual cost missing"
        a.notes.append("Financial approval tier cannot be determined until the annual cost is provided (§4).")

    # §2 Budget ---------------------------------------------------------------------------------
    if budget is not None:
        status = budget.get("status")
        if status == "ok" and budget.get("within_budget") is False:
            a.budget_shortfall_usd = budget.get("shortfall_usd")
            b.flag("budget_insufficient",
                   f"{_money(cost)} exceeds {budget.get('department')} available budget "
                   f"{_money(budget.get('available_usd'))} (shortfall {_money(budget.get('shortfall_usd'))})", "§2")
            b.approve("Finance", "Budget exception review required (cost exceeds available budget)", "§2")
        elif status == "department_not_found":
            b.flag("budget_unverified", f"No budget record for department '{budget.get('department')}'", "§2")
        elif status == "requester_unknown":
            b.flag("budget_unverified", "Budget could not be checked because the requester is unknown", "§2")
        elif status == "cost_missing":
            a.notes.append("Budget check deferred: annual cost missing.")

    # §3 Overlap ---------------------------------------------------------------------------------
    if catalog is not None:
        strong = [m for m in catalog.get("matches", []) if set(m.get("match_reasons", [])) & {"same_product", "same_vendor", "same_category"}]
        a.overlap_candidates = [f"{m['product_name']} ({m['software_id']})" for m in strong]
        if strong:
            desc = "; ".join(
                f"{m['product_name']} ({m['software_id']}, {m.get('status')}, {m.get('licensed_seats')} seats, "
                f"scope {m.get('scope')}; {', '.join(m['match_reasons'])})" for m in strong)
            b.flag("existing_tool_overlap", f"Existing catalog software: {desc}", "§3")
        restricted = [m for m in strong if "limited" in norm(m.get("status")) and "same_vendor" in m.get("match_reasons", [])]
        if restricted and data_class in {"sensitive", "production", "unknown", "unrecognised"}:
            names = ", ".join(m["product_name"] for m in restricted)
            b.flag("restricted_use_product",
                   f"{names} is approved for limited use only; prior approval does not cover this data class", "§8")
            if data_class != "unknown":
                b.approve("Security", f"Limited-use approval of {names} does not extend to {data_desc}", "§8")
                b.approve("Privacy", f"Limited-use approval of {names} does not extend to {data_desc}", "§8")

    # §5/§6 Data-driven Security & Privacy triggers ---------------------------------------
    effective_pii = data_level in _PII_DATA or norm(data_level).replace(" ", "_") in _PII_DATA
    sensitive = data_class in {"sensitive", "production"}
    if data_class in {"sensitive", "production"}:
        b.approve("Security", f"Data access involves {data_desc}", "§5")
        b.flag("security_review_required", f"Data access involves {data_desc}", "§5")
    elif data_class == "unrecognised":
        b.approve("Security", f"Unrecognised data-access level '{data_level}' must be classified", "§5")
        b.flag("security_review_required", f"Unrecognised data-access level '{data_level}'", "§5")

    implications = integration_implications(integrations)
    for integ, implied, desc in implications:
        b.approve("Security", f"Integration '{integ}' implies {desc}", "§5")
        b.flag("security_review_required", f"Integration '{integ}' implies {desc}", "§5")
        sensitive = True
        if implied in _PII_DATA:
            effective_pii = True
            stated_pii = norm(data_level).replace(" ", "_") in _PII_DATA
            if not stated_pii and data_class != "unknown":
                b.flag("data_classification_mismatch",
                       f"Integration '{integ}' implies {desc} but stated data-access level is '{data_level}'", "§5/§6")

    if effective_pii:
        b.approve("Privacy", "Tool will process personal data (PII)", "§6")
        b.flag("privacy_review_required", "Tool will process personal data (PII)", "§6")

    # §5 Vendor assessment freshness & §10 tool failures -----------------------------------
    registry_found = registry is not None and registry.get("status") == "found"
    va: dict[str, Any] = {"registry": None, "risk_service": None}

    reg_status = reg_date = None
    if registry_found:
        reg_status = _norm_status(registry.get("security_status"))
        reg_date = _parse_date(registry.get("security_review_date"))
        va["registry"] = {"status": reg_status, "review_date": str(reg_date) if reg_date else None}

    risk_status = risk_date = None
    risk_record: dict[str, Any] = {}
    if risk is None:
        pass
    elif risk.get("status") == "ok":
        risk_record = risk.get("record") or {}
        risk_status = _norm_status(risk_record.get("security_review_status"))
        risk_date = _parse_date(risk_record.get("last_review_date"))
        va["risk_service"] = {"status": risk_status, "review_date": str(risk_date) if risk_date else None,
                              "risk_level": risk_record.get("risk_level")}
    elif risk.get("status") == "unavailable":
        b.flag("vendor_risk_unavailable", f"Vendor-risk service unavailable: {risk.get('error')}", "§10")
        b.approve("Security", "Vendor security status could not be verified (service unavailable)", "§5/§10")
        b.flag("security_review_required", "Vendor security status could not be verified", "§5/§10")
    elif risk.get("status") == "not_found":
        va["risk_service"] = {"status": "no_record"}

    def expired(d: date | None) -> bool:
        return d is not None and reference_date > d + timedelta(days=REVIEW_VALIDITY_DAYS)

    # Conflict detection between the two sources.
    if registry_found and risk_status is not None:
        conflicts = []
        if reg_status and risk_status and reg_status != risk_status:
            conflicts.append(f"status registry='{registry.get('security_status')}' vs risk service='{risk_record.get('security_review_status')}'")
        if reg_date and risk_date and reg_date != risk_date:
            conflicts.append(f"review date registry={reg_date} vs risk service={risk_date}")
        if conflicts:
            b.flag("conflicting_vendor_evidence", "; ".join(conflicts), "§5")
            b.approve("Security", "Internal registry and vendor-risk service disagree", "§5")

    dates = [d for d in (reg_date, risk_date) if d]
    latest = max(dates) if dates else None
    current_approved = (
        risk is not None  # the external service must at least have been consulted
        and latest is not None
        and not expired(latest)
        and "approved" in {reg_status, risk_status}
        and "expired" not in {reg_status, risk_status}
        and "not_completed" not in {risk_status}
    )
    va["latest_review_date"] = str(latest) if latest else None
    va["valid_until"] = str(latest + timedelta(days=REVIEW_VALIDITY_DAYS)) if latest else None
    va["current"] = current_approved

    if any(expired(d) for d in dates) or "expired" in {reg_status, risk_status}:
        b.flag("vendor_review_expired",
               f"Security assessment dated {latest} is older than {REVIEW_VALIDITY_DAYS} days at {reference_date}"
               if latest and expired(latest) else "Vendor-risk service reports the assessment as expired", "§5")
    if not current_approved and not (risk and risk.get("status") == "unavailable"):
        if risk is None:
            reason = "Vendor security assessment not verified with the vendor-risk service"
        elif not dates:
            reason = "No completed vendor security assessment on record"
        elif any(expired(d) for d in dates) or "expired" in {reg_status, risk_status}:
            reason = "Vendor security assessment expired"
        else:
            reason = "Vendor security assessment not completed / not approved"
        b.approve("Security", reason, "§5")
        b.flag("security_review_required", reason, "§5")

    if risk_record.get("risk_level") == "high":
        b.flag("high_vendor_risk", "Vendor-risk service rates this vendor HIGH risk", "§5")

    # §6/§7 Cross-region ---------------------------------------------------------------------
    outside_region = bool(risk_record.get("stores_data_outside_region"))
    if outside_region and (effective_pii or data_class == "sensitive"):
        b.approve("Privacy", "Sensitive data may be stored outside the operating region", "§6")
        b.flag("privacy_review_required", "Sensitive data may be stored outside the operating region", "§6")
        b.approve("Legal", "Cross-region processing of sensitive data", "§7")
        b.flag("legal_review_required", "Cross-region processing of sensitive data", "§7")
    elif outside_region and data_class == "unknown":
        a.notes.append("Vendor stores data outside the region; Privacy/Legal review may apply once the data-access level is known.")

    # §7 Legal ------------------------------------------------------------------------------------
    is_new_vendor = registry is not None and (not registry_found or norm(registry.get("procurement_status")) == "new")
    if is_new_vendor:
        b.flag("new_vendor", "Vendor is not yet an approved supplier in the internal registry", "§7")
        if isinstance(cost, (int, float)) and cost >= 10000:
            b.approve("Legal", f"New vendor with annual spend {_money(cost)} (>= $10,000)", "§7")
            b.flag("legal_review_required", f"New vendor with annual spend {_money(cost)}", "§7")
    if registry is not None:
        terms = registry.get("legal_terms_status") if registry_found else None
        if norm(terms) not in {"approved", "standard"}:
            b.approve("Legal", f"Legal terms not approved/standard (status: {terms or 'no contract on file'})", "§7")
            b.flag("legal_review_required", f"Legal terms status: {terms or 'no contract on file'}", "§7")

    # Routing ---------------------------------------------------------------------------------------
    codes = set(a.flag_codes)
    routes: list[str] = []
    if a.missing_information:
        routes.append("request_more_information")
    if codes & {"vendor_risk_unavailable", "conflicting_vendor_evidence", "required_evidence_missing", "requester_unverified", "budget_unverified"}:
        routes.append("escalate_manual_review")
    if "budget_insufficient" in codes:
        routes.append("budget_exception_review")
    if SPECIALIST_ROLES & set(a.approval_roles):
        routes.append("route_for_specialist_review")
    if not routes:
        routes.append("route_for_approval")
    a.triggered_routes = routes
    a.primary_route = min(routes, key=ROUTE_PRIORITY.index)
    a.approvals.sort(key=lambda x: APPROVAL_ORDER.index(x.role))
    a.vendor_assessment = va
    return a
