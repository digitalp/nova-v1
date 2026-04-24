"""Bootstrap — creates every service in dependency order and returns an AppContainer."""

from __future__ import annotations

import asyncio
import logging as _logging

import structlog
from fastapi import FastAPI

from avatar_backend.bootstrap.container import AppContainer
from avatar_backend.config import get_settings
from avatar_backend.models.acl import ACLManager
from avatar_backend.middleware.session_ratelimit import SessionRateLimiter
from avatar_backend.runtime_paths import config_dir
from avatar_backend.routers.announce import AnnounceRequest, announce_handler

_CONFIG_DIR = config_dir()


async def bootstrap(app: FastAPI, settings, system_prompt: str) -> AppContainer:
    """Create all services in dependency order and return a populated container."""
    logger = structlog.get_logger()
    c = AppContainer()

    # ═══════════════════════════════════════════════════════════════════
    # Phase 1 — Core
    # ═══════════════════════════════════════════════════════════════════
    from avatar_backend.services.user_service import UserService
    c.user_service = UserService(_CONFIG_DIR / "users.json")
    if not c.user_service.has_users():
        logger.warning(
            "avatar_backend.no_users",
            detail="No admin users found. Visit /admin/login to create the first account.",
        )

    c.acl_manager = ACLManager.from_yaml_safe(str(_CONFIG_DIR / "acl.yaml"))
    c.session_limiter = SessionRateLimiter(
        max_requests=settings.session_rate_limit_max,
        window_s=settings.session_rate_limit_window_s,
    )

    from avatar_backend.services.llm_service import LLMService
    c.llm_service = LLMService()

    # Gemini API key pool for vision rotation
    from avatar_backend.services.gemini_key_pool import GeminiKeyPool, load_pool_from_settings
    from pathlib import Path as _Path
    c.gemini_key_pool = GeminiKeyPool()
    load_pool_from_settings(c.gemini_key_pool, settings)
    _pool_state_path = _Path("/opt/avatar-server/data/gemini_pool_state.json")
    c.gemini_key_pool.set_state_path(_pool_state_path)
    c.gemini_key_pool.load_state()
    if c.gemini_key_pool.size:
        logger.info("gemini_key_pool.configured", pool_size=c.gemini_key_pool.size)
    from avatar_backend.services.llm_service import set_gemini_key_pool
    set_gemini_key_pool(c.gemini_key_pool)

    from avatar_backend.services.session_manager import SessionManager
    c.session_manager = SessionManager(system_prompt)
    c.system_prompt = system_prompt

    from avatar_backend.services.ha_proxy import HAProxy
    c.ha_proxy = HAProxy(ha_url=settings.ha_url, ha_token=settings.ha_token, acl=c.acl_manager)

    from avatar_backend.services.presence_context import PresenceContextService
    c.presence_service = PresenceContextService(ha_url=settings.ha_url, ha_token=settings.ha_token)

    from avatar_backend.services.prompt_sync_service import PromptSyncService
    c.prompt_sync_service = PromptSyncService(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        llm_service=c.llm_service, prompt_file=_CONFIG_DIR / "system_prompt.txt", app=app,
    )
    c.prompt_sync_service.start()

    # ═══════════════════════════════════════════════════════════════════
    # Phase 2 — Services
    # ═══════════════════════════════════════════════════════════════════
    c.audio_cache = {}
    c.recent_event_contexts = {}

    from avatar_backend.services.stt_service import STTService
    c.stt_service = STTService(model_name=settings.whisper_model)

    from avatar_backend.routers.voice import _is_wake_word
    from avatar_backend.services.coral_wake_detector import CoralWakeDetector
    c.coral_wake_detector = CoralWakeDetector.build(c.stt_service, _is_wake_word)

    from avatar_backend.services.tts_service import create_tts_service, PiperTTSService
    from avatar_backend.services.tts_fallback import FallbackTTSService
    _primary_tts = create_tts_service(settings)
    # Only add Piper fallback if primary is NOT afrotts/piper (avoid double-loading)
    if settings.tts_provider.lower() == "piper":
        c.tts_service = FallbackTTSService(primary=_primary_tts, fallbacks=[])
        logger.info("tts_service.configured", provider=settings.tts_provider, fallback="none")
    else:
        c.tts_service = FallbackTTSService(primary=_primary_tts, fallbacks=[PiperTTSService(settings.piper_voice)])
        logger.info("tts_service.configured", provider=settings.tts_provider, fallback="piper")

    from avatar_backend.services.ws_manager import ConnectionManager
    c.ws_manager = ConnectionManager()

    from avatar_backend.services.conversation_service import ConversationService
    c.conversation_service = ConversationService(app)

    from avatar_backend.services.realtime_voice_service import RealtimeVoiceService, create_realtime_voice_adapter
    c.realtime_voice_adapter = create_realtime_voice_adapter(settings)
    c.realtime_voice_service = RealtimeVoiceService()

    from avatar_backend.services.action_service import ActionService
    c.action_service = ActionService()

    from avatar_backend.services.surface_state_service import SurfaceStateService
    c.surface_state_service = SurfaceStateService(action_service=c.action_service)

    from avatar_backend.services.event_bus import EventBusService
    c.event_bus = EventBusService()

    from avatar_backend.services.event_service import EventService
    c.event_service = EventService()

    from avatar_backend.services.metrics_db import MetricsDB
    c.metrics_db = MetricsDB()

    from avatar_backend.services.health_history import HealthHistoryService
    c.health_history_service = HealthHistoryService(c.metrics_db)

    from avatar_backend.services.issue_autofix_service import IssueAutoFixService
    c.issue_autofix_service = IssueAutoFixService(app)

    from avatar_backend.services.event_store import EventStoreService
    c.event_store = EventStoreService(c.metrics_db)

    from avatar_backend.services.camera_event_service import CameraEventService
    c.camera_event_service = CameraEventService(
        ha_proxy=c.ha_proxy, llm_service=c.llm_service, event_service=c.event_service,
    )

    # Face recognition via CodeProject.AI
    from avatar_backend.services.face_recognition import FaceRecognitionService
    c.face_service = FaceRecognitionService(cpai_url=settings.codeproject_ai_url)
    c.camera_event_service._face_service = c.face_service
    if settings.codeproject_ai_url:
        logger.info("face_recognition.configured", url=settings.codeproject_ai_url)

    from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService
    c.open_loop_workflow_service = OpenLoopWorkflowService()

    from avatar_backend.services.open_loop_automation_service import OpenLoopAutomationService
    c.open_loop_automation_service = OpenLoopAutomationService(app, workflow_service=c.open_loop_workflow_service)

    imported_memories = c.metrics_db.import_memories_from(settings.shared_memory_db_path)
    from avatar_backend.services.persistent_memory import PersistentMemoryService
    c.memory_service = PersistentMemoryService(c.metrics_db, ollama_url=settings.ollama_url)
    if imported_memories:
        logger.info("persistent_memory.imported", source_db=settings.shared_memory_db_path, count=imported_memories)

    from avatar_backend.services.motion_clip_service import MotionClipService
    c.motion_clip_service = MotionClipService(
        db=c.metrics_db, ha_proxy=c.ha_proxy, llm_service=c.llm_service,
        issue_autofix_service=c.issue_autofix_service,
        clip_duration_s=settings.motion_clip_duration_s,
        max_search_candidates=settings.motion_clip_search_candidates,
        max_search_results=settings.motion_clip_search_results,
        retention_days=settings.motion_clip_retention_days,
    )

    from avatar_backend.services.speaker_service import SpeakerService
    c.speaker_service = SpeakerService(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        speakers=settings.speaker_list, tts_engine=settings.tts_engine,
    )
    if settings.speaker_list:
        logger.info("speaker_service.configured", speakers=settings.speaker_list)
    else:
        logger.info("speaker_service.disabled")

    c.ha_proxy._llm_service = c.llm_service
    if settings.scoreboard_enabled:
        from avatar_backend.services.scoreboard_service import ScoreboardService
        c.scoreboard_service = ScoreboardService(
            db_path=_CONFIG_DIR / "scoreboard.db",
            config_path=_CONFIG_DIR / "scoreboard_config.json",
        )
        c.ha_proxy._scoreboard_service = c.scoreboard_service
        c.scoreboard_service._face_service = c.face_service

    # Family model (graceful if family_state.json absent)
    try:
        from avatar_backend.services.family_service import FamilyService
        from avatar_backend.runtime_paths import config_dir as _cfg_dir
        _family_path = _cfg_dir() / 'family_state.json'
        c.family_service = FamilyService(_family_path, c.metrics_db)
        if hasattr(c, 'ha_proxy') and c.ha_proxy:
            c.ha_proxy._family_service = c.family_service

    except Exception as _fe:
        structlog.get_logger().warning('family_service.init_failed', exc=str(_fe))
    else:
        logger.info("scoreboard.disabled")

    from avatar_backend.services.energy_service import EnergyService
    c.energy_service = EnergyService(ha_proxy=c.ha_proxy)
    # -- DeepFace --
    if settings.deepface_enabled:
        from avatar_backend.services.deepface_service import DeepFaceService
        c.deepface_service = DeepFaceService(deepface_home=settings.deepface_home)
        c.deepface_service._model_name = settings.deepface_model
        c.deepface_service._detector_backend = settings.deepface_detector
        c.deepface_service._actions = [a.strip() for a in settings.deepface_actions.split(',') if a.strip()]
        c.deepface_service._align = settings.deepface_align
        c.deepface_service._anti_spoofing = settings.deepface_anti_spoofing
        c.deepface_service._expand_percentage = settings.deepface_expand_percentage
        c.deepface_service._enforce_detection = settings.deepface_enforce_detection
        c.deepface_service._use_gpu = settings.deepface_use_gpu
        c.deepface_service._preprocess_training = settings.deepface_preprocess_training
        c.deepface_service.warmup()
        # Wire DeepFace as unknown-face pre-filter in the face recognition service
        if c.face_service:
            c.face_service._deepface_svc = c.deepface_service


    from avatar_backend.services.music_service import MusicService
    c.music_service = MusicService(ha_proxy=c.ha_proxy, music_assistant_url=settings.music_assistant_url)
    c.ha_proxy._music_service = c.music_service

    from avatar_backend.services.blueiris_service import BlueIrisService
    c.blueiris_service = BlueIrisService(bi_url=settings.blueiris_url, bi_user=settings.blueiris_user, bi_password=settings.blueiris_password)
    c.ha_proxy._blueiris_service = c.blueiris_service
    if settings.blueiris_url:
        logger.info("blueiris.configured", url=settings.blueiris_url)

    if settings.ha_url.startswith("http://"):
        logger.warning(
            "ha_proxy.insecure_url", ha_url=settings.ha_url,
            detail="HA_URL uses plain HTTP — credentials and data are sent unencrypted. "
                   "Set HA_URL to https:// in .env to enable TLS.",
        )

    if await c.ha_proxy.is_connected():
        logger.info("ha_proxy.connected", ha_url=settings.ha_url)
    else:
        logger.warning("ha_proxy.not_connected", ha_url=settings.ha_url)

    # Phase 2 complete — continue to Phase 3
    await _bootstrap_phase3(app, settings, system_prompt, c, logger)
    return c


async def _bootstrap_phase3(app, settings, system_prompt, c, logger):
    """Phase 3 — background services, monitoring, tasks."""

    async def _proactive_announce(
        message: str,
        priority: str = "normal",
        *,
        target_areas: list[str] | None = None,
        room_id: str | None = None,
    ) -> None:
        from types import SimpleNamespace
        fake_request = SimpleNamespace(app=app)
        await announce_handler(
            AnnounceRequest(
                message=message,
                priority=priority,
                source="proactive",
                target_areas=target_areas or [],
                room_id=room_id,
            ),
            fake_request,
            container=app.state._container,
        )

    from avatar_backend.services.ha_ws_manager import HAWebSocketManager
    c.ha_ws_manager = HAWebSocketManager(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        issue_autofix=c.issue_autofix_service,
    )
    c.ha_proxy.set_ws_manager(c.ha_ws_manager)
    c.presence_service._ha_ws_manager = c.ha_ws_manager

    from avatar_backend.services.proactive_service import ProactiveService
    c.proactive_service = ProactiveService(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        ha_proxy=c.ha_proxy, llm_service=c.llm_service,
        motion_clip_service=c.motion_clip_service, announce_fn=_proactive_announce,
        system_prompt=system_prompt, event_service=c.event_service,
        camera_event_service=c.camera_event_service,
        issue_autofix_service=c.issue_autofix_service, ha_ws_manager=c.ha_ws_manager,
    )

    from avatar_backend.services.camera_discovery import CameraDiscoveryService
    try:
        discovery = CameraDiscoveryService(settings.ha_url, settings.ha_token)
        discovery_result = await discovery.discover(timeout_s=15.0)
        if discovery_result.discovered:
            c.proactive_service.apply_discovery(discovery_result)
            c.camera_discovery = discovery_result
            logger.info(
                "camera_discovery.applied",
                outdoor_cameras=len(discovery_result.outdoor_cameras),
                motion_mappings=len(discovery_result.motion_camera_map),
            )
        else:
            logger.info("camera_discovery.skipped", detail="Using legacy + runtime config only")
    except Exception as exc:
        logger.warning("camera_discovery.startup_failed", exc=str(exc))

    await c.proactive_service.start()

    from avatar_backend.services.sensor_watch_service import SensorWatchService
    c.sensor_watch = SensorWatchService(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        ollama_url=settings.ollama_url, announce_fn=_proactive_announce,
        llm_service=c.llm_service, issue_autofix_service=c.issue_autofix_service,
        ha_ws_manager=c.ha_ws_manager,
    )
    await c.sensor_watch.start()

    from avatar_backend.services.ha_update_monitor import HAUpdateMonitor
    c.update_monitor = HAUpdateMonitor(
        ha_url=settings.ha_url, ha_token=settings.ha_token,
        announce_fn=_proactive_announce, ha_ws_manager=c.ha_ws_manager,
    )
    await c.update_monitor.start()

    await c.ha_ws_manager.start()
    await c.open_loop_automation_service.start()

    from avatar_backend.services.decision_log import DecisionLog
    c.decision_log = DecisionLog()
    c.decision_log.set_db(c.metrics_db)
    c.proactive_service.set_decision_log(c.decision_log)
    c.sensor_watch.set_decision_log(c.decision_log)

    from avatar_backend.services.system_metrics import SystemMetrics
    c.sys_metrics = SystemMetrics(db=c.metrics_db, interval=5)
    await c.sys_metrics.start()

    from avatar_backend.services.cost_log import CostLog
    c.cost_log = CostLog()
    c.cost_log.set_db(c.metrics_db)
    c.llm_service.set_cost_log(c.cost_log)

    from avatar_backend.services.log_store import LogStore
    c.log_store = LogStore()
    c.log_store.set_db(c.metrics_db)
    _logging.getLogger().addHandler(c.log_store.make_handler())

    # Stash announce fn so background tasks can use it
    c._proactive_announce = _proactive_announce

    # Background tasks
    from avatar_backend.bootstrap.background import schedule_background_tasks
    schedule_background_tasks(app, c)

    # Auto-detect Cloudflare quick-tunnel URL — skipped when a permanent domain is set.
    # A permanent domain is any PUBLIC_URL that is set and is NOT a trycloudflare.com URL.
    try:
        current_public = settings.public_url or ""
        _is_permanent = current_public and "trycloudflare.com" not in current_public
        if _is_permanent:
            logger.info("tunnel.permanent_url", url=current_public, detail="skipping quick-tunnel auto-detect")
        else:
            from avatar_backend.routers.admin import _read_tunnel_url, _update_env_value
            tunnel_url = None
            for _attempt in range(5):
                tunnel_url = await _read_tunnel_url()
                if tunnel_url:
                    break
                await asyncio.sleep(3)
            if tunnel_url:
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
