"""Run the shared test set against both architectures (plus the no-LLM rules
baseline) and write reproducible results.

    python evals/run_comparison.py                       # single + staged + rules, 3 repeats
    python evals/run_comparison.py --architectures rules # free, deterministic smoke run
    python evals/run_comparison.py --cases PUB-01 SYN-11 --repeats 1

Outputs (in evals/results/):
    runs.jsonl          full trace per run (decision, ledger, guardrail events, metrics)
    evaluation_results.csv  one row per run, in the starter template's columns + extras
    summary.md          architecture comparison tables
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_public_evals import evaluate as public_minimum_checks  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.copilot import CopilotRun, run_copilot  # noqa: E402
from src.data_access import default_store  # noqa: E402
from src.mock_server import ensure_mock_api  # noqa: E402

RESULTS_DIR = ROOT / "evals" / "results"
SPECIALISTS = {"Security", "Privacy", "Legal"}


def load_cases(selected: list[str] | None) -> list[dict[str, Any]]:
    data = json.loads((ROOT / "evals" / "test_set.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    if selected:
        cases = [c for c in cases if c["case_id"] in selected]
    public = {c["case_id"]: c for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}
    for case in cases:
        case["public_expectations"] = public.get(case["case_id"], {}).get("expectations")
    return cases


def role_of(label: str) -> str:
    return label.split(" (")[0].strip()


def score(case: dict[str, Any], run: CopilotRun) -> dict[str, Any]:
    exp = case["expected"]
    d = run.decision
    roles = {role_of(a) for a in d.required_approvals}
    flags = set(d.risk_flags)
    acceptable = exp.get("acceptable_routes") or [exp["route"]]

    route_ok = run.category in acceptable
    model_route = (run.agent_output or {}).get("recommendation_category")
    model_route_ok = None if model_route is None else model_route in acceptable

    approvals_exact = None
    approvals_missing: list[str] = []
    approvals_extra: list[str] = []
    if exp.get("approvals") is not None:
        expected_roles = set(exp["approvals"])
        approvals_missing = sorted(expected_roles - roles)
        approvals_extra = sorted(roles - expected_roles)
        approvals_exact = not approvals_missing and not approvals_extra

    flags_missing = sorted(set(exp.get("flags_required", [])) - flags)
    flags_unexpected = sorted(set(exp.get("flags_forbidden", [])) & flags)

    missing_text = [m.lower() for m in d.missing_information]
    missing_groups_failed = [g for g in exp.get("missing_required", [])
                             if not any(tok in m for tok in g for m in missing_text)]
    too_many_missing = exp.get("max_missing") is not None and len(d.missing_information) > exp["max_missing"]

    public_failures = public_minimum_checks(d, case["public_expectations"]) if case.get("public_expectations") else None

    # Policy followed: no missing required approval/flag, no forbidden flag, missing-info handled.
    policy_followed = (not approvals_missing and not flags_missing and not flags_unexpected
                       and not missing_groups_failed and not too_many_missing)
    expected_specialists = set(exp.get("approvals") or []) & SPECIALISTS
    escalation_ok = d.human_review_required and expected_specialists <= roles and (
        route_ok if exp["route"] in {"escalate_manual_review", "request_more_information", "budget_exception_review"} else True)
    g = run.grounding
    grounded = not g.get("invalid_citations") and g.get("numeric_violations", 0) == 0 and len(d.evidence) >= 2

    injection_expected = "prompt_injection_detected" in exp.get("flags_required", [])
    ai_injection = (run.agent_output or {}).get("prompt_injection_suspected") if run.agent_output else None

    passed = route_ok and policy_followed and approvals_exact is not False and grounded and escalation_ok \
        and (public_failures is None or not public_failures)
    notes = []
    if not route_ok:
        notes.append(f"route {run.category} not in {acceptable}")
    if approvals_missing:
        notes.append(f"missing approvals {approvals_missing}")
    if approvals_extra:
        notes.append(f"extra approvals {approvals_extra}")
    if flags_missing:
        notes.append(f"missing flags {flags_missing}")
    if flags_unexpected:
        notes.append(f"forbidden flags {flags_unexpected}")
    if missing_groups_failed:
        notes.append(f"missing-info groups absent {missing_groups_failed}")
    if too_many_missing:
        notes.append(f"too many missing-info items ({len(d.missing_information)})")
    if public_failures:
        notes.append("public checks: " + "; ".join(public_failures))
    if run.fallback_reason:
        notes.append(f"fallback: {run.fallback_reason[:80]}")

    return {
        "passed": passed,
        "correct_next_action": route_ok,
        "model_route_correct": model_route_ok,
        "model_route": model_route,
        "final_route": run.category,
        "approvals_exact": approvals_exact,
        "over_escalation": bool(approvals_extra),
        "policy_followed": policy_followed,
        "human_escalation_correct": escalation_ok,
        "grounded_evidence": grounded,
        "public_minimum_pass": None if public_failures is None else not public_failures,
        "injection_expected": injection_expected,
        "ai_detected_injection": ai_injection,
        "guardrail_overrides": sum(1 for e in run.guardrail_events
                                   if e["type"] in {"category_overridden", "unsafe_claim_replaced"}),
        "evidence_backfilled": any(e["type"] == "evidence_backfilled" for e in run.guardrail_events),
        "citation_count": g.get("cited", 0),
        "invalid_citations": len(g.get("invalid_citations", [])),
        "numeric_violations": g.get("numeric_violations", 0),
        "notes": " | ".join(notes),
    }


def run_case(case: dict[str, Any], architecture: str, repeat: int) -> dict[str, Any]:
    store = default_store().with_overrides(case.get("overrides"))
    started = time.perf_counter()
    try:
        run = run_copilot(case.get("request_id"), architecture, request=case.get("request"), store=store,
                          vendor_risk_base_url=case.get("vendor_risk_base_url"))
    except Exception as exc:  # noqa: BLE001 - a crash is a recorded failure, not an aborted eval
        return {"case_id": case["case_id"], "architecture": architecture, "repeat": repeat, "error": repr(exc),
                "passed": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    s = score(case, run)
    m = run.metrics
    return {
        "case_id": case["case_id"], "edge_case": case["edge_case"], "source": case["source"],
        "architecture": architecture, "architecture_used": run.architecture_used, "repeat": repeat, **s,
        "latency_ms": m["latency_ms"], "llm_calls": m["llm_calls"], "tool_calls": m["tool_calls"],
        "agent_tool_calls": m["agent_tool_calls"], "input_tokens": m["input_tokens"],
        "output_tokens": m["output_tokens"], "cost_usd": m["cost_usd"] or 0.0,
        "trace": run.to_dict(),
    }


def pct(values: list[bool | None]) -> str:
    vals = [v for v in values if v is not None]
    return f"{100 * sum(vals) / len(vals):.0f}%" if vals else "n/a"


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def summarise(rows: list[dict[str, Any]], architectures: list[str], cases: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = ["# Evaluation results", "",
             f"Generated by `python evals/run_comparison.py` on {meta['generated_at']}. "
             f"Model `{meta['model']}` (effort `{meta['effort']}`), {meta['repeats']} repeat(s) per case for LLM "
             f"architectures, {len(cases)} cases, workers={meta['workers']}. Policy reference date {meta['reference_date']}.", "",
             "## Architecture comparison (all runs)", "",
             "| Metric | " + " | ".join(architectures) + " |", "|---|" + "---:|" * len(architectures)]

    def col(fn) -> list[str]:
        return [fn([r for r in rows if r["architecture"] == a]) for a in architectures]

    def mean(key: str, digits: int = 1):
        return lambda rs: f"{statistics.mean(r.get(key, 0) or 0 for r in rs):,.{digits}f}" if rs else "n/a"

    metrics = [
        ("Runs", lambda rs: str(len(rs))),
        ("Runs passing all checks", lambda rs: f"{sum(r['passed'] for r in rs)}/{len(rs)} ({pct([r['passed'] for r in rs])})"),
        ("Public cases meeting starter minimum checks", lambda rs: pct([r.get("public_minimum_pass") for r in rs])),
        ("Correct next action (final, after guardrails)", lambda rs: pct([r.get("correct_next_action") for r in rs])),
        ("Correct next action (model's own choice)", lambda rs: pct([r.get("model_route_correct") for r in rs])),
        ("Approvals exactly right", lambda rs: pct([r.get("approvals_exact") for r in rs])),
        ("Policy + deterministic rules followed", lambda rs: pct([r.get("policy_followed") for r in rs])),
        ("Escalation / human review correct", lambda rs: pct([r.get("human_escalation_correct") for r in rs])),
        ("Evidence grounded (valid citations, no unsupported numbers)", lambda rs: pct([r.get("grounded_evidence") for r in rs])),
        ("Model citations / run", mean("citation_count")),
        ("Invalid citations (total)", lambda rs: str(sum(r.get("invalid_citations", 0) for r in rs))),
        ("Unsupported numeric claims (total)", lambda rs: str(sum(r.get("numeric_violations", 0) for r in rs))),
        ("Guardrail overrides of model output (total)", lambda rs: str(sum(r.get("guardrail_overrides", 0) for r in rs))),
        ("Runs where guardrail back-filled skipped evidence", lambda rs: str(sum(bool(r.get("evidence_backfilled")) for r in rs))),
        ("Injection cases where the AI itself flagged injection",
         lambda rs: pct([r.get("ai_detected_injection") for r in rs if r.get("injection_expected")])),
        ("Runs that fell back to rules (LLM failure)", lambda rs: str(sum(r.get("architecture_used") == "rules" and r["architecture"] != "rules" for r in rs))),
        ("Avg latency (ms)", mean("latency_ms", 0)),
        ("p95 latency (ms)", lambda rs: f"{p95([r.get('latency_ms', 0) for r in rs]):,.0f}"),
        ("Avg LLM calls", mean("llm_calls")),
        ("Avg tool calls (total)", mean("tool_calls")),
        ("Avg tool calls made by agents", mean("agent_tool_calls")),
        ("Avg input tokens", mean("input_tokens", 0)),
        ("Avg output tokens", mean("output_tokens", 0)),
        ("Avg cost per request (USD)", mean("cost_usd", 4)),
    ]
    for name, fn in metrics:
        lines.append(f"| {name} | " + " | ".join(col(fn)) + " |")

    lines += ["", "## Per-case pass rate", "", "| Case | Edge case | " + " | ".join(architectures) + " |",
              "|---|---|" + "---:|" * len(architectures)]
    for case in cases:
        cells = []
        for a in architectures:
            rs = [r for r in rows if r["architecture"] == a and r["case_id"] == case["case_id"]]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        lines.append(f"| {case['case_id']} | {case['edge_case']} | " + " | ".join(cells) + " |")

    failures = [r for r in rows if not r["passed"]]
    lines += ["", "## Failures and notable notes", ""]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines.append(f"- **{r['case_id']} / {r['architecture']} / repeat {r['repeat']}**: {r.get('notes') or r.get('error')}")
    overridden = [r for r in rows if r.get("guardrail_overrides")]
    if overridden:
        lines += ["", "## Guardrail interventions (model output corrected by code)", ""]
        for r in overridden:
            events = [e for e in r["trace"]["guardrail_events"] if e["type"] in {"category_overridden", "unsafe_claim_replaced"}]
            lines.append(f"- {r['case_id']} / {r['architecture']} / repeat {r['repeat']}: " + "; ".join(e["detail"] for e in events))
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--architectures", nargs="+", default=["single", "staged", "rules"],
                        choices=["single", "staged", "rules"])
    parser.add_argument("--repeats", type=int, default=3, help="Repeats per case for LLM architectures")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cases", nargs="*", help="Subset of case IDs")
    parser.add_argument("--out", default=str(RESULTS_DIR))
    args = parser.parse_args()

    settings = get_settings()
    if any(a != "rules" for a in args.architectures) and not settings.llm_available:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env, or run with --architectures rules.")
    if not ensure_mock_api():
        sys.exit("Vendor-risk mock API is not reachable and could not be started.")

    cases = load_cases(args.cases)
    jobs = [(c, a, i) for a in args.architectures for c in cases for i in range(1 if a == "rules" else args.repeats)]
    print(f"Running {len(jobs)} runs ({len(cases)} cases x {args.architectures}) with {args.workers} workers ...")

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, c, a, i): (c, a, i) for c, a, i in jobs}
        for n, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            rows.append(row)
            status = "PASS" if row["passed"] else "FAIL"
            print(f"[{n:>3}/{len(jobs)}] {status} {row['case_id']:<7} {row['architecture']:<6} "
                  f"r{row['repeat']} {row.get('latency_ms', 0):>8.0f} ms  {row.get('notes') or row.get('error') or ''}", flush=True)

    order = {c["case_id"]: i for i, c in enumerate(cases)}
    rows.sort(key=lambda r: (args.architectures.index(r["architecture"]), order[r["case_id"]], r["repeat"]))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "runs.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    columns = ["case_id", "architecture", "repeat", "passed", "correct_next_action", "grounded_evidence", "policy_followed",
               "human_escalation_correct", "approvals_exact", "model_route", "final_route", "latency_ms", "llm_calls",
               "tool_calls", "agent_tool_calls", "input_tokens", "output_tokens", "cost_usd", "notes"]
    with (out / "evaluation_results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    from src.config import policy_reference_date

    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "model": settings.model, "effort": settings.effort,
            "repeats": args.repeats, "workers": args.workers, "reference_date": policy_reference_date().isoformat()}
    (out / "summary.md").write_text(summarise(rows, args.architectures, cases, meta), encoding="utf-8")
    print(f"\nWrote {out / 'summary.md'}, evaluation_results.csv and runs.jsonl")
    for a in args.architectures:
        rs = [r for r in rows if r["architecture"] == a]
        print(f"  {a:<7} passed {sum(r['passed'] for r in rs)}/{len(rs)}")


if __name__ == "__main__":
    main()
