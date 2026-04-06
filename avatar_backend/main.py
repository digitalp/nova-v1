import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import structlog.stdlib
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from avatar_backend.config import get_settings
from avatar_backend.models.acl import ACLManager
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.cost_log import CostLog
from avatar_backend.services.decision_log import DecisionLog
from avatar_backend.services.session_manager import SessionManager
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import create_tts_service
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.services.proactive_service import ProactiveService
from avatar_backend.services.metrics_db import MetricsDB
from avatar_backend.services.system_metrics import SystemMetrics
from avatar_backend.services.user_service import UserService
from avatar_backend.routers import health, chat
from avatar_backend.routers import voice, avatar_ws, announce
from avatar_backend.routers import admin
from avatar_backend.routers.announce import AnnounceRequest, announce_handler

_INSTALL_DIR = Path("/opt/avatar-server")
_CONFIG_DIR  = _INSTALL_DIR / "config"


_LOG_FILE = _INSTALL_DIR / "logs" / "avatar-backend.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_LOG_BACKUP_COUNT = 2


def configure_logging(log_level: str) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(message)s")

    # Stream handler → journald (stdout captured by systemd)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    # Rotating file handler → /tmp/avatar-backend.log (for admin panel SSE)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=level, handlers=[stream_handler, file_handler])

    # Suppress uvicorn access log — it records full URLs including ?api_key= query params.
    # Application-level request logging is handled by structlog instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _load_system_prompt() -> str:
    path = _CONFIG_DIR / "system_prompt.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        structlog.get_logger().warning("system_prompt.not_found", path=str(path))
        return "You are a helpful smart home assistant."


async def _session_cleanup_loop(sm: SessionManager, interval: int = 300) -> None:
    while True:
        await asyncio.sleep(interval)
        await sm.cleanup_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()

    logger.info("avatar_backend.starting",
                host=settings.host, port=settings.port,
                model=settings.ollama_model)

    system_prompt = _load_system_prompt()
    logger.info("system_prompt.loaded", chars=len(system_prompt))

    # ── User / session service ─────────────────────────────────────────────
    user_service = UserService(_CONFIG_DIR / "users.json")
    if not user_service.has_users():
        logger.warning(
            "avatar_backend.no_users",
            detail="No admin users found. Visit /admin/login to create the first account.",
        )
    app.state.user_service = user_service

    acl = ACLManager.from_yaml_safe(str(_CONFIG_DIR / "acl.yaml"))
    app.state.acl_manager = acl

    app.state.llm_service     = LLMService()
    app.state.session_manager = SessionManager(system_prompt)
    app.state.ha_proxy        = HAProxy(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        acl=acl,
    )

    app.state.audio_cache = {}  # token → (wav_bytes, expiry) for one-shot audio serving
    app.state.stt_service = STTService(model_name=settings.whisper_model)
    app.state.tts_service = create_tts_service(settings)
    logger.info("tts_service.configured", provider=settings.tts_provider)
    app.state.ws_manager  = ConnectionManager()

    app.state.speaker_service = SpeakerService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        speakers=settings.speaker_list,
        tts_engine=settings.tts_engine,
    )
    if settings.speaker_list:
        logger.info("speaker_service.configured", speakers=settings.speaker_list)
    else:
        logger.info("speaker_service.disabled")

    if settings.ha_url.startswith("http://"):
        logger.warning(
            "ha_proxy.insecure_url",
            ha_url=settings.ha_url,
            detail="HA_URL uses plain HTTP — credentials and data are sent unencrypted. "
                   "Set HA_URL to https:// in .env to enable TLS.",
        )

    if await app.state.ha_proxy.is_connected():
        logger.info("ha_proxy.connected", ha_url=settings.ha_url)
    else:
        logger.warning("ha_proxy.not_connected", ha_url=settings.ha_url)

    # Announce callback for ProactiveService — calls the announce pipeline directly
    async def _proactive_announce(message: str, priority: str = "normal") -> None:
        from types import SimpleNamespace
        fake_request = SimpleNamespace(app=app)
        await announce_handler(
            AnnounceRequest(message=message, priority=priority),
            fake_request,
        )

    proactive = ProactiveService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        ha_proxy=app.state.ha_proxy,
        llm_service=app.state.llm_service,
        announce_fn=_proactive_announce,
        system_prompt=system_prompt,
    )
    app.state.proactive_service = proactive
    await proactive.start()

    cleanup_task = asyncio.create_task(
        _session_cleanup_loop(app.state.session_manager)
    )

    # Decision log — shared by proactive service and chat service
    decision_log = DecisionLog()
    app.state.decision_log = decision_log

    # Persistent metrics DB (LLM costs + system samples)
    metrics_db = MetricsDB()
    app.state.metrics_db = metrics_db

    # System metrics poller — CPU/RAM/disk/GPU every 5 s
    sys_metrics = SystemMetrics(db=metrics_db, interval=5)
    app.state.sys_metrics = sys_metrics
    await sys_metrics.start()

    # Cost log — tracks token usage and cost per LLM call
    cost_log = CostLog()
    cost_log.set_db(metrics_db)
    app.state.cost_log = cost_log
    app.state.llm_service.set_cost_log(cost_log)
    proactive = getattr(app.state, 'proactive_service', None)
    if proactive:
        proactive.set_decision_log(decision_log)

    logger.info("avatar_backend.ready")
    yield

    cleanup_task.cancel()
    await proactive.stop()
    await app.state.sys_metrics.stop()
    logger.info("avatar_backend.stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Avatar Backend",
        description="AI avatar backend for Home Assistant",
        version="0.7.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.log_level == "DEBUG" else None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(voice.router)
    app.include_router(avatar_ws.router)
    app.include_router(announce.router)
    app.include_router(admin.router)

    # Serve 3D avatar page and static assets
    _static_dir = _INSTALL_DIR / "static"
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

        @app.get("/avatar", include_in_schema=False)
        async def avatar_page():
            return FileResponse(str(_static_dir / "avatar.html"))

    return app


app = create_app()
