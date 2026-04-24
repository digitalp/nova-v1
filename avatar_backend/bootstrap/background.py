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
    from avatar_backend.services.home_runtime import load_home_runtime_config as _lhrc
    WINDOW_START = (15, 30)
    WINDOW_END   = (19, 30)
    last_announced: dict[str, float] = {}

    while True:
        try:
            now = _dt.now()
            h, m = now.hour, now.minute
            in_window = (h, m) >= WINDOW_START and (h, m) < WINDOW_END
            if in_window:
                _rt = _lhrc()
                KITCHEN_CAM = _rt.kitchen_watch_camera
                WATCH_TASKS = _rt.kitchen_watch_tasks
                # Fetch camera image once
                image_bytes = None
                try:
                    image_bytes = await ha_proxy.fetch_camera_image(KITCHEN_CAM)
                except Exception as exc:
                    logger.debug("kitchen_watch.camera_failed", exc=str(exc)[:80])

                if image_bytes:
                    prompt = (
                        "Look at this kitchen image. "
                        "Is the kitchen bin overflowing or visibly full? "
                        "Answer YES or NO only."
                    )
                    try:
                        description = await llm_service.describe_image(image_bytes, prompt=prompt)
                        desc_upper = description.upper()
                        bin_full  = "YES" in desc_upper
                        sink_full = False
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



async def _living_room_sweep_loop(announce_fn, scoreboard_service, blueiris_service,
                                   llm_service, ha_proxy, interval_m: int = 30) -> None:
    """Weekdays 15:00-20:00: PTZ sweep of living room, announce if messy."""
    from datetime import datetime as _dt
    import time as _time
    await asyncio.sleep(180)
    logger = structlog.get_logger()
    WINDOW_START  = (15, 0)
    WINDOW_END    = (20, 0)
    WEEKDAYS      = {0, 1, 2, 3, 4}  # Mon-Fri
    from avatar_backend.services.home_runtime import load_home_runtime_config as _lhrc_lr
    LR_TASK_ID    = "tidy_living_room"
    PTZ_PRESETS   = [0, 1, 2]
    COOLDOWN_S    = 7200
    last_announced: float = 0.0

    while True:
        try:
            now = _dt.now()
            if now.weekday() in WEEKDAYS:
                h, m = now.hour, now.minute
                in_window = (h, m) >= WINDOW_START and (h, m) < WINDOW_END
                if in_window and (_time.time() - last_announced) >= COOLDOWN_S:
                    task = scoreboard_service.get_task(LR_TASK_ID)
                    assigned = (task or {}).get("assigned_to") or []
                    members = await scoreboard_service.get_members()
                    check_members = assigned if assigned else members
                    already_done = any(
                        scoreboard_service.already_logged_today(LR_TASK_ID, m)
                        for m in check_members
                    )
                    if not already_done:
                        _rt_lr = _lhrc_lr()
                        LR_HA_CAM = _rt_lr.living_room_camera
                        LR_BI_CAM = _rt_lr.blueiris_camera_map.get(LR_HA_CAM, "sittingroom")
                        has_ptz = bool(getattr(blueiris_service, "_bi_user", ""))
                        positions = PTZ_PRESETS if has_ptz else [None]
                        untidy_details = []

                        for preset in positions:
                            # Move to PTZ preset if available
                            if preset is not None:
                                moved = await blueiris_service.ptz_preset(LR_BI_CAM, preset)
                                if moved:
                                    await asyncio.sleep(3)  # let camera settle

                            # Fetch snapshot: Blue Iris first, then HA
                            image_bytes = await blueiris_service.fetch_snapshot_by_name(LR_BI_CAM)
                            if not image_bytes:
                                try:
                                    image_bytes = await ha_proxy.fetch_camera_image(LR_HA_CAM)  # LR_HA_CAM set above
                                except Exception:
                                    pass

                            if not image_bytes:
                                continue

                            prompt = (
                                "Look carefully at this living room image. "
                                "Is the room tidy? Check: cushions straight on sofa, "
                                "floor clear of items, no obvious mess on tables or surfaces. "
                                "Answer YES if it looks reasonably tidy, or NO followed by a brief "
                                "description of what needs tidying (one sentence, max 20 words)."
                            )
                            try:
                                result = await llm_service.describe_image(image_bytes, prompt=prompt)
                                logger.debug("lr_sweep.position_result", preset=preset, result=result[:120])
                                if result.upper().startswith("NO"):
                                    detail = result[2:].lstrip(":- ").strip()
                                    untidy_details.append(detail)
                            except Exception as exc:
                                logger.debug("lr_sweep.llm_failed", preset=preset, exc=str(exc)[:80])

                        if untidy_details:
                            detail = untidy_details[0]
                            if assigned:
                                names = " and ".join(m.title() for m in assigned)
                                msg = f"Hey {names}, the living room needs tidying — {detail}"
                            else:
                                msg = f"Can someone tidy the living room? {detail}"
                            last_announced = _time.time()
                            await announce_fn(msg, "normal")
                            logger.info("lr_sweep.announced", positions_checked=len(positions),
                                        untidy_count=len(untidy_details))
        except Exception as exc:
            structlog.get_logger().warning("lr_sweep.error", exc=str(exc)[:120])
        await asyncio.sleep(interval_m * 60)




async def _blind_check_loop(announce_fn, blueiris_service, llm_service, ha_proxy) -> None:
    """At 20:00 check living room blinds; re-remind every 5 min until closed or 21:00."""
    from datetime import datetime as _dt
    import time as _time
    await asyncio.sleep(60)
    logger = structlog.get_logger()
    from avatar_backend.services.home_runtime import load_home_runtime_config as _lhrc_bl
    CHECK_START = 20
    CHECK_END   = 21
    INTERVAL_S  = 300
    last_check_date: str = ""
    active: bool = False
    last_reminder: float = 0.0
    _rt_bl_init = _lhrc_bl()
    LR_HA_CAM = _rt_bl_init.blind_check_camera
    LR_BI_CAM = _rt_bl_init.blueiris_camera_map.get(LR_HA_CAM, "sittingroom")
    NAMES = _rt_bl_init.blind_reminder_names

    while True:
        try:
            now = _dt.now()
            date_str = now.strftime("%Y-%m-%d")

            # Arm at 20:00
            if now.hour == CHECK_START and now.minute == 0 and last_check_date != date_str:
                active = True
                last_check_date = date_str
                last_reminder = 0.0
                logger.info("blind_check.armed", date=date_str)
                _rt_bl = _lhrc_bl()
                LR_HA_CAM = _rt_bl.blind_check_camera
                LR_BI_CAM = _rt_bl.blueiris_camera_map.get(LR_HA_CAM, "sittingroom")
                NAMES = _rt_bl.blind_reminder_names

            if active and now.hour >= CHECK_END:
                active = False
                logger.info("blind_check.expired", date=date_str)

            if active and (_time.time() - last_reminder) >= INTERVAL_S:
                # Fetch snapshot
                image_bytes = None
                bi_url = getattr(blueiris_service, "_bi_url", "")
                if bi_url:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=6.0) as client:
                            r = await client.get(f"{bi_url}/image/{LR_BI_CAM}?q=70")
                            if r.status_code == 200 and len(r.content) > 2000:
                                image_bytes = r.content
                    except Exception as exc:
                        logger.debug("blind_check.bi_failed", exc=str(exc)[:80])

                if not image_bytes:
                    try:
                        image_bytes = await ha_proxy.fetch_camera_image(LR_HA_CAM)
                    except Exception as exc:
                        logger.debug("blind_check.ha_failed", exc=str(exc)[:80])

                if image_bytes:
                    prompt = (
                        "Look at this living room image. "
                        "Are the window blinds or curtains fully closed? "
                        "Answer YES if they are completely closed and no window frame or outside light is visible. "
                        "Answer NO if the blinds are open or partially open and you can see the window frame or outside."
                    )
                    try:
                        result = await llm_service.describe_image(image_bytes, prompt=prompt)
                        logger.info("blind_check.result", result=result[:100])
                        if result.upper().startswith("YES"):
                            active = False
                            logger.info("blind_check.blinds_closed")
                        else:
                            msg = (
                                "Hey " + NAMES + " — can someone please close the living room blinds?"
                            )
                            last_reminder = _time.time()
                            await announce_fn(msg, "normal")
                            logger.info("blind_check.reminded")
                    except Exception as exc:
                        logger.debug("blind_check.llm_failed", exc=str(exc)[:80])
                else:
                    logger.debug("blind_check.no_image")

        except Exception as exc:
            structlog.get_logger().warning("blind_check.error", exc=str(exc)[:120])
        await asyncio.sleep(30)







async def _morning_digest_loop(announce_fn, scoreboard_service, llm_service) -> None:
    """Every day at 07:30 announce a morning digest: yesterday recap + today outlook."""
    from datetime import datetime as _dt, timedelta as _td
    from collections import defaultdict
    from pathlib import Path
    from avatar_backend.runtime_paths import config_dir
    await asyncio.sleep(90)
    logger = structlog.get_logger()
    last_fired_date: str = ""
    _NL = chr(10)

    while True:
        try:
            now = _dt.now()
            date_str = now.strftime("%Y-%m-%d")
            if now.hour == 7 and now.minute == 30 and last_fired_date != date_str:
                last_fired_date = date_str
                yesterday = now - _td(days=1)
                midnight_yesterday = yesterday.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                midnight_today = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()

                logs = scoreboard_service.all_logs(days=2)
                yest_logs = [l for l in logs if midnight_yesterday <= l["ts"] < midnight_today]
                by_person: dict = defaultdict(lambda: {"net": 0, "chores": [], "penalties": []})
                for log in yest_logs:
                    p = log["person"].title()
                    pts = log["points"]
                    by_person[p]["net"] += pts
                    if pts >= 0:
                        by_person[p]["chores"].append(log["task_label"])
                    else:
                        by_person[p]["penalties"].append(log["task_label"])

                day_name = now.strftime("%A").lower()
                cfg = scoreboard_service.get_config()
                today_tasks: list[str] = []
                for task in cfg.get("tasks", []):
                    for reminder in task.get("reminders", []):
                        r_day = reminder.get("day", "")
                        if not r_day or r_day == day_name:
                            today_tasks.append(task.get("label", task.get("id", "")))
                            break

                if not by_person:
                    yest_block = "No activity was logged yesterday."
                else:
                    lines = []
                    for person, d in sorted(by_person.items(), key=lambda x: -x[1]["net"]):
                        chores = ", ".join(d["chores"]) or "none"
                        pens   = ", ".join(d["penalties"]) or "none"
                        lines.append(
                            f"{person}: chores={chores}; penalties={pens}; net={d['net']:+d} pts"
                        )
                    yest_block = "YESTERDAY'S SUMMARY:" + _NL + _NL.join(lines)

                today_block = (
                    "TODAY'S SCHEDULED TASKS: "
                    + (", ".join(today_tasks) if today_tasks else "none scheduled")
                )
                data_block = yest_block + _NL + _NL + today_block

                sys_prompt_path = Path(config_dir()) / "system_prompt.txt"
                try:
                    system_prompt = sys_prompt_path.read_text()
                except Exception:
                    system_prompt = "You are Nova, a home assistant."

                user_msg = (
                    "It is 7:30 AM. Give the morning household digest as a brief, "
                    "warm spoken announcement (2-4 sentences). Recap yesterday's "
                    "highlights for each child (praise good behaviour, acknowledge "
                    "any penalties constructively in one short sentence), then mention "
                    "what tasks are on today so the family is prepared. Keep it "
                    "upbeat and energising. Plain spoken English only, no markdown."
                    + _NL + _NL + data_block
                )

                try:
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ]
                    msg, _ = await llm_service.chat(messages, use_tools=False)
                except Exception as exc:
                    logger.warning("morning_digest.llm_failed", exc=str(exc)[:80])
                    if by_person:
                        msg = "Good morning! Yesterday: " + "; ".join(
                            f"{p}: net {d['net']:+d} points" for p, d in by_person.items()
                        ) + ". Have a great day!"
                    else:
                        msg = "Good morning everyone! No activity logged yesterday. Let us make today count!"

                await announce_fn(msg, "normal")
                logger.info("morning_digest.announced",
                            persons=list(by_person.keys()), today_tasks=today_tasks)
        except Exception as exc:
            structlog.get_logger().warning("morning_digest.error", exc=str(exc)[:120])
        await asyncio.sleep(30)


async def _daily_chore_summary_loop(announce_fn, scoreboard_service, llm_service) -> None:
    """Every day at 20:00 announce a behaviour summary: chores earned + penalties issued."""
    from datetime import datetime as _dt
    from collections import defaultdict
    from pathlib import Path
    from avatar_backend.runtime_paths import config_dir
    await asyncio.sleep(60)
    logger = structlog.get_logger()
    last_fired_date: str = ""

    while True:
        try:
            now = _dt.now()
            date_str = now.strftime("%Y-%m-%d")
            if now.hour == 20 and now.minute == 0 and last_fired_date != date_str:
                last_fired_date = date_str
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

                logs = scoreboard_service.all_logs(days=1)
                today_logs = [l for l in logs if l["ts"] >= midnight]

                by_person: dict = defaultdict(lambda: {"chores": [], "penalties": [], "net": 0})
                for log in today_logs:
                    p = log["person"].title()
                    pts = log["points"]
                    by_person[p]["net"] += pts
                    if pts >= 0:
                        by_person[p]["chores"].append(f"{log['task_label']} (+{pts})")
                    else:
                        by_person[p]["penalties"].append(f"{log['task_label']} ({pts})")

                weekly = scoreboard_service.weekly_scores()

                if not by_person:
                    data_block = "No chores or penalties were logged today."
                else:
                    lines = []
                    for person, d in sorted(by_person.items(), key=lambda x: -x[1]["net"]):
                        good = ", ".join(d["chores"]) or "none"
                        bad  = ", ".join(d["penalties"]) or "none"
                        lines.append(
                            f"{person}: chores={good}; penalties={bad}; net={d['net']:+d} pts"
                        )
                    weekly_lines = [
                        f"{s['person'].title()} {s['points']} pts" for s in weekly
                    ]
                    data_block = (
                        "TODAY'S BEHAVIOUR SUMMARY\n" +
                        "\n".join(lines) +
                        "\n\nWEEKLY STANDINGS: " + ", ".join(weekly_lines)
                    )

                sys_prompt_path = Path(config_dir()) / "system_prompt.txt"
                try:
                    system_prompt = sys_prompt_path.read_text()
                except Exception:
                    system_prompt = "You are Nova, a home assistant."

                user_msg = (
                    "It is 8 PM. Give the daily behaviour summary for the family as a "
                    "warm, natural spoken announcement (3-5 sentences). Cover both good "
                    "behaviour (chores, points earned) and any bad behaviour (penalties "
                    "issued) honestly but constructively. If a child had penalties, "
                    "mention it directly but encourage improvement. If everyone was good, "
                    "celebrate it. Plain spoken sentences only, no markdown or lists.\n\n"
                    + data_block
                )

                try:
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ]
                    msg, _ = await llm_service.chat(messages, use_tools=False)
                except Exception as exc:
                    logger.warning("daily_summary.llm_failed", exc=str(exc)[:80])
                    if by_person:
                        msg = "Daily update: " + "; ".join(
                            f"{p}: net {d['net']:+d} points" for p, d in by_person.items()
                        ) + ". Keep up the good work!"
                    else:
                        msg = "No activity was logged today. Let us aim for a great day tomorrow!"

                await announce_fn(msg, "normal")
                logger.info("daily_summary.announced", persons=list(by_person.keys()))
        except Exception as exc:
            structlog.get_logger().warning("daily_summary.error", exc=str(exc)[:120])
        await asyncio.sleep(30)

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
        container._background_tasks.append(asyncio.create_task(
            _daily_chore_summary_loop(
                container._proactive_announce,
                container.scoreboard_service,
                container.llm_service,
            ),
            name="daily_behaviour_summary",
        ))
        container._background_tasks.append(asyncio.create_task(
            _morning_digest_loop(
                container._proactive_announce,
                container.scoreboard_service,
                container.llm_service,
            ),
            name="morning_digest",
        ))
