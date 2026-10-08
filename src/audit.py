"""Append-only log of human review decisions taken in the UI.

The copilot never approves anything; this file records what a *human* decided
after reading the recommendation, so the hand-off is auditable.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import VAR_DIR

AUDIT_LOG = VAR_DIR / "audit_log.jsonl"
HUMAN_ACTIONS = [
    "Accept recommendation and route to approvers",
    "Return to requester for information",
    "Reject request",
    "Override recommendation (justification required)",
]
_lock = threading.Lock()


def record_decision(*, request_id: str, reviewer: str, action: str, comment: str, recommendation: str,
                    architecture: str, path: Path = AUDIT_LOG) -> dict[str, Any]:
    if action not in HUMAN_ACTIONS:
        raise ValueError(f"Unknown action: {action}")
    if not reviewer.strip():
        raise ValueError("Reviewer name is required")
    if action.startswith(("Override", "Reject")) and not comment.strip():
        raise ValueError("A justification is required for overrides and rejections")
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id,
        "reviewer": reviewer.strip(),
        "action": action,
        "comment": comment.strip(),
        "copilot_recommendation": recommendation,
        "architecture": architecture,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def list_decisions(request_id: str, path: Path = AUDIT_LOG) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("request_id") == request_id:
            rows.append(row)
    return rows
