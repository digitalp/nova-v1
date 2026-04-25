import logging
import re
from contextlib import asynccontextmanager

import structlog
import structlog.stdlib
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

from avatar_backend.bootstrap import bootstrap, teardown
from avatar_backend.config import get_settings
from avatar_backend.runtime_paths import config_dir, logs_dir, static_dir
from avatar_backend.routers import admin, announce, announce_vision, avatar_ws, chat, health, voice

_CONFIG_DIR = config_dir()
_LOG_FILE, _LOG_MAX_BYTES, _LOG_BACKUP_COUNT = logs_dir() / "avatar-backend.log", 5 * 1024 * 1024, 2


def configure_logging(log_level: str) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level, structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(), cache_logger_on_first_use=True,
    )
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(message)s")
    sh = logging.StreamHandler(); sh.setFormatter(fmt)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(_LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=[sh, fh])
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _load_system_prompt() -> str:
    path = _CONFIG_DIR / "system_prompt.txt"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        structlog.get_logger().warning("system_prompt.not_found", path=str(path))
        return "You are a helpful smart home assistant."
    return _compress_system_prompt(raw)


def _compress_system_prompt(prompt: str) -> str:
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'\n={3,}\n', '\n', prompt)).strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()
    logger.info("avatar_backend.starting", host=settings.host, port=settings.port, model=settings.ollama_model)

    system_prompt = _load_system_prompt()
    logger.info("system_prompt.loaded", chars=len(system_prompt))

    container = await bootstrap(app, settings, system_prompt)
    app.state._container = container

    # Auto-sync MDM devices to family members
    if hasattr(container, 'family_service') and container.family_service:
        try:
            from avatar_backend.services import mdm_client as _mdm
            _devs = await _mdm.get_devices()
            _n = container.family_service.sync_mdm_devices(_devs)
            if _n:
                logger.info("family_service.mdm_sync", new_mappings=_n)
        except Exception as _me:
            logger.info("family_service.mdm_sync_skipped", reason=str(_me)[:80])

    yield

    await teardown(container)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next) -> StarletteResponse:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        path = request.url.path
        # Cache static assets aggressively
        if path.startswith("/static/avatars/"):
            response.headers.setdefault("Cache-Control", "public, max-age=0, must-revalidate")
        elif path.startswith("/static/") and any(path.endswith(ext) for ext in (".js", ".css", ".svg", ".png", ".jpg", ".webp")):
            response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        if path.startswith("/admin/music-ui"):
            response.headers.pop("X-Frame-Options", None)
            response.headers.pop("Content-Security-Policy", None)
            return response
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/admin"):
            response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            response.headers.setdefault("Content-Security-Policy", (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
                "https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
                "https://cdn.jsdelivr.net; font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; media-src 'self' blob:; "
                "connect-src 'self' ws: wss: https:; frame-src 'self' https:; frame-ancestors 'self'"
            ))
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Avatar Backend", description="AI avatar backend for Home Assistant",
        version="0.7.0", lifespan=lifespan,
        docs_url="/docs" if settings.log_level == "DEBUG" else None, redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins_list,
        allow_credentials=True, allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(voice.router)
    app.include_router(avatar_ws.router)
    app.include_router(announce.router)
    app.include_router(announce_vision.router)
    app.include_router(admin.router)

    _static_dir = static_dir()
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

        @app.post("/auth/set-cookie", include_in_schema=False)
        async def set_api_key_cookie(request: StarletteRequest):
            import secrets as _secrets
            from avatar_backend.config import get_settings as _gs
            body = await request.json()
            key = body.get("api_key", "")
            if not key or not _secrets.compare_digest(key.encode(), _gs().api_key.encode()):
                return JSONResponse({"ok": False}, status_code=401)
            is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            resp = JSONResponse({"ok": True})
            resp.set_cookie(
                key="nova_api_key", value=key, httponly=True, samesite="lax",
                secure=is_https, max_age=31536000, path="/",
            )
            return resp

        @app.get("/avatar", include_in_schema=False)
        async def avatar_page():
            return FileResponse(str(_static_dir / "avatar.html"))

    return app


app = create_app()
