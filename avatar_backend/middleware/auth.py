import secrets
import time
from fastapi import Request, WebSocket, HTTPException, status
import structlog

logger = structlog.get_logger()

_HEADER_NAME = "X-API-Key"

# ── Short-lived WebSocket tokens ──────────────────────────────────────────────
# The browser WebSocket API cannot set custom headers, so persistent API keys
# must not appear in WebSocket URLs. Instead, clients exchange their key for a
# short-lived single-use token via POST /ws/token (header-authenticated), then
# connect with ?token=<token>. Tokens expire after _TOKEN_TTL seconds and are
# deleted on first use.

_WS_TOKENS: dict[str, float] = {}  # token → expiry (monotonic)
_TOKEN_TTL  = 30  # seconds


def _purge_expired() -> None:
    now = time.monotonic()
    expired = [t for t, exp in _WS_TOKENS.items() if exp < now]
    for t in expired:
        del _WS_TOKENS[t]


def issue_ws_token() -> str:
    """Generate a short-lived single-use WebSocket auth token."""
    _purge_expired()
    token = secrets.token_hex(32)
    _WS_TOKENS[token] = time.monotonic() + _TOKEN_TTL
    return token


async def verify_api_key(request: Request) -> None:
    """
    FastAPI dependency that validates the X-API-Key header.
    Uses secrets.compare_digest to prevent timing attacks.
    Also accepts ?api_key= query param for HA rest_command automations
    that cannot set custom headers.
    """
    from avatar_backend.config import get_settings

    settings = get_settings()

    incoming = (
        request.headers.get(_HEADER_NAME)
        or request.query_params.get("api_key", "")
    )

    if not incoming or not secrets.compare_digest(
        incoming.encode(), settings.api_key.encode()
    ):
        logger.warning(
            "auth.rejected",
            path=str(request.url.path),
            client=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def verify_api_key_ws(websocket: WebSocket) -> None:
    """
    FastAPI dependency for WebSocket endpoints.
    Accepts a short-lived single-use token from the ?token= query param.
    Tokens are issued by POST /ws/token (requires X-API-Key header).
    Closes the WebSocket with code 1008 (policy violation) if rejected.
    """
    _purge_expired()
    token = websocket.query_params.get("token", "")
    now   = time.monotonic()

    if token and token in _WS_TOKENS and _WS_TOKENS[token] > now:
        del _WS_TOKENS[token]  # single-use: consume on first connection
        return

    logger.warning(
        "auth.ws_rejected",
        path=str(websocket.url.path),
        client=websocket.client.host if websocket.client else "unknown",
    )
    await websocket.close(code=1008)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing WebSocket token",
    )
