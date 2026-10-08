"""Make sure the local mock vendor-risk API is reachable.

The starter eval runner called ``handle_request`` without starting the mock API,
so every vendor-risk lookup failed and every case looked like an outage. When the
configured URL is the default local mock and nothing is listening, we start the
API in a background thread of the current process. Set MOCK_API_AUTOSTART=0 to
disable (e.g. to test real outages).
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

from src.config import DEFAULT_MOCK_API_URL, get_settings

_lock = threading.Lock()
_started = False


def is_healthy(base_url: str, timeout: float = 0.5) -> bool:
    try:
        return requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout).ok
    except requests.RequestException:
        return False


def ensure_mock_api(base_url: str | None = None) -> bool:
    """Return True if the vendor-risk API is reachable (starting the local mock if allowed)."""
    global _started
    settings = get_settings()
    url = (base_url or settings.vendor_risk_base_url).rstrip("/")
    if is_healthy(url):
        return True
    if not settings.mock_api_autostart or url != DEFAULT_MOCK_API_URL.rstrip("/"):
        return False
    with _lock:
        if _started:
            return is_healthy(url, timeout=2)
        import uvicorn

        from mock_api.app import app

        parsed = urlparse(url)
        server = uvicorn.Server(uvicorn.Config(app, host=parsed.hostname or "127.0.0.1", port=parsed.port or 8001,
                                               log_level="warning"))
        threading.Thread(target=server.run, name="mock-vendor-risk-api", daemon=True).start()
        _started = True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if is_healthy(url):
            return True
        time.sleep(0.1)
    return False
