"""
Admin panel — /admin

Session-based authentication (username + password) replaces the API key gate.
All browser sessions are tracked via an HTTP-only cookie (nova_session).

Roles
-----
admin  — full access: config, prompt, ACL, restart, user management
viewer — read-only: dashboard, logs, sessions
"""
from fastapi import APIRouter

from . import auth, config, motion, events, dashboard, monitoring, system, scoreboard, parental

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(auth.router)
router.include_router(config.router)
router.include_router(motion.router)
router.include_router(events.router)
router.include_router(dashboard.router)
router.include_router(monitoring.router)
router.include_router(system.router)
router.include_router(scoreboard.router)
router.include_router(parental.router)

# Re-exports for backward compatibility (tests, main.py, etc.)
from .common import (  # noqa: F401
    _update_env_value,
    _require_session,
    EventHistoryDomainActionBody,
    EventHistoryWorkflowRunBody,
)
from .motion import _serialize_motion_clip, _motion_clip_is_playable  # noqa: F401
from .events import (  # noqa: F401
    get_event_history,
    get_event_history_workflow_summary,
    get_event_history_workflow_status,
    run_event_history_workflow,
    run_event_history_domain_action,
)
from .system import _read_tunnel_url  # noqa: F401
