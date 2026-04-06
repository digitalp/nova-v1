"""
SystemMetrics — polls CPU/RAM/disk/GPU every 5 s, persists to MetricsDB,
and fan-outs live samples to SSE subscribers.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)

# Optional GPU support via pynvml
try:
    import pynvml  # type: ignore
    pynvml.nvmlInit()
    _GPU_AVAILABLE = True
except Exception:
    _GPU_AVAILABLE = False


def _collect_sample() -> dict:
    """Synchronous collection — runs in an executor thread."""
    import psutil

    cpu  = psutil.cpu_percent(interval=None)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    sample: dict[str, Any] = {
        "cpu_pct":   cpu,
        "ram_used":  ram.used,
        "ram_total": ram.total,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "gpu_util":       None,
        "gpu_mem_used":   None,
        "gpu_mem_total":  None,
        "ollama_gpu_pct": None,
    }

    if _GPU_AVAILABLE:
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
            sample["gpu_util"]      = float(util.gpu)
            sample["gpu_mem_used"]  = int(mem.used)
            sample["gpu_mem_total"] = int(mem.total)
            # ollama_gpu_pct: same GPU util since Ollama shares the card
            sample["ollama_gpu_pct"] = float(util.gpu)
        except Exception as exc:
            _log.debug("gpu_metrics.error: %s", exc)

    return sample


class SystemMetrics:
    """Background service — call start() to begin polling."""

    def __init__(self, db, interval: int = 5) -> None:
        self._db = db
        self._interval = interval
        self._latest: dict | None = None
        self._subscribers: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Public API ─────────────────────────────────────────────────────────

    def latest(self) -> dict | None:
        return self._latest

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    # ── Internal ───────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        purge_counter = 0
        while True:
            try:
                sample = await loop.run_in_executor(None, _collect_sample)
                self._latest = sample
                # persist
                try:
                    self._db.insert_sample(sample)
                except Exception as exc:
                    _log.warning("system_metrics.db_write_error: %s", exc)
                # broadcast
                dead: list[asyncio.Queue] = []
                for q in self._subscribers:
                    try:
                        q.put_nowait(sample)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    self._subscribers.remove(q)
                # purge old samples once every 10 min (120 × 5 s)
                purge_counter += 1
                if purge_counter >= 120:
                    purge_counter = 0
                    try:
                        deleted = self._db.purge_old_samples(keep_days=7)
                        if deleted:
                            _log.debug("system_metrics.purged %d old samples", deleted)
                    except Exception as exc:
                        _log.debug("system_metrics.purge_error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("system_metrics.poll_error: %s", exc)

            await asyncio.sleep(self._interval)
