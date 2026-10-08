"""Orchestration entry point: request -> evidence -> policy -> agent judgment -> guarded decision."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from src.agents.architectures import run_single_agent, run_staged
from src.agents.schemas import AgentRecommendation, EvidencePack
from src.assembler import assemble
from src.config import Settings, get_settings, policy_reference_date
from src.contracts import ProcurementDecision
from src.data_access import DataStore, Row, default_store, norm
from src.llm import LLMClient, LLMError
from src.mock_server import ensure_mock_api
from src.policy_engine import PolicyAssessment, evaluate_policy
from src.tools import EVIDENCE_TOOL_NAMES, ToolContext, run_tool

ArchitectureName = Literal["single", "staged", "rules"]


@dataclass
class CopilotRun:
    decision: ProcurementDecision
    category: str
    architecture_requested: str
    architecture_used: str
    fallback_reason: str | None
    assessment: dict[str, Any]
    agent_output: dict[str, Any] | None
    evidence_pack: dict[str, Any] | None
    ledger: list[dict[str, Any]]
    guardrail_events: list[dict[str, str]]
    grounding: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["decision"] = self.decision.model_dump()
        return out


def _evidence_args(name: str, ctx: ToolContext) -> dict[str, Any]:
    if name in {"get_vendor_registry_record", "get_vendor_risk_assessment"}:
        return {"vendor_name": str(ctx.request.get("vendor_name") or "")}
    if name == "search_existing_software":
        return {"request_id": ctx.request_id, "query": ""}
    return {"request_id": ctx.request_id}


_KIND_BY_TOOL = {"get_requester_profile": "requester", "check_department_budget": "budget",
                 "search_existing_software": "catalog", "get_vendor_registry_record": "registry",
                 "get_vendor_risk_assessment": "vendor_risk"}


def _complete_evidence(ctx: ToolContext, caller: str) -> list[str]:
    """Run any required evidence tool the agent skipped. Returns the tools that were backfilled."""
    vendor = norm(ctx.request.get("vendor_name"))
    backfilled = []
    for name in EVIDENCE_TOOL_NAMES:
        kind = _KIND_BY_TOOL[name]
        # Vendor tools only count when called for the request's own vendor, not some other name.
        attempted = any(
            e.kind == kind and (kind not in {"registry", "vendor_risk"} or norm(e.input.get("vendor_name")) == vendor)
            for e in ctx.ledger.entries
        )
        if not attempted:
            run_tool(ctx, name, _evidence_args(name, ctx), caller=caller)
            backfilled.append(name)
    return backfilled


def _final_assessment(ctx: ToolContext) -> PolicyAssessment:
    """Re-run the policy engine on the final ledger, recording it if the last run is stale."""
    entries = ctx.ledger.entries
    last_policy = max((i for i, e in enumerate(entries) if e.kind == "policy"), default=-1)
    stale = any(e.kind != "policy" for e in entries[last_policy + 1:]) or last_policy < 0
    if stale:
        run_tool(ctx, "evaluate_policy_rules", {"request_id": ctx.request_id}, caller="orchestrator")
    return evaluate_policy(ctx.request, ctx.ledger.facts_for(ctx.request), ctx.reference_date)


def run_copilot(
    request_id: str | None = None,
    architecture: ArchitectureName = "single",
    *,
    request: Row | None = None,
    store: DataStore | None = None,
    vendor_risk_base_url: str | None = None,
    settings: Settings | None = None,
    llm_client: Any | None = None,
) -> CopilotRun:
    settings = settings or get_settings()
    store = store or default_store()
    if request is None:
        if request_id is None:
            raise ValueError("Provide request_id or request")
        request = store.get_request(request_id)
    if architecture not in ("single", "staged", "rules"):
        raise ValueError(f"Unknown architecture '{architecture}'")

    base_url = vendor_risk_base_url or settings.vendor_risk_base_url
    ensure_mock_api(base_url)

    start = time.perf_counter()
    ctx = ToolContext(store=store, request=request, reference_date=policy_reference_date(),
                      vendor_risk_base_url=base_url)
    rec: AgentRecommendation | None = None
    pack: EvidencePack | None = None
    used: str = architecture
    fallback_reason: str | None = None
    llm: LLMClient | None = None

    if architecture != "rules":
        if not settings.llm_available and llm_client is None:
            used, fallback_reason = "rules", "No LLM credentials configured (ANTHROPIC_API_KEY)"
        else:
            try:
                llm = LLMClient(settings, client=llm_client)
                if architecture == "single":
                    rec = run_single_agent(llm, ctx, settings)
                else:
                    rec, pack = run_staged(llm, ctx, settings)
            except (LLMError, ValidationError) as exc:
                used, fallback_reason = "rules", f"AI step failed, deterministic fallback used: {exc}"[:300]
                rec = None

    caller = "orchestrator" if used == "rules" else "guardrail"
    backfilled = _complete_evidence(ctx, caller=caller)
    assessment = _final_assessment(ctx)
    result = assemble(request, assessment, ctx.ledger, rec, store.policy_text, llm.usage.calls if llm else 0)

    events = list(result.guardrail_events)
    if used != "rules" and backfilled:
        events.insert(0, {"type": "evidence_backfilled", "detail": ", ".join(backfilled)})
    if fallback_reason:
        events.insert(0, {"type": "llm_fallback", "detail": fallback_reason})
        if architecture != "rules":
            result.decision.risk_flags.append("ai_assist_unavailable")

    latency_ms = (time.perf_counter() - start) * 1000
    usage = llm.usage if llm else None
    metrics = {
        "latency_ms": round(latency_ms, 1),
        "llm_calls": usage.calls if usage else 0,
        "llm_latency_ms": round(usage.latency_ms, 1) if usage else 0.0,
        "input_tokens": (usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens) if usage else 0,
        "cache_read_tokens": usage.cache_read_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "cost_usd": usage.cost_usd(settings.model) if usage else 0.0,
        "tool_calls": len(ctx.ledger.entries),
        "agent_tool_calls": sum(1 for e in ctx.ledger.entries if e.caller not in {"guardrail", "orchestrator"}),
        "model": settings.model if usage else None,
        "llm_per_call": usage.per_call if usage else [],
    }
    return CopilotRun(
        decision=result.decision,
        category=result.category,
        architecture_requested=architecture,
        architecture_used=used,
        fallback_reason=fallback_reason,
        assessment=assessment.to_dict(),
        agent_output=rec.model_dump() if rec else None,
        evidence_pack=pack.model_dump() if pack else None,
        ledger=[asdict(e) for e in ctx.ledger.entries],
        guardrail_events=events,
        grounding=result.grounding,
        metrics=metrics,
    )
