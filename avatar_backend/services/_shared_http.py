"""Shared httpx.AsyncClient singleton for all LLM HTTP calls."""
from __future__ import annotations
import httpx

_SHARED_HTTP: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    """Return (or lazily create) the shared async HTTP client.

    Using a single client avoids per-request SSL handshakes and TCP setup.
    Each caller provides its own timeout per-request.
    """
    global _SHARED_HTTP
    if _SHARED_HTTP is None or _SHARED_HTTP.is_closed:
        _SHARED_HTTP = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
        )
    return _SHARED_HTTP


async def close_shared_http_client() -> None:
    global _SHARED_HTTP
    if _SHARED_HTTP is not None and not _SHARED_HTTP.is_closed:
        await _SHARED_HTTP.aclose()
        _SHARED_HTTP = None
