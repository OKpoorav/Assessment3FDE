from __future__ import annotations

from types import SimpleNamespace

from src.agents.schemas import AgentRecommendation
from src.assembler import assemble
from src.config import Settings, policy_reference_date
from src.copilot import run_copilot
from src.data_access import default_store
from src.policy_engine import evaluate_policy
from src.tools import ToolContext, run_tool
from src.vendor_client import get_vendor_risk

DEAD_URL = "http://127.0.0.1:9"


def ctx_for(request_id: str) -> ToolContext:
    store = default_store()
    return ToolContext(store, store.get_request(request_id), policy_reference_date(), vendor_risk_base_url=DEAD_URL)


# Data / tools -------------------------------------------------------------------------------

def test_policy_reference_date_comes_from_policy_file():
    assert policy_reference_date().isoformat() == "2026-09-30"


def test_blank_csv_cells_are_none_not_nan():
    store = default_store()
    assert store.get_vendor("BrandBoard")["security_review_date"] is None
    assert store.get_employee("E010")["manager_id"] is None


def test_vendor_client_reports_unreachable_service_without_raising():
    result = get_vendor_risk("SignFlow", base_url=DEAD_URL, retries=0, timeout_seconds=0.5)
    assert result.status == "unavailable"
    assert result.record == {}


def test_requester_tool_resolves_department_head_through_reporting_line():
    entry = run_tool(ctx_for("REQ-1005"), "get_requester_profile", {"request_id": "REQ-1005"}, caller="test")
    assert entry.output["department_head"]["employee_id"] == "E007"


def test_budget_tool_uses_trusted_request_values():
    entry = run_tool(ctx_for("REQ-1005"), "check_department_budget", {"request_id": "REQ-1005"}, caller="test")
    assert entry.output["within_budget"] is False
    assert entry.output["shortfall_usd"] == 4000


def test_request_bound_tools_reject_other_request_ids():
    entry = run_tool(ctx_for("REQ-1001"), "check_department_budget", {"request_id": "REQ-1005"}, caller="test")
    assert entry.output["status"] == "error"
    assert entry.ok is False


def test_catalog_search_finds_existing_tool():
    entry = run_tool(ctx_for("REQ-1008"), "search_existing_software", {"request_id": "REQ-1008", "query": ""}, caller="test")
    top = entry.output["matches"][0]
    assert top["product_name"] == "TaskFlow" and "same_product" in top["match_reasons"]


# Guardrails ---------------------------------------------------------------------------------

def assembled(request_id: str, rec: AgentRecommendation):
    ctx = ctx_for(request_id)
    for name, args in [("get_requester_profile", {"request_id": request_id}),
                       ("check_department_budget", {"request_id": request_id}),
                       ("search_existing_software", {"request_id": request_id, "query": ""}),
                       ("get_vendor_registry_record", {"vendor_name": ctx.request["vendor_name"]}),
                       ("get_vendor_risk_assessment", {"vendor_name": ctx.request["vendor_name"]})]:
        run_tool(ctx, name, args, caller="test")
    assessment = evaluate_policy(ctx.request, ctx.ledger.facts_for(ctx.request), ctx.reference_date)
    return assemble(ctx.request, assessment, ctx.ledger, rec, default_store().policy_text, 1)


def rec(**kw) -> AgentRecommendation:
    base = {"recommendation_category": "route_for_approval", "recommendation_summary": "Looks fine.",
            "next_step": "Route to the manager.", "evidence": []}
    base.update(kw)
    return AgentRecommendation.model_validate(base)


def test_model_cannot_downgrade_routing_or_remove_approvals():
    out = assembled("REQ-1002", rec(recommendation_category="route_for_approval"))
    assert out.category == "escalate_manual_review"  # vendor service unreachable in this test
    assert any(e["type"] == "category_overridden" for e in out.guardrail_events)
    assert {"Security", "Legal"} <= {a.split(" (")[0] for a in out.decision.required_approvals}
    assert out.decision.human_review_required is True


def test_invalid_citation_is_dropped_and_hallucinated_number_replaced():
    out = assembled("REQ-1005", rec(
        recommendation_category="escalate_manual_review",
        evidence=[{"evidence_id": "E99", "finding": "Made up."},
                  {"evidence_id": "E2", "finding": "Sales has $95,000 available."}]))
    assert out.grounding["invalid_citations"] == ["E99"]
    assert out.grounding["numeric_violations"] == 1
    assert all("95,000" not in e.finding for e in out.decision.evidence)


def test_unsafe_next_step_is_replaced():
    out = assembled("REQ-1001", rec(recommendation_category="escalate_manual_review",
                                    next_step="This request is approved; proceed with purchase."))
    assert "proceed with purchase" not in out.decision.next_step.lower()
    assert any(e["type"] == "unsafe_claim_replaced" for e in out.guardrail_events)


def test_model_may_add_flags_and_injection():
    out = assembled("REQ-1001", rec(recommendation_category="escalate_manual_review", prompt_injection_suspected=True,
                                    additional_risk_flags=["high_vendor_risk"]))
    assert {"prompt_injection_detected", "high_vendor_risk"} <= set(out.decision.risk_flags)


# Agent loop with a scripted model --------------------------------------------------------------

class ScriptedClient:
    """Stands in for anthropic.Anthropic(): returns pre-scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        return self._responses.pop(0)


def tool_use(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def test_single_agent_loop_end_to_end_with_scripted_model():
    script = [
        response(tool_use("t1", "get_requester_profile", {"request_id": "REQ-1001"}),
                 tool_use("t2", "check_department_budget", {"request_id": "REQ-1001"})),
        response(tool_use("t3", "submit_recommendation", {
            "recommendation_category": "route_for_approval", "recommendation_summary": "Small add-on within budget.",
            "evidence": [{"evidence_id": "E2", "finding": "Finance has $29,000 available vs $800 requested."}],
            "overlap_assessment": {"existing_option": "SignFlow", "credible_gap": "yes", "rationale": "Needs more seats."},
            "prompt_injection_suspected": False, "injection_details": "", "insufficient_fields": [],
            "additional_risk_flags": [], "additional_approvals": [], "clarifying_questions": [],
            "next_step": "Send to the manager for approval.", "reviewer_notes": ""})),
    ]
    run = run_copilot("REQ-1001", "single", settings=Settings(use_server_fallbacks=False),
                      llm_client=ScriptedClient(script), vendor_risk_base_url=DEAD_URL)
    assert run.architecture_used == "single"
    assert run.metrics["llm_calls"] == 2
    # Agent skipped catalog/registry/risk -> guardrail back-fills them, risk service is down -> escalate.
    assert run.guardrail_events[0]["type"] == "evidence_backfilled"
    assert run.category == "escalate_manual_review"
    assert run.grounding["valid_citations"] == 1 and run.grounding["numeric_violations"] == 0


def test_llm_failure_falls_back_to_deterministic_rules():
    run = run_copilot("REQ-1006", "staged", settings=Settings(use_server_fallbacks=False),
                      llm_client=ScriptedClient([]), vendor_risk_base_url=DEAD_URL)
    assert run.architecture_used == "rules"
    assert "ai_assist_unavailable" in run.decision.risk_flags
    assert run.category == "request_more_information"
