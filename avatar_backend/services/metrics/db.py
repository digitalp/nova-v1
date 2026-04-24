"""
MetricsDB — SQLite persistence for LLM cost and system metrics.

Tables:
  llm_invocations  — one row per LLM call (immutable)
  system_samples   — one row per metrics poll (CPU/RAM/disk/GPU) — kept 7 days
  long_term_memories — stable household memories Nova can reuse across restarts
  motion_clips     — archived motion-triggered video clips + AI descriptions
"""
from avatar_backend.services.metrics.base import MetricsDBBase
from avatar_backend.services.metrics.llm_costs import LLMCostsMixin
from avatar_backend.services.metrics.system_samples import SystemSamplesMixin
from avatar_backend.services.metrics.memories import MemoriesMixin
from avatar_backend.services.metrics.overrides import OverridesMixin
from avatar_backend.services.metrics.motion_clips import MotionClipsMixin
from avatar_backend.services.metrics.events import EventsMixin
from avatar_backend.services.metrics.logs import LogsMixin


class MetricsDB(MetricsDBBase, LLMCostsMixin, SystemSamplesMixin, MemoriesMixin, OverridesMixin, MotionClipsMixin, EventsMixin, LogsMixin):
    pass
