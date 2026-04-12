import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import structlog.stdlib
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from avatar_backend.config import get_settings
from avatar_backend.models.acl import ACLManager
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.presence_context import PresenceContextService
from avatar_backend.services.prompt_sync_service import PromptSyncService
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.cost_log import CostLog
from avatar_backend.services.decision_log import DecisionLog
from avatar_backend.services.log_store import LogStore
from avatar_backend.services.session_manager import SessionManager
from avatar_backend.services.conversation_service import ConversationService
from avatar_backend.services.action_service import ActionService
from avatar_backend.services.persistent_memory import PersistentMemoryService
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import create_tts_service
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.services.realtime_voice_service import (
    RealtimeVoiceService,
    create_realtime_voice_adapter,
)
from avatar_backend.services.surface_state_service import SurfaceStateService
from avatar_backend.services.event_service import EventService
from avatar_backend.services.event_bus import EventBusService
from avatar_backend.services.event_store import EventStoreService
from avatar_backend.services.camera_event_service import CameraEventService
from avatar_backend.services.proactive_service import ProactiveService
from avatar_backend.services.open_loop_automation_service import OpenLoopAutomationService
from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService
from avatar_backend.services.sensor_watch_service import SensorWatchService
from avatar_backend.services.coral_wake_detector import CoralWakeDetector
from avatar_backend.services.issue_autofix_service import IssueAutoFixService
from avatar_backend.services.metrics_db import MetricsDB
from avatar_backend.services.motion_clip_service import MotionClipService
from avatar_backend.services.system_metrics import SystemMetrics
from avatar_backend.services.user_service import UserService
from avatar_backend.runtime_paths import config_dir, install_dir, logs_dir, static_dir
from avatar_backend.routers import health, chat
from avatar_backend.routers import voice, avatar_ws, announce
from avatar_backend.routers import admin
from avatar_backend.routers.announce import AnnounceRequest, announce_handler

_INSTALL_DIR = install_dir()
_CONFIG_DIR  = config_dir()


_LOG_FILE = logs_dir() / "avatar-backend.log"
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


async def _restart_fully_kiosk_after_startup(app: FastAPI, delay_s: float = 5.0) -> None:
    await asyncio.sleep(delay_s)
    logger = structlog.get_logger()
    ws_mgr = getattr(app.state, "ws_manager", None)
    ha = getattr(app.state, "ha_proxy", None)
    if ha is None:
        logger.warning("avatar_backend.kiosk_restart_skipped", reason="ha_proxy_unavailable")
    else:
        try:
            result = await ha.call_service("button", "press", "button.rk3566_restart_browser")
        except Exception as exc:
            logger.warning(
                "avatar_backend.kiosk_restart_failed",
                entity_id="button.rk3566_restart_browser",
                error=str(exc),
            )
        else:
            if not result.success:
                logger.warning(
                    "avatar_backend.kiosk_restart_failed",
                    entity_id="button.rk3566_restart_browser",
                    detail=result.message,
                )
            else:
                logger.info(
                    "avatar_backend.kiosk_restart_requested",
                    entity_id="button.rk3566_restart_browser",
                )
    if ws_mgr is not None:
        payload = {"type": "server_restarted"}
        await ws_mgr.broadcast_json(payload)
        await ws_mgr.broadcast_to_voice_json(payload)
        logger.info("avatar_backend.restart_signal_broadcast")


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
    app.state.system_prompt   = system_prompt
    app.state.ha_proxy        = HAProxy(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        acl=acl,
    )

    app.state.presence_service = PresenceContextService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
    )
    app.state.prompt_sync_service = PromptSyncService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        llm_service=app.state.llm_service,
        prompt_file=_CONFIG_DIR / "system_prompt.txt",
        app=app,
    )
    app.state.prompt_sync_service.start()
    app.state.audio_cache = {}  # token → (wav_bytes, expiry) for one-shot audio serving
    app.state.recent_event_contexts = {}
    app.state.stt_service = STTService(model_name=settings.whisper_model)
    from avatar_backend.routers.voice import _is_wake_word
    app.state.coral_wake_detector = CoralWakeDetector.build(
        app.state.stt_service, _is_wake_word
    )
    app.state.tts_service = create_tts_service(settings)
    logger.info("tts_service.configured", provider=settings.tts_provider)
    app.state.ws_manager  = ConnectionManager()
    app.state.conversation_service = ConversationService(app)
    app.state.realtime_voice_adapter = create_realtime_voice_adapter(settings)
    app.state.realtime_voice_service = RealtimeVoiceService()
    app.state.action_service = ActionService()
    app.state.surface_state_service = SurfaceStateService(action_service=app.state.action_service)
    app.state.event_bus = EventBusService()
    app.state.event_service = EventService()
    app.state.metrics_db = MetricsDB()
    app.state.issue_autofix_service = IssueAutoFixService(app)
    app.state.event_store = EventStoreService(app.state.metrics_db)
    app.state.camera_event_service = CameraEventService(
        ha_proxy=app.state.ha_proxy,
        llm_service=app.state.llm_service,
        event_service=app.state.event_service,
    )
    app.state.open_loop_workflow_service = OpenLoopWorkflowService()
    app.state.open_loop_automation_service = OpenLoopAutomationService(
        app,
        workflow_service=app.state.open_loop_workflow_service,
    )
    imported_memories = app.state.metrics_db.import_memories_from(settings.shared_memory_db_path)
    app.state.memory_service = PersistentMemoryService(app.state.metrics_db)
    if imported_memories:
        logger.info(
            "persistent_memory.imported",
            source_db=settings.shared_memory_db_path,
            count=imported_memories,
        )
    app.state.motion_clip_service = MotionClipService(
        db=app.state.metrics_db,
        ha_proxy=app.state.ha_proxy,
        llm_service=app.state.llm_service,
        issue_autofix_service=app.state.issue_autofix_service,
        clip_duration_s=settings.motion_clip_duration_s,
        max_search_candidates=settings.motion_clip_search_candidates,
        max_search_results=settings.motion_clip_search_results,
    )

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
            AnnounceRequest(message=message, priority=priority, source="proactive"),
            fake_request,
        )

    proactive = ProactiveService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        ha_proxy=app.state.ha_proxy,
        llm_service=app.state.llm_service,
        motion_clip_service=app.state.motion_clip_service,
        announce_fn=_proactive_announce,
        system_prompt=system_prompt,
        event_service=app.state.event_service,
        camera_event_service=app.state.camera_event_service,
        issue_autofix_service=app.state.issue_autofix_service,
    )
    app.state.proactive_service = proactive

    # Auto-discover cameras and motion sensors from HA area registry
    from avatar_backend.services.camera_discovery import CameraDiscoveryService
    try:
        discovery = CameraDiscoveryService(settings.ha_url, settings.ha_token)
        discovery_result = await discovery.discover(timeout_s=15.0)
        if discovery_result.discovered:
            proactive.apply_discovery(discovery_result)
            app.state.camera_discovery = discovery_result
            logger.info(
                "camera_discovery.applied",
                outdoor_cameras=len(discovery_result.outdoor_cameras),
                motion_mappings=len(discovery_result.motion_camera_map),
            )
        else:
            logger.info("camera_discovery.skipped", detail="Using legacy + runtime config only")
    except Exception as exc:
        logger.warning("camera_discovery.startup_failed", exc=str(exc))

    await proactive.start()

    sensor_watch = SensorWatchService(
        ha_url=settings.ha_url,
        ha_token=settings.ha_token,
        ollama_url=settings.ollama_url,
        announce_fn=_proactive_announce,
        llm_service=app.state.llm_service,
        issue_autofix_service=app.state.issue_autofix_service,
    )
    app.state.sensor_watch = sensor_watch
    await sensor_watch.start()
    await app.state.open_loop_automation_service.start()
    kiosk_restart_task = asyncio.create_task(_restart_fully_kiosk_after_startup(app))

    cleanup_task = asyncio.create_task(
        _session_cleanup_loop(app.state.session_manager)
    )

    # Decision log — shared by proactive service and chat service
    decision_log = DecisionLog()
    app.state.decision_log = decision_log

    # Persistent metrics DB (LLM costs + system samples)
    metrics_db = app.state.metrics_db

    # System metrics poller — CPU/RAM/disk/GPU every 5 s
    sys_metrics = SystemMetrics(db=metrics_db, interval=5)
    app.state.sys_metrics = sys_metrics
    await sys_metrics.start()

    # Cost log — tracks token usage and cost per LLM call
    cost_log = CostLog()
    cost_log.set_db(metrics_db)
    app.state.cost_log = cost_log
    app.state.llm_service.set_cost_log(cost_log)
    decision_log.set_db(metrics_db)  # persist decisions across restarts

    # Log store — captures structlog output into DB + SSE for admin panel
    import logging as _logging
    log_store = LogStore()
    log_store.set_db(metrics_db)
    app.state.log_store = log_store
    _logging.getLogger().addHandler(log_store.make_handler())
    proactive = getattr(app.state, 'proactive_service', None)
    if proactive:
        proactive.set_decision_log(decision_log)
    sensor_watch = getattr(app.state, 'sensor_watch', None)
    if sensor_watch:
        sensor_watch.set_decision_log(decision_log)

    # Auto-detect Cloudflare tunnel URL on startup
    try:
        from avatar_backend.routers.admin import _read_tunnel_url, _update_env_value
        tunnel_url = None
        # Retry up to 15s — cloudflared may still be starting
        for _attempt in range(5):
            tunnel_url = await _read_tunnel_url()
            if tunnel_url:
                break
            await asyncio.sleep(3)
        if tunnel_url:
            current_public = settings.public_url or ""
            if current_public != tunnel_url:
                _update_env_value("PUBLIC_URL", tunnel_url)
                get_settings.cache_clear()
                logger.info("tunnel.auto_updated", old=current_public, new=tunnel_url)
            else:
                logger.info("tunnel.url_current", url=tunnel_url)
        else:
            logger.info("tunnel.not_detected", detail="No Cloudflare tunnel found — Alexa will use native TTS")
    except Exception as exc:
        logger.debug("tunnel.auto_detect_skipped", exc=str(exc))

    logger.info("avatar_backend.ready")
    yield

    kiosk_restart_task.cancel()
    cleanup_task.cancel()
    try:
        await kiosk_restart_task
    except asyncio.CancelledError:
        pass
    await proactive.stop()
    await app.state.sensor_watch.stop()
    await app.state.open_loop_automation_service.stop()
    await app.state.sys_metrics.stop()
    logger.info("avatar_backend.stopped")



# M1 security fix: add standard security headers to all responses.
# CSP is only applied to /admin paths — the avatar page loads Three.js and
# TalkingHead from cdn.jsdelivr.net via importmap and needs broad connect-src
# for the HA camera proxy, Google TTS, and WebSocket voice pipeline.
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next) -> StarletteResponse:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Only set restrictive CSP on admin pages — avatar/static need broad access
        path = request.url.path
        if path.startswith("/admin"):
            response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; "
                "media-src 'self' blob:; "
                "connect-src 'self' ws: wss: https:; "
                "frame-ancestors 'none'"
            )
        return response


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

    # M1 security fix: standard security headers on all responses
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(voice.router)
    app.include_router(avatar_ws.router)
    app.include_router(announce.router)
    app.include_router(admin.router)

    # Serve 3D avatar page and static assets
    _static_dir = static_dir()
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

        @app.get("/avatar", include_in_schema=False)
        async def avatar_page():
            return FileResponse(str(_static_dir / "avatar.html"))

    return app


app = create_app()
