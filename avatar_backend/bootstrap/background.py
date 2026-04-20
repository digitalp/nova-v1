"""Background tasks — helper coroutines and scheduling."""

from __future__ import annotations

import asyncio

import structlog
from fastapi import FastAPI


async def _session_cleanup_loop(sm, interval: int = 300) -> None:
    while True:
        await asyncio.sleep(interval)
        await sm.cleanup_expired()


async def _clip_cleanup_loop(svc, interval_h: int = 24) -> None:
    await asyncio.sleep(300)
    while True:
        try:
            await svc.run_cleanup()
        except Exception as exc:
            structlog.get_logger().warning("clip_cleanup.error", exc=str(exc))
        await asyncio.sleep(interval_h * 3600)


async def _audit_cleanup_loop(db, interval_h: int = 24) -> None:
    await asyncio.sleep(600)
    while True:
        try:
            db.cleanup_old_audits(retention_days=30)
        except Exception:
            pass
        await asyncio.sleep(interval_h * 3600)


async def _backfill_thumbs(motion_clip_service) -> None:
    await asyncio.sleep(30)
    try:
        result = await motion_clip_service.backfill_thumbnails()
        if result.get("generated", 0) > 0:
            structlog.get_logger().info("thumbnail_backfill.done", **result)
    except Exception as exc:
        structlog.get_logger().debug("thumbnail_backfill.skipped", exc=str(exc))


async def _restart_fully_kiosk_after_startup(app: FastAPI, delay_s: float = 5.0) -> None:
    await asyncio.sleep(delay_s)
    logger = structlog.get_logger()
    ws_mgr = getattr(app.state, "ws_manager", None)
    ha = getattr(app.state, "ha_proxy", None)
    from avatar_backend.services.home_runtime import load_home_runtime_config
    _rt = load_home_runtime_config()
    kiosk_entity = getattr(_rt, "kiosk_restart_entity", "") or ""
    if ha is not None and kiosk_entity:
        try:
            domain, _ = kiosk_entity.split(".", 1)
            result = await ha.call_service(domain, "press", kiosk_entity)
        except Exception as exc:
            logger.warning("avatar_backend.kiosk_restart_failed", entity_id=kiosk_entity, error=str(exc))
        else:
            if result.success:
                logger.info("avatar_backend.kiosk_restart_requested", entity_id=kiosk_entity)
    if ws_mgr is not None:
        payload = {"type": "server_restarted"}
        await ws_mgr.broadcast_json(payload)
        await ws_mgr.broadcast_to_voice_json(payload)
        logger.info("avatar_backend.restart_signal_broadcast")



async def _chore_reminder_loop(announce_fn, scoreboard_service) -> None:
    """Check reminder schedule every minute and announce unlogged chores."""
    from datetime import datetime as _dt
    await asyncio.sleep(90)
    last_fired: set[str] = set()
    logger = structlog.get_logger()
    while True:
        try:
            now = _dt.now()
            time_str = now.strftime("%H:%M")
            day_str = now.strftime("%A").lower()
            date_str = now.strftime("%Y-%m-%d")
            cfg = scoreboard_service.get_config()
            members = await scoreboard_service.get_members()
            for task in cfg.get("tasks", []):
                for reminder in task.get("reminders", []):
                    if reminder.get("time") != time_str:
                        continue
                    if reminder.get("day") and reminder["day"] != day_str:
                        continue
                    fire_key = f"{task['id']}:{date_str}:{time_str}"
                    if fire_key in last_fired:
                        continue
                    already_done = any(
                        scoreboard_service.already_logged_today(task["id"], m)
                        for m in members
                    )
                    if not already_done:
                        last_fired.add(fire_key)
                        await announce_fn(reminder["message"], "normal")
                        logger.info("chore_reminder.fired", task=task["id"], time=time_str)
        except Exception as exc:
            structlog.get_logger().warning("chore_reminder.error", exc=str(exc)[:120])
        await asyncio.sleep(60)


async def _kitchen_watch_loop(announce_fn, scoreboard_service, ha_proxy, llm_service,
                               interval_m: int = 20) -> None:
    """Between 15:30-19:30 check the kitchen camera for overflowing bin / sink full of dishes."""
    from datetime import datetime as _dt
    await asyncio.sleep(120)  # let services settle
    logger = structlog.get_logger()
    WINDOW_START = (15, 30)
    WINDOW_END   = (19, 30)
    KITCHEN_CAM  = "camera.tangu_home_kitchen"
    # task_id -> min cooldown between announcements (seconds)
    WATCH_TASKS  = {
        "empty_kitchen_bin": 7200,   # 2h between reminders
        "load_dishwasher":   7200,
    }
    last_announced: dict[str, float] = {}

    while True:
        try:
            now = _dt.now()
            h, m = now.hour, now.minute
            in_window = (h, m) >= WINDOW_START and (h, m) < WINDOW_END
            if in_window:
                # Fetch camera image once
                image_bytes = None
                try:
                    image_bytes = await ha_proxy.fetch_camera_image(KITCHEN_CAM)
                except Exception as exc:
                    logger.debug("kitchen_watch.camera_failed", exc=str(exc)[:80])

                if image_bytes:
                    prompt = (
                        "Look carefully at this kitchen image and answer TWO questions: " "1. BIN: Is the kitchen bin overflowing or visibly full? Answer YES or NO. " "2. SINK: Is the sink noticeably full of dishes or cluttered? Answer YES or NO. " "Reply in exactly this format - BIN: YES/NO SINK: YES/NO"
                    )
                    try:
                        description = await llm_service.describe_image(image_bytes, prompt=prompt)
                        desc_upper = description.upper()
                        bin_full  = "BIN: YES"  in desc_upper
                        sink_full = "SINK: YES" in desc_upper
                        logger.debug("kitchen_watch.result", bin_full=bin_full, sink_full=sink_full, desc=description[:120])
                    except Exception as exc:
                        logger.debug("kitchen_watch.llm_failed", exc=str(exc)[:80])
                        bin_full = sink_full = False

                    cfg = scoreboard_service.get_config()
                    members = await scoreboard_service.get_members()
                    date_str = now.strftime("%Y-%m-%d")
                    import time as _time

                    issues = []
                    if bin_full:
                        issues.append(("empty_kitchen_bin", "the kitchen bin is overflowing"))
                    if sink_full:
                        issues.append(("load_dishwasher", "the sink is full of dishes"))

                    for task_id, description_text in issues:
                        # Skip if cooldown active
                        cooldown = WATCH_TASKS.get(task_id, 7200)
                        last = last_announced.get(task_id, 0)
                        if _time.time() - last < cooldown:
                            continue
                        # Skip if already logged today by any assigned member
                        task = scoreboard_service.get_task(task_id)
                        assigned = (task or {}).get("assigned_to") or []
                        check_members = assigned if assigned else members
                        already_done = any(
                            scoreboard_service.already_logged_today(task_id, m)
                            for m in check_members
                        )
                        if already_done:
                            continue
                        # Build announcement
                        if assigned:
                            names = " and ".join(m.title() for m in assigned)
                            msg = f"Hey {names}, {description_text} — please sort it out soon!"
                        else:
                            msg = f"Heads up everyone, {description_text} — can someone take care of it?"
                        last_announced[task_id] = _time.time()
                        await announce_fn(msg, "normal")
                        logger.info("kitchen_watch.announced", task=task_id, bin_full=bin_full, sink_full=sink_full)
        except Exception as exc:
            structlog.get_logger().warning("kitchen_watch.error", exc=str(exc)[:120])
        await asyncio.sleep(interval_m * 60)

def schedule_background_tasks(app: FastAPI, container) -> None:
    """Schedule all background asyncio tasks. Called after service creation."""
    container._background_tasks.append(asyncio.create_task(_restart_fully_kiosk_after_startup(app), name="kiosk_restart"))
    container._background_tasks.append(asyncio.create_task(_session_cleanup_loop(container.session_manager), name="session_cleanup"))
    container._background_tasks.append(asyncio.create_task(_clip_cleanup_loop(container.motion_clip_service), name="clip_cleanup"))
    container._background_tasks.append(asyncio.create_task(_audit_cleanup_loop(container.metrics_db), name="audit_cleanup"))
    container._background_tasks.append(asyncio.create_task(_backfill_thumbs(container.motion_clip_service), name="thumb_backfill"))
    if getattr(container, 'scoreboard_service', None) is not None:
        container._background_tasks.append(asyncio.create_task(
            _chore_reminder_loop(container._proactive_announce, container.scoreboard_service),
            name="chore_reminders",
        ))
    if (getattr(container, 'scoreboard_service', None) is not None
            and getattr(container, 'ha_proxy', None) is not None
            and getattr(container, 'llm_service', None) is not None):
        container._background_tasks.append(asyncio.create_task(
            _kitchen_watch_loop(
                container._proactive_announce,
                container.scoreboard_service,
                container.ha_proxy,
                container.llm_service,
            ),
            name="kitchen_watch",
        ))
