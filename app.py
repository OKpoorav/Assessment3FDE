"""Procurement Request Copilot - reviewer UI (Streamlit).

Run with `python run_local.py` (starts the mock vendor-risk API too).
"""
from __future__ import annotations

import json
import uuid

import pandas as pd
import streamlit as st

from src.audit import HUMAN_ACTIONS, list_decisions, record_decision
from src.config import get_settings, policy_reference_date, policy_version
from src.copilot import CopilotRun, run_copilot
from src.data_access import default_store
from src.mock_server import ensure_mock_api, is_healthy
from src.policy_engine import ROUTE_LABELS

st.set_page_config(page_title="Procurement Request Copilot", page_icon=":material/fact_check:", layout="wide")

ROUTE_STYLE = {
    "route_for_approval": ("success", ":material/check_circle:"),
    "redirect_to_existing_tool": ("info", ":material/swap_horiz:"),
    "route_for_specialist_review": ("warning", ":material/policy:"),
    "budget_exception_review": ("warning", ":material/account_balance:"),
    "escalate_manual_review": ("error", ":material/report:"),
    "request_more_information": ("error", ":material/help:"),
}
ARCH_LABELS = {
    "single": "A - Single agent",
    "staged": "B - Analyst + Reviewer",
    "rules": "Rules only (no LLM)",
}

store = default_store()
settings = get_settings()

# Sidebar ---------------------------------------------------------------------------------
with st.sidebar:
    st.header("Procurement Copilot")
    mode = st.radio("Request source", ["Existing request", "New request"], horizontal=True)
    architecture = st.radio("Architecture", list(ARCH_LABELS), format_func=ARCH_LABELS.get, index=0)

    st.divider()
    api_ok = is_healthy(settings.vendor_risk_base_url) or ensure_mock_api()
    st.caption("System status")
    st.markdown(
        f"- Vendor-risk API: {'🟢 reachable' if api_ok else '🔴 unreachable'}\n"
        f"- LLM: {'🟢 ' + settings.model if settings.llm_available else '🟠 no API key - rules-only fallback'}\n"
        f"- Policy {policy_version()} · reference date {policy_reference_date()}"
    )
    st.caption("The copilot recommends. Humans approve. Nothing here purchases, approves spend or edits budgets.")

# Request selection -------------------------------------------------------------------------
employees = {e["employee_id"]: e for e in store.employees}

if mode == "Existing request":
    by_id = {r["request_id"]: r for r in store.requests}
    request_id = st.sidebar.selectbox("Request", list(by_id),
                                      format_func=lambda rid: f"{rid} · {by_id[rid]['product_name']}")
    request = by_id[request_id]
else:
    with st.sidebar.form("new_request"):
        st.subheader("New purchase request")
        requester = st.selectbox("Requester", list(employees),
                                 format_func=lambda e: f"{e} · {employees[e]['name']} ({employees[e]['department']})")
        product = st.text_input("Product", "")
        vendor = st.text_input("Vendor", "")
        category = st.text_input("Category", "")
        cost_known = st.checkbox("Annual cost known", value=True)
        cost = st.number_input("Annual cost (USD)", min_value=0.0, step=100.0, value=0.0)
        users_known = st.checkbox("User count known", value=True)
        users = st.number_input("Users / licences", min_value=1, step=1, value=1)
        purpose = st.text_area("Business justification", "")
        data_level = st.selectbox("Data access level", [
            "unknown", "none", "internal", "internal_documents", "internal_marketing", "confidential_documents",
            "employee_pii", "customer_pii", "source_code", "credentials", "production_telemetry"])
        integrations = st.text_input("Integrations (comma-separated, blank for none)", "")
        submitted = st.form_submit_button("Use this request", type="primary")
    if submitted or "draft_request" not in st.session_state:
        st.session_state.draft_request = {
            "request_id": st.session_state.get("draft_id") or f"NEW-{uuid.uuid4().hex[:6].upper()}",
            "requester_id": requester, "product_name": product, "vendor_name": vendor, "category": category,
            "annual_cost_usd": cost if cost_known else None, "user_count": int(users) if users_known else None,
            "business_justification": purpose, "data_access_level": data_level,
            "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()],
            "urgency": "normal",
        }
        st.session_state.draft_id = st.session_state.draft_request["request_id"]
    request = st.session_state.draft_request
    request_id = request["request_id"]

# Header + request panel -----------------------------------------------------------------------
st.title("Procurement Request Copilot")
st.caption("Gathers evidence, applies deterministic policy checks, and recommends the next action for human review.")

left, right = st.columns([0.9, 1.1], gap="large")

with left:
    with st.container(border=True):
        st.subheader(f"{request.get('product_name') or '(no product)'}")
        emp = employees.get(request.get("requester_id"))
        cost_val = request.get("annual_cost_usd")
        c1, c2, c3 = st.columns(3)
        c1.metric("Annual cost", f"${cost_val:,.2f}".replace(".00", "") if cost_val is not None else "missing")
        c2.metric("Users", request.get("user_count") if request.get("user_count") is not None else "missing")
        c3.metric("Urgency", str(request.get("urgency") or "-").title())
        st.markdown(
            f"**Request** `{request_id}`  \n"
            f"**Requester** {emp['name'] + ' · ' + emp['department'] if emp else '⚠️ unknown (' + str(request.get('requester_id')) + ')'}  \n"
            f"**Vendor** {request.get('vendor_name') or '—'} · **Category** {request.get('category') or '—'}  \n"
            f"**Data access** `{request.get('data_access_level')}`  \n"
            f"**Integrations** {', '.join(request.get('requested_integrations') or []) or 'none' if request.get('requested_integrations') is not None else 'not stated'}"
        )
        st.markdown("**Business justification** · _untrusted text, shown verbatim_")
        st.info(request.get("business_justification") or "(empty)", icon=":material/format_quote:")
        with st.expander("Raw request JSON"):
            st.json(request)

    run_clicked = st.button("Run analysis", type="primary", use_container_width=True, icon=":material/play_arrow:")

state_key = f"run::{request_id}::{architecture}::{json.dumps(request, sort_keys=True, default=str)}"
if run_clicked:
    with right, st.spinner(f"Gathering evidence with {ARCH_LABELS[architecture]} ..."):
        try:
            st.session_state[state_key] = run_copilot(request_id if mode == "Existing request" else None,
                                                      architecture, request=request)
        except Exception as exc:  # noqa: BLE001 - surface to the reviewer instead of crashing the app
            st.session_state[state_key] = exc

result: CopilotRun | Exception | None = st.session_state.get(state_key)

# Recommendation panel ----------------------------------------------------------------------------
with right:
    if result is None:
        st.info("Select a request and an architecture, then **Run analysis**.", icon=":material/arrow_back:")
    elif isinstance(result, Exception):
        st.error(f"Analysis failed: {type(result).__name__}: {result}")
    else:
        d = result.decision
        kind, icon = ROUTE_STYLE.get(result.category, ("info", None))
        getattr(st, kind)(f"**{d.recommendation}**", icon=icon)
        if result.fallback_reason:
            st.warning(f"AI assist unavailable - deterministic recommendation shown. {result.fallback_reason}",
                       icon=":material/warning:")

        with st.container(border=True):
            st.markdown("**Next step**")
            st.write(d.next_step)

        m = result.metrics
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Latency", f"{m['latency_ms'] / 1000:.1f}s")
        k2.metric("LLM calls", m["llm_calls"])
        k3.metric("Tool calls", m["tool_calls"])
        k4.metric("Est. cost", f"${m['cost_usd']:.3f}" if m.get("cost_usd") else "$0")

        assessment = result.assessment
        a1, a2 = st.columns(2)
        with a1, st.container(border=True):
            st.markdown("**Approvals required**")
            reasons = {a["role"]: a for a in assessment["approvals"]}
            for label in d.required_approvals:
                role = label.split(" (")[0]
                info = reasons.get(role)
                st.markdown(f"- **{label}**" + (f" — {info['reason']} ({info['policy_ref']})" if info else " — added by AI reviewer"))
            if not d.required_approvals:
                st.caption("Tier cannot be determined until the missing information is provided.")
            if assessment.get("approval_tier"):
                st.caption(f"Financial tier: {assessment['approval_tier']}")
        with a2, st.container(border=True):
            st.markdown("**Risk flags**")
            flag_reasons = {f["code"]: f for f in assessment["flags"]}
            for code in d.risk_flags:
                info = flag_reasons.get(code)
                st.markdown(f"- `{code}`" + (f" — {info['reason']}" if info else " — raised by AI reviewer"))
            if not d.risk_flags:
                st.caption("None")

        if d.missing_information:
            with st.container(border=True):
                st.markdown("**Missing information**")
                for item in d.missing_information:
                    st.markdown(f"- {item}")

        if result.agent_output and result.agent_output.get("overlap_assessment", {}).get("existing_option"):
            ov = result.agent_output["overlap_assessment"]
            st.caption(f"Overlap judgment: **{ov['existing_option']}** · credible gap: **{ov['credible_gap']}** — {ov['rationale']}")

# Evidence + trace --------------------------------------------------------------------------------
if isinstance(result, CopilotRun):
    st.divider()
    tab_ev, tab_trace, tab_policy, tab_agent, tab_guard = st.tabs(
        ["Evidence", "Tool trace", "Policy engine", "Agent output", "Guardrails"])
    with tab_ev:
        st.dataframe(pd.DataFrame([e.model_dump() for e in result.decision.evidence]),
                     use_container_width=True, hide_index=True)
    with tab_trace:
        ledger = pd.DataFrame([{k: e[k] for k in ("evidence_id", "tool", "caller", "deterministic", "ok", "latency_ms", "summary")}
                               for e in result.ledger])
        st.dataframe(ledger, use_container_width=True, hide_index=True)
        for e in result.ledger:
            with st.expander(f"{e['evidence_id']} · {e['tool']} · {e['caller']}"):
                st.json({"input": e["input"], "output": e["output"]})
    with tab_policy:
        st.json(result.assessment)
    with tab_agent:
        if result.evidence_pack:
            st.markdown("**Stage 1 - analyst evidence pack**")
            st.json(result.evidence_pack)
        if result.agent_output:
            st.markdown("**Agent recommendation (before guardrails)**")
            st.json(result.agent_output)
        else:
            st.caption("No LLM output (rules-only run).")
        st.json({"llm_calls": result.metrics["llm_per_call"]})
    with tab_guard:
        if result.guardrail_events:
            st.dataframe(pd.DataFrame(result.guardrail_events), use_container_width=True, hide_index=True)
        else:
            st.caption("No guardrail interventions: the agent's output was consistent with policy and fully grounded.")
        st.json(result.grounding)

    # Human review ----------------------------------------------------------------------------------
    st.divider()
    st.subheader("Human review")
    st.caption("Record the reviewer's decision. This writes to a local audit log only; it does not purchase or approve spend in any system.")
    with st.form("human_review", clear_on_submit=True):
        h1, h2 = st.columns([1, 2])
        reviewer = h1.text_input("Reviewer name")
        action = h2.selectbox("Decision", HUMAN_ACTIONS)
        comment = st.text_area("Comment / justification")
        if st.form_submit_button("Record decision", type="primary"):
            try:
                record_decision(request_id=request_id, reviewer=reviewer, action=action, comment=comment,
                                recommendation=result.decision.recommendation, architecture=result.architecture_used)
                st.success("Decision recorded.")
            except ValueError as exc:
                st.error(str(exc))
    history = list_decisions(request_id)
    if history:
        st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)
