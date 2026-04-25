"""Mixin providing MDM, parental, and scoreboard tool methods for HAProxy."""
from __future__ import annotations
from typing import TYPE_CHECKING

from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services import mdm_client as _mdm
from avatar_backend.services._shared_http import _http_client

if TYPE_CHECKING:
    pass


class ParentalToolsMixin:
    """Parental controls, MDM, and scoreboard tool handlers.

    Mixed into HAProxy.  All methods access state via self (same instance).
    No extra constructor — HAProxy.__init__ owns all attributes.
    """

    def _check_homework_gate(self, args: dict) -> str:
        from datetime import datetime as _dt
        person_id = str(args.get("person_id") or "").strip().lower()
        fs = getattr(self, "_family_service", None)
        sb = getattr(self, "_scoreboard_service", None)
        if not fs or not person_id:
            return "Homework gate is not configured for this household."
        now = _dt.now()
        midnight_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        person = fs.get_person(person_id)
        if not person:
            return f"I don't have {person_id} in the household roster."
        state = fs.get_child_state(person_id)
        policies = fs.get_policies_for(person_id)
        hw_policies = [p for p in policies if p.rule_type == "requires_task_before_entertainment"]
        if not hw_policies:
            return f"{person.display_name} has no homework gate policy active."
        lines = [f"{person.display_name}'s homework gate status:"]
        lines.append(f"  Current state: {state.get('state', 'allowed')} — {state.get('reason', '')}")
        if sb:
            logs = sb.all_logs(days=1)
            done_today = {l["task_id"] for l in logs
                         if l["ts"] >= midnight_ts and l["person"] == person_id and l["points"] > 0}
            for pol in hw_policies:
                required = pol.required_task_ids or []
                if required:
                    done = [t for t in required if t in done_today]
                    pending = [t for t in required if t not in done_today]
                    lines.append(f"  Tasks done: {', '.join(done) or 'none'}")
                    lines.append(f"  Tasks pending: {', '.join(pending) or 'none'}")
                else:
                    lines.append(f"  Chores logged today: {len(done_today)}")
        return chr(10).join(lines)

    def _log_parental_action(self, tool: str, args: dict, result) -> None:
        """Fire-and-forget audit log for parental LLM tool calls."""
        try:
            db = getattr(self._container, "metrics_db", None)
            if db is None:
                return
            success = getattr(result, "success", True)
            msg = getattr(result, "message", str(result))
            db.log_parental_tool(tool=tool, args=args, success=bool(success), message=msg)
        except Exception:
            pass

    async def _get_enrolled_devices(self) -> "ToolResult":
        try:
            devices = await _mdm.get_devices()
            if not devices:
                return ToolResult(success=True, message="No devices enrolled.")
            lines = []
            for d in devices:
                number = d.get("number", "?")
                name = d.get("description") or d.get("name") or d.get("model") or "Unknown"
                is_online = d.get("online")
                last_upd_raw = d.get("lastUpdate")
                if not is_online and last_upd_raw:
                    # If flag is missing or false, check if update was in last 10 mins
                    if abs((time.time() * 1000) - last_upd_raw) < 600000:
                        is_online = True
                online = "online" if is_online else "offline"
                raw_upd = d.get("lastUpdate")
                if raw_upd and isinstance(raw_upd, int):
                    try: last_seen = datetime.fromtimestamp(raw_upd / 1000).strftime("%Y-%m-%d")
                    except Exception: last_seen = str(raw_upd)[:10]
                else:
                    last_seen = str(raw_upd or "never")[:10]
                info = d.get("info") or {}
                phone = info.get("phone", "")
                model = info.get("model", "")
                battery = info.get("batteryLevel", "")
                extra = []
                if model: extra.append(model)
                if phone: extra.append(f"phone: {phone}")
                if battery: extra.append(f"battery: {battery}%")
                suffix = " | ".join(extra)
                lines.append(f"{number}: {name} — {online} (last seen {last_seen}) [{suffix}]")
            return ToolResult(success=True, message=chr(10).join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_parental_status(self) -> "ToolResult":
        try:
            status = await _mdm.get_parental_status()
            reachable = "reachable" if status.get("hmdm_reachable") else "unreachable"
            url = status.get("url") or ""
            msg = f"Headwind parental backend is {reachable}."
            if url:
                msg += f" URL: {url}"
            return ToolResult(success=True, message=msg)
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _simulate_household_at(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt
        time_str = str(args.get("time") or "").strip()
        day_str = str(args.get("day") or "").strip().lower()
        if not time_str:
            return ToolResult(success=False, message="time is required (HH:MM).")
        try:
            sim_h, sim_m = (int(x) for x in time_str.split(":"))
        except Exception:
            return ToolResult(success=False, message=f"Invalid time {time_str!r}.")
        if not day_str:
            day_str = _dt.now().strftime("%A").lower()
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        hm = (sim_h, sim_m)
        lines = []
        for person in fs.get_children():
            school_nights = [s.lower() for s in (person.school_nights or [])]
            is_school = day_str in school_nights
            bedtime_str = person.bedtime_weekday if is_school else person.bedtime_weekend
            night_type = "school night" if is_school else "weekend"
            parts = []
            if bedtime_str:
                bt_h, bt_m = (int(x) for x in bedtime_str.split(":"))
                if hm >= (bt_h, bt_m):
                    parts.append(f"past bedtime ({bedtime_str}, {night_type}) — device LOCKED")
                else:
                    mins = (bt_h * 60 + bt_m) - (sim_h * 60 + sim_m)
                    h, m = divmod(mins, 60)
                    t = f"{h}h {m}m" if h else f"{m}m"
                    parts.append(f"bedtime {bedtime_str} in {t} ({night_type})")
            else:
                parts.append(f"no bedtime for {night_type}")
            for pol in fs.get_homework_gate_policies():
                if pol.subject_id != person.id or not pol.active:
                    continue
                if pol.enforce_from and pol.enforce_until:
                    fh, fm = (int(x) for x in pol.enforce_from.split(":"))
                    uh, um = (int(x) for x in pol.enforce_until.split(":"))
                    in_w = (fh, fm) <= hm < (uh, um)
                    tasks = ", ".join(pol.required_task_ids) or "assigned tasks"
                    if in_w:
                        parts.append(f"homework gate ACTIVE until {pol.enforce_until} (needs: {tasks})")
                    elif hm < (fh, fm):
                        mins2 = (fh * 60 + fm) - (sim_h * 60 + sim_m)
                        h2, m2 = divmod(mins2, 60)
                        t2 = f"{h2}h {m2}m" if h2 else f"{m2}m"
                        parts.append(f"homework gate opens in {t2} (at {pol.enforce_from})")
                    else:
                        parts.append("homework gate closed")
            lines.append(f"{person.display_name}: " + "; ".join(parts))
        if not lines:
            return ToolResult(success=True, message="No children in family model.")
        return ToolResult(success=True, message=
            f"Simulation for {day_str.title()} at {time_str}:\n" +
            "\n".join(f"  {chr(8226)} {l}" for l in lines))

    async def _get_household_forecast(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt, timedelta as _td
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        now = _dt.now()
        day = now.strftime("%A").lower()
        hm = (now.hour, now.minute)
        lines = []

        for person in fs.get_children():
            school_nights = [s.lower() for s in (person.school_nights or [])]
            is_school = day in school_nights
            bedtime_str = person.bedtime_weekday if is_school else person.bedtime_weekend
            night_type = "school night" if is_school else "weekend"
            state = fs.get_child_state(person.id)
            state_name = state.get("state", "allowed")
            state_reason = state.get("reason", "")

            entry = f"{person.display_name} [{state_name}]"
            if state_reason:
                entry += f" ({state_reason})"

            if bedtime_str:
                bt_h, bt_m = (int(x) for x in bedtime_str.split(":"))
                if hm < (bt_h, bt_m):
                    mins_left = (bt_h * 60 + bt_m) - (now.hour * 60 + now.minute)
                    h, m = divmod(mins_left, 60)
                    time_str = f"{h}h {m}m" if h else f"{m}m"
                    entry += f" — bedtime {bedtime_str} in {time_str} ({night_type})"
                else:
                    entry += f" — past bedtime ({bedtime_str}, {night_type})"

            for pol in fs.get_homework_gate_policies():
                if pol.subject_id != person.id or not pol.active:
                    continue
                if pol.enforce_from and pol.enforce_until:
                    from_h, from_m = (int(x) for x in pol.enforce_from.split(":"))
                    until_h, until_m = (int(x) for x in pol.enforce_until.split(":"))
                    if hm < (from_h, from_m):
                        mins = (from_h * 60 + from_m) - (now.hour * 60 + now.minute)
                        h, m = divmod(mins, 60)
                        t = f"{h}h {m}m" if h else f"{m}m"
                        entry += f" — homework gate opens in {t}"
                    elif hm < (until_h, until_m):
                        entry += f" — homework gate active until {pol.enforce_until}"

            lines.append(entry)

        if not lines:
            return ToolResult(success=True, message="No children found in family model.")
        now_str = now.strftime("%H:%M")
        return ToolResult(success=True, message=f"Household forecast at {now_str}:\n" + "\n".join(f"• {l}" for l in lines))

    async def _get_bedtime_status(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt
        person_id = str(args.get("person_id") or "").strip().lower()
        if not person_id:
            return ToolResult(success=False, message="person_id is required.")
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        person = fs.get_person(person_id)
        if not person:
            return ToolResult(success=False, message=f"Unknown person '{person_id}'.")
        if person.role != "child":
            return ToolResult(success=True, message=f"{person.display_name} is a guardian — no bedtime enforced.")
        now = _dt.now()
        day = now.strftime("%A").lower()
        school_nights = [s.lower() for s in (person.school_nights or [])]
        is_school = day in school_nights
        bedtime = person.bedtime_weekday if is_school else person.bedtime_weekend
        night_type = "school night" if is_school else "weekend"
        state = fs.get_child_state(person_id)
        state_str = state.get("state", "allowed")
        state_reason = state.get("reason", "")
        if not bedtime:
            return ToolResult(success=True, message=f"{person.display_name} has no bedtime set for tonight ({night_type}).")
        msg = (f"{person.display_name}'s bedtime tonight is {bedtime} ({night_type}). "
               f"Current device state: {state_str}")
        if state_reason:
            msg += f" — {state_reason}"
        return ToolResult(success=True, message=msg + ".")

    async def _get_device_location(self, args: dict) -> "ToolResult":
        import httpx as _httpx
        person_id = str(args.get("person_id") or "").strip().lower()
        device_number = str(args.get("device_number") or "").strip()
        display_name = person_id or device_number

        # Resolve person_id → device_number via family_service
        if person_id and not device_number:
            fs = getattr(self, "_family_service", None)
            if fs:
                for res in fs.get_resources_for(person_id):
                    if res.kind == "mdm_device" and res.device_number:
                        device_number = res.device_number
                        person = fs.get_person(person_id)
                        if person:
                            display_name = person.display_name
                        break
        if not device_number:
            return ToolResult(success=False, message=f"No MDM device found for '{person_id or 'unknown'}'. Check family_state.json or provide device_number directly.")

        try:
            device = await _mdm.get_device(device_number)
            location = await _mdm.get_device_location(device_number)
            dev_name = device.get("description") or device.get("name") or device.get("model") or device_number
            if not location:
                return ToolResult(success=True, message=f"No recent location is available for {display_name} — their device ({device_number}) may have location disabled or has not checked in recently.")
            lat = location.get("lat")
            lon = location.get("lon")
            ts = str(location.get("ts") or "unknown time")[:16]

            # Reverse geocode via Nominatim
            address = None
            try:
                r = await _http_client().get(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={"lat": lat, "lon": lon, "format": "json", "zoom": 16},
                    headers={"User-Agent": "Nova-HomeAssistant/1.0"},
                    timeout=5.0,
                )
                if r.status_code == 200:
                        geo = r.json()
                        addr = geo.get("address", {})
                        parts = []
                        for key in ("road", "suburb", "city", "town", "village", "county"):
                            v = addr.get(key)
                            if v and v not in parts:
                                parts.append(v)
                        if parts:
                            address = ", ".join(parts[:3])
            except Exception:
                pass

            loc_str = address if address else f"{lat}, {lon}"
            return ToolResult(
                success=True,
                message=f"IMPORTANT: {display_name} (not anyone else) was last seen near {loc_str} at {ts}. Device: {dev_name}. Always refer to this person as {display_name}.",
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM location error: {exc}")

    async def _search_apps(self, args: dict) -> "ToolResult":
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, message="query is required.")
        try:
            apps = await _mdm.search_apps(query, limit=12)
            if not apps:
                return ToolResult(success=True, message=f"No apps found for '{query}'.")
            lines = []
            for app in apps:
                mode = "installable" if app.get("installable") else "allow only"
                lines.append(f"{app.get('name') or app.get('pkg')} — {app.get('pkg')} ({mode})")
            return ToolResult(success=True, message="\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _mdm_set_app(self, args: dict, action: int) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        package = str(args.get("package") or "").strip()
        if not device_number or not package:
            return ToolResult(success=False, message="device_number and package are required.")
        try:
            await _mdm.set_app_action(device_number, package, action)
            verb = "blocked" if action == 0 else "unblocked"
            return ToolResult(success=True, message=f"{package} {verb} on device {device_number}.")
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _deploy_app(self, args: dict) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        package = str(args.get("package") or "").strip()
        name = str(args.get("name") or "").strip()
        if not device_number or not package:
            return ToolResult(success=False, message="device_number and package are required.")
        try:
            result = await _mdm.deploy_app(device_number, package, preferred_name=name)
            app = result.get("application") or {}
            mode = result.get("result_mode") or "allow"
            app_name = app.get("name") or name or package
            if mode == "install":
                msg = f"{app_name} is marked for install on device {device_number}."
            else:
                msg = (
                    f"{app_name} is allow-only in Headwind, so Nova allowed it on device {device_number} "
                    f"but Headwind cannot silently install it."
                )
            return ToolResult(success=True, message=msg)
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _send_device_message(self, args: dict) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        message = str(args.get("message") or "").strip()
        if not device_number or not message:
            return ToolResult(success=False, message="device_number and message are required.")
        try:
            await _mdm.send_message(device_number, message)
            return ToolResult(success=True, message=f"Message sent to device {device_number}.")
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_parental_configurations(self) -> "ToolResult":
        try:
            configs = await _mdm.get_configurations()
            if not configs:
                return ToolResult(success=True, message="No parental configurations were found.")
            lines = []
            for cfg in configs:
                cfg_id = cfg.get("id", "?")
                name = cfg.get("name") or f"Configuration {cfg_id}"
                desc = str(cfg.get("description") or "").strip()
                lines.append(f"{cfg_id}: {name}" + (f" — {desc}" if desc else ""))
            return ToolResult(success=True, message="\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_enrollment_link(self, args: dict) -> "ToolResult":
        raw_config_id = args.get("config_id")
        if raw_config_id is None:
            return ToolResult(success=False, message="config_id is required.")
        try:
            config_id = int(raw_config_id)
            info = await _mdm.get_enrollment_info(config_id)
            return ToolResult(
                success=True,
                message=(
                    f"Enrollment link for {info.get('config_name') or config_id}: "
                    f"{info.get('enroll_url')} (QR key: {info.get('qr_key')})"
                ),
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _log_chore(self, args: dict) -> "ToolResult":
        svc = getattr(self, "_scoreboard_service", None)
        llm = getattr(self, "_llm_service", None)
        if svc is None:
            from avatar_backend.models.tool_result import ToolResult
            return ToolResult(success=False, message="Scoreboard service not available.")
        result = await svc.handle_log_chore(args, ha_proxy=self, llm_service=llm)
        from avatar_backend.models.tool_result import ToolResult
        return ToolResult(success=True, message=result)

    async def _get_scoreboard(self, args: dict) -> "ToolResult":
        from avatar_backend.models.tool_result import ToolResult
        svc = getattr(self, "_scoreboard_service", None)
        if svc is None:
            return ToolResult(success=False, message="Scoreboard service not available.")
        from datetime import datetime as _dt
        from collections import defaultdict
        period = str(args.get("period") or "week").strip().lower()
        lines = []
        try:
            if period == "today":
                midnight = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                logs = [l for l in svc.all_logs(days=1) if l["ts"] >= midnight]
                if not logs:
                    return ToolResult(success=True, message="No chores have been logged today yet.")
                by_person: dict = defaultdict(lambda: {"count": 0, "points": 0, "tasks": []})
                for log in logs:
                    p = log["person"].title()
                    by_person[p]["count"] += 1
                    by_person[p]["points"] += log["points"]
                    by_person[p]["tasks"].append(log["task_label"])
                for p, d in sorted(by_person.items(), key=lambda x: -x[1]["points"]):
                    lines.append(p + ": " + str(d["count"]) + " chore(s), " + str(d["points"]) + " pts - " + ", ".join(d["tasks"]))
                return ToolResult(success=True, message="Today's chores: " + "; ".join(lines))
            elif period == "week":
                scores = svc.weekly_scores()
                if not scores:
                    return ToolResult(success=True, message="No chores logged this week yet.")
                for i, s in enumerate(scores):
                    rank = ["1st", "2nd", "3rd"][i] if i < 3 else str(i+1) + "th"
                    lines.append(rank + ": " + s["person"].title() + " - " + str(s["points"]) + " pts (" + str(s["tasks"]) + " tasks)")
                return ToolResult(success=True, message="Weekly scoreboard: " + "; ".join(lines))
            else:
                logs = svc.recent_logs(10)
                if not logs:
                    return ToolResult(success=True, message="No chores logged recently.")
                for log in logs:
                    when = _dt.fromtimestamp(log["ts"]).strftime("%a %H:%M")
                    lines.append(log["person"].title() + ": " + log["task_label"] + " (+" + str(log["points"]) + "pts) at " + when)
                return ToolResult(success=True, message="Recent chores: " + "; ".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message="Error fetching scoreboard: " + str(exc))


