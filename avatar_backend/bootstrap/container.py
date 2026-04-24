"""Typed container holding every service instance created during bootstrap."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from starlette.requests import HTTPConnection


def get_container(connection: HTTPConnection) -> "AppContainer":
    """FastAPI Depends() — extract the typed AppContainer from app.state.

    Accepts both HTTP requests and websocket connections.
    """
    return connection.app.state._container


@dataclass
class AppContainer:
    """Holds ALL services previously stored on ``app.state``.

    Accessed at runtime via ``app.state._container``.
    """

    # Phase 1 — core
    user_service: Any = None
    acl_manager: Any = None
    session_limiter: Any = None
    llm_service: Any = None
    session_manager: Any = None
    system_prompt: Any = None
    ha_proxy: Any = None
    presence_service: Any = None
    prompt_sync_service: Any = None

    # Phase 2 — services
    audio_cache: Dict[str, Any] = field(default_factory=dict)
    recent_event_contexts: Dict[str, Any] = field(default_factory=dict)
    stt_service: Any = None
    coral_wake_detector: Any = None
    tts_service: Any = None
    ws_manager: Any = None
    conversation_service: Any = None
    realtime_voice_adapter: Any = None
    realtime_voice_service: Any = None
    action_service: Any = None
    surface_state_service: Any = None
    event_bus: Any = None
    event_service: Any = None
    metrics_db: Any = None
    health_history_service: Any = None
    issue_autofix_service: Any = None
    event_store: Any = None
    camera_event_service: Any = None
    open_loop_workflow_service: Any = None
    open_loop_automation_service: Any = None
    memory_service: Any = None
    motion_clip_service: Any = None
    speaker_service: Any = None
    energy_service: Any = None
    deepface_service: Any = None
    music_service: Any = None
    blueiris_service: Any = None
    face_service: Any = None
    gemini_key_pool: Any = None
    scoreboard_service: Any = None

    # Phase 3 — background / monitoring
    ha_ws_manager: Any = None
    proactive_service: Any = None
    camera_discovery: Any = None
    sensor_watch: Any = None
    update_monitor: Any = None
    decision_log: Any = None
    sys_metrics: Any = None
    cost_log: Any = None
    log_store: Any = None

    # Runtime state
    motion_announce_cooldowns: Dict[str, float] = field(default_factory=dict)

    # Background asyncio.Task handles
    _background_tasks: List[Any] = field(default_factory=list)
