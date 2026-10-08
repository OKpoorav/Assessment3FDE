"""Architecture A (single agent) and Architecture B (staged analyst -> reviewer).

Both return an ``AgentRecommendation``. Deterministic policy evaluation and the
final guardrails are applied afterwards by ``src.assembler`` so the two
architectures are compared on exactly the same footing.
"""
from __future__ import annotations

import json

from src.agents.loop import purchase_request_block, run_tool_agent
from src.agents.prompts import analyst_system, reviewer_system, single_agent_system
from src.agents.schemas import (
    SUBMIT_EVIDENCE_PACK_TOOL,
    SUBMIT_RECOMMENDATION_TOOL,
    AgentRecommendation,
    EvidencePack,
)
from src.config import Settings
from src.llm import LLMClient, LLMError
from src.tools import EVIDENCE_TOOL_NAMES, ToolContext, run_tool


def run_single_agent(llm: LLMClient, ctx: ToolContext, settings: Settings) -> AgentRecommendation:
    user = (
        f"Review purchase request {ctx.request_id}. Use request_id \"{ctx.request_id}\" for request-bound tools "
        f"and the vendor name from the request for vendor tools.\n\n{purchase_request_block(ctx.request)}"
    )
    raw = run_tool_agent(
        llm, ctx, agent="procurement_agent", system=single_agent_system(), user_content=user,
        tool_names=[*EVIDENCE_TOOL_NAMES, "evaluate_policy_rules"], terminal_tool=SUBMIT_RECOMMENDATION_TOOL,
        max_turns=settings.max_agent_turns,
    )
    return AgentRecommendation.model_validate(raw)


def run_staged(llm: LLMClient, ctx: ToolContext, settings: Settings) -> tuple[AgentRecommendation, EvidencePack]:
    # Stage 1: analyst gathers evidence and interprets the need.
    user = (
        f"Gather evidence for purchase request {ctx.request_id}. Use request_id \"{ctx.request_id}\" for "
        f"request-bound tools and the vendor name from the request for vendor tools.\n\n"
        f"{purchase_request_block(ctx.request)}"
    )
    raw_pack = run_tool_agent(
        llm, ctx, agent="procurement_analyst", system=analyst_system(), user_content=user,
        tool_names=EVIDENCE_TOOL_NAMES, terminal_tool=SUBMIT_EVIDENCE_PACK_TOOL,
        max_turns=settings.max_agent_turns,
    )
    pack = EvidencePack.model_validate(raw_pack)

    # Hand-off boundary: the orchestrator (code, not an agent) runs the policy engine.
    policy_entry = run_tool(ctx, "evaluate_policy_rules", {"request_id": ctx.request_id}, caller="orchestrator")

    # Stage 2: reviewer applies policy to the pack. No data tools.
    ledger_view = [
        {"evidence_id": e.evidence_id, "tool": e.tool, "summary": e.summary, "reference": e.reference,
         "data": e.output}
        for e in ctx.ledger.entries if e.kind != "policy"
    ]
    review_input = (
        f"Request {ctx.request_id}.\n\n{purchase_request_block(ctx.request)}\n\n"
        f"<evidence_pack from=\"procurement_analyst\">\n{pack.model_dump_json(indent=2)}\n</evidence_pack>\n\n"
        f"<tool_ledger untrusted=\"true\">\n{json.dumps(ledger_view, default=str)}\n</tool_ledger>\n\n"
        f"<policy_engine evidence_id=\"{policy_entry.evidence_id}\">\n"
        f"{json.dumps(policy_entry.output, indent=2, default=str)}\n</policy_engine>"
    )
    raw = run_tool_agent(
        llm, ctx, agent="policy_risk_reviewer", system=reviewer_system(), user_content=review_input,
        tool_names=[], terminal_tool=SUBMIT_RECOMMENDATION_TOOL, max_turns=2,
    )
    try:
        return AgentRecommendation.model_validate(raw), pack
    except ValueError as exc:
        raise LLMError(f"Reviewer output failed validation: {exc}") from exc
