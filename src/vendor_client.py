"""Client for the external vendor-risk service.

The starter client raised raw ``requests`` exceptions, so a connection refusal,
a timeout, a 404 and a 503 all looked the same to callers (or crashed them).
This version never raises for transport/service problems: it returns a typed
result whose ``status`` is one of ``ok``, ``not_found`` or ``unavailable``, so the
policy layer can treat "no record" and "could not verify" differently and never
infer a favourable status from a failure.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import requests

from src.config import get_settings

Status = Literal["ok", "not_found", "unavailable"]

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class VendorRiskResult:
    status: Status
    vendor_name: str
    record: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    http_status: int | None = None
    attempts: int = 0
    endpoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_vendor_risk(
    vendor_name: str,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    retries: int = 1,
    backoff_seconds: float = 0.3,
) -> VendorRiskResult:
    settings = get_settings()
    base = (base_url or settings.vendor_risk_base_url).rstrip("/")
    timeout = timeout_seconds or settings.vendor_risk_timeout_seconds
    url = f"{base}/vendor-risk/{quote(vendor_name, safe='')}"

    last_error = "unknown error"
    last_status: int | None = None
    attempts = 0
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: could not reach vendor-risk service"
            last_status = None
        else:
            last_status = response.status_code
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    return VendorRiskResult("unavailable", vendor_name, error="Malformed JSON from vendor-risk service",
                                            http_status=200, attempts=attempts, endpoint=url)
                return VendorRiskResult("ok", vendor_name, record=body, http_status=200, attempts=attempts, endpoint=url)
            if response.status_code == 404:
                return VendorRiskResult("not_found", vendor_name, error="No vendor-risk record", http_status=404,
                                        attempts=attempts, endpoint=url)
            detail = _detail(response)
            last_error = f"HTTP {response.status_code}: {detail}"
            if response.status_code not in _RETRYABLE_STATUS:
                break
        if attempt < retries:
            time.sleep(backoff_seconds * (attempt + 1))

    return VendorRiskResult("unavailable", vendor_name, error=last_error, http_status=last_status,
                            attempts=attempts, endpoint=url)


def _detail(response: requests.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:300]
    except ValueError:
        return response.text[:300]
