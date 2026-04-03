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
from avatar_backend.services.session_manager import SessionManager
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import create_tts_service
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.routers import health, chat
from avatar_backend.routers import voice, avatar_ws, announce
from avatar_backend.routers import admin

_INSTALL_DIR = Path("/opt/avatar-server")
_CONFIG_DIR  = _INSTALL_DIR / "config"


def configure_logging(log_level: str) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
    )


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

    acl = ACLManager.from_yaml_safe(str(_CONFIG_DIR / "acl.yaml"))
    app.state.acl_manager = acl

    app.state.llm_service     = LLMService()
    app.state.session_manager = SessionManager(system_prompt)
    app.state.ha_proxy        = HAProxy(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        acl=acl,
    )

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

    if await app.state.ha_proxy.is_connected():
        logger.info("ha_proxy.connected", ha_url=settings.ha_url)
    else:
        logger.warning("ha_proxy.not_connected", ha_url=settings.ha_url)

    cleanup_task = asyncio.create_task(
        _session_cleanup_loop(app.state.session_manager)
    )

    logger.info("avatar_backend.ready")
    yield

    cleanup_task.cancel()
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
        allow_origins=["*"],
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
