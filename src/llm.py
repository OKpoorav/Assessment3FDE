"""Thin wrapper around the Anthropic Messages API with per-run telemetry."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.config import Settings, estimate_cost_usd


class LLMError(RuntimeError):
    """Raised when the model cannot produce a usable response (API error, refusal, truncation)."""


@dataclass
class LLMUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0
    per_call: list[dict[str, Any]] = field(default_factory=list)

    def cost_usd(self, model: str) -> float | None:
        return estimate_cost_usd(model, self.input_tokens, self.output_tokens,
                                 cache_read_tokens=self.cache_read_tokens, cache_write_tokens=self.cache_write_tokens)


class LLMClient:
    """Calls the Messages API. ``client`` can be injected for tests (any object with ``messages.create``)."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=settings.llm_timeout_seconds, max_retries=2)
        self._client = client
        self.usage = LLMUsage()

    def create(self, *, agent: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               max_tokens: int = 8000) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.use_server_fallbacks:
            # Re-run on Anthropic's recommended fallback model if a safety classifier declines.
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}

        start = time.perf_counter()
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced as LLMError so the caller can degrade gracefully
            self.usage.calls += 1
            self.usage.per_call.append({"agent": agent, "error": f"{type(exc).__name__}: {exc}"[:300]})
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        elapsed = (time.perf_counter() - start) * 1000

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cache_tok = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        write_tok = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.usage.calls += 1
        self.usage.input_tokens += in_tok
        self.usage.output_tokens += out_tok
        self.usage.cache_read_tokens += cache_tok
        self.usage.cache_write_tokens += write_tok
        self.usage.latency_ms += elapsed
        self.usage.per_call.append({"agent": agent, "latency_ms": round(elapsed, 1), "input_tokens": in_tok,
                                    "cache_read_tokens": cache_tok, "cache_write_tokens": write_tok,
                                    "output_tokens": out_tok, "stop_reason": response.stop_reason})

        if response.stop_reason == "refusal":
            raise LLMError("Model declined the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError("Model output truncated (stop_reason=max_tokens)")
        return response
