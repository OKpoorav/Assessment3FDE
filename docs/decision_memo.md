# Architecture Decision Memo

## Decision

**Ship Architecture A, the single agent.**

## Evidence

Same 22 cases, 3 runs each (6 public, 4 dataset, 12 synthetic edge cases). Source: `evals/results/summary.md`, model `claude-opus-5-5`, effort `low`.

| Metric | Single agent (A) | Staged (B) | Rules only (ref.) |
|---|---:|---:|---:|
| Runs passing all checks | 65/66 (98%) | 66/66 (100%) | 19/22 (86%) |
| Public cases, starter minimum checks | 100% | 100% | 100% |
| Model's own next-action choice correct | 100% | 100% | n/a |
| Approvals exactly right / policy followed | 100% / 98% | 100% / 100% | 100% / 95% |
| Evidence grounded (invalid citations, unsupported numbers) | 100% (0, 0) | 100% (0, 0) | 100% |
| AI flagged the injection itself | 100% | 100% | n/a |
| Avg / p95 latency | 19.5 s / 31.8 s | 27.3 s / 37.9 s | 0.03 s |
| Avg LLM calls / tool calls | 3.0 / 6.0 | 3.0 / 6.0 | 0 / 6 |
| Avg cost per request | $0.062 | $0.078 | $0 |

## Interpretation

- **Quality is equal within noise.** A's one miss (SYN-09, 1 of 3 runs) asked for clarification on a contradictory data class instead of routing to Security/Privacy. The answer is cautious and defensible, but one run out of 66 cannot separate the architectures.
- **The hard policy work is done by code in both.** Approvals, flags and routing were 100% correct for A, for B and for rules-only. The LLM's measurable value is judgment: only the LLM architectures pass the "existing tool solves the need" cases (DS-03, SYN-12) and the trigger-word-free injection (SYN-11). One agent captures all of that.
- **B costs more and adds no measured accuracy.** It is about 40% slower and about 25% more expensive. Its analyst also skipped the vendor tools on the vague request in 3 of 3 runs, which code had to back-fill.

## Trade-offs

B separates duties and adds a second reading of the analyst's claims. It also exposed a hand-off weakness: when the reviewer saw only summaries, it rejected true claims, so it needs the full tool results (more input tokens). A is simpler to debug (one transcript), faster and cheaper.

## Risks / validate before production

An independently written, larger test set (my policy interpretations, such as SSO or split purchases, may differ), reviewer override rate from the audit log, effort/model sweeps for latency, and injection red-teaming.

## Why A is the right MVP

The client's risk sits in thresholds, required reviews and unverifiable evidence. Those live in tested code that both architectures share. The agent only has to gather evidence and make one judgment, and a single agent does that as accurately, faster and more cheaply. B remains available through `handle_request(..., "staged")` if a separate reviewer becomes an audit requirement.
