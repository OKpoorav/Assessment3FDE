from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.copilot import run_copilot


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the public/hidden evaluation harness.

    ``architecture`` is "single" (Architecture A) or "staged" (Architecture B).
    Without LLM credentials, or if the model call fails, both fall back to the
    deterministic rules path and add the ``ai_assist_unavailable`` risk flag.
    """
    return run_copilot(request_id, architecture).decision
