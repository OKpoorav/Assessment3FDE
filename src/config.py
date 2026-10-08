"""Runtime configuration.

Everything tunable lives here and is read from environment variables (a local
`.env` is loaded by `src/__init__.py`). Nothing in this module talks to the
network.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
POLICY_PATH = DATA_DIR / "procurement_policy.md"
VAR_DIR = ROOT / "var"

DEFAULT_MOCK_API_URL = "http://127.0.0.1:8001"
_FALLBACK_REFERENCE_DATE = date(2026, 9, 30)

# USD per million tokens (input, output). Used only for the cost estimate in telemetry.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def policy_reference_date() -> date:
    """Read the evaluation reference date from the policy document itself.

    The policy is the source of truth for date-based checks; the machine clock is
    never used.
    """
    try:
        text = POLICY_PATH.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_REFERENCE_DATE
    match = re.search(r"reference date:\*\*\s*(\d{4}-\d{2}-\d{2})", text, flags=re.IGNORECASE)
    return date.fromisoformat(match.group(1)) if match else _FALLBACK_REFERENCE_DATE


@lru_cache(maxsize=1)
def policy_version() -> str:
    try:
        text = POLICY_PATH.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"Policy version:\*\*\s*([\w.\-]+)", text)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.getenv("MODEL_NAME") or "claude-opus-5-5")
    effort: str = field(default_factory=lambda: os.getenv("MODEL_EFFORT") or "low")
    max_agent_turns: int = field(default_factory=lambda: int(os.getenv("MAX_AGENT_TURNS") or 8))
    llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS") or 90))
    use_server_fallbacks: bool = field(default_factory=lambda: _env_bool("ANTHROPIC_SERVER_FALLBACKS", True))
    vendor_risk_base_url: str = field(
        default_factory=lambda: (os.getenv("VENDOR_RISK_BASE_URL") or DEFAULT_MOCK_API_URL).rstrip("/")
    )
    vendor_risk_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("VENDOR_RISK_TIMEOUT_SECONDS") or 3.0)
    )
    mock_api_autostart: bool = field(default_factory=lambda: _env_bool("MOCK_API_AUTOSTART", True))

    @property
    def llm_available(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def get_settings() -> Settings:
    return Settings()


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, *, cache_read_tokens: int = 0,
                      cache_write_tokens: int = 0) -> float | None:
    """Estimate request cost. Cache writes bill at 1.25x input, cache reads at 0.1x input."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    inp, out = pricing[0] / 1e6, pricing[1] / 1e6
    total = (input_tokens * inp + cache_write_tokens * inp * 1.25 + cache_read_tokens * inp * 0.1
             + output_tokens * out)
    return round(total, 5)
