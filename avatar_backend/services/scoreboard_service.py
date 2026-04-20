"""Chore scoreboard — tracks points per person, weekly leaderboard, task config."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

_LOGGER = structlog.get_logger()

_DEFAULT_CONFIG: dict[str, Any] = {
    "members": ["penn", "tangu"],
    "show_widget": True,
    "week_start": "monday",
    "tasks": [
        {
            "id": "morning_prayer", "label": "Morning Prayer", "points": 10,
            "cooldown_hours": 16, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [{"time": "06:45", "message": "Time for morning prayer — let's start the day right!"}],
            "keywords": ["morning prayer", "said prayer", "prayed", "said our prayer", "morning prayers"],
        },
        {
            "id": "make_bed", "label": "Make Bed", "points": 5,
            "cooldown_hours": 16, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [{"time": "07:00", "message": "Don't forget to make your bed before you start the day!"}],
            "keywords": ["made my bed", "made bed", "tidied my bed", "made up bed"],
        },
        {
            "id": "meal_prayer", "label": "Prayer Before Meal", "points": 10,
            "cooldown_hours": 4, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [{"time": "15:30", "message": "Time to say grace before your meal!"}],
            "keywords": ["meal prayer", "said grace", "prayer before meal", "blessed the food", "prayed before eating"],
        },
        {
            "id": "empty_toilet_bin", "label": "Empty Toilet Bin", "points": 5,
            "cooldown_hours": 16, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [{"time": "19:00", "message": "Has the toilet bin been emptied today?"}],
            "keywords": ["emptied toilet bin", "toilet bin", "bathroom bin", "empty toilet bin"],
        },
        {
            "id": "tidy_bedroom", "label": "Tidy Bedroom", "points": 10,
            "cooldown_hours": 16, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [{"time": "16:00", "message": "Time to tidy your bedroom!"}],
            "keywords": ["tidied bedroom", "tidied my room", "cleaned bedroom", "cleaned my room", "tidy bedroom", "tidy room"],
        },
        {
            "id": "empty_kitchen_bin", "label": "Empty Kitchen Bin", "points": 10,
            "cooldown_hours": 16, "verification": "camera",
            "camera_entity_id": "camera.tangu_home_kitchen",
            "camera_prompt": "Look at the kitchen bin area. Is the bin empty or has it been recently emptied? Answer YES if the bin looks empty or has a fresh liner, NO if it looks full.",
            "requires_approval": False,
            "reminders": [{"time": "19:00", "message": "Has the kitchen bin been emptied today?"}],
            "keywords": ["emptied kitchen bin", "kitchen bin", "emptied the bin", "empty kitchen bin"],
        },
        {
            "id": "tidy_living_room", "label": "Tidy Living Room", "points": 8,
            "cooldown_hours": 6, "verification": "camera",
            "camera_entity_id": "camera.reolink_living_room_profile000_mainstream",
            "camera_prompt": "Look at the living room. Is it tidy — cushions straight, no items left on the floor or sofa? Answer YES if it looks reasonably tidy, NO if it's messy.",
            "requires_approval": False,
            "reminders": [{"time": "17:15", "message": "Please tidy the living room after eating!"}],
            "keywords": ["tidied living room", "tidied lounge", "cleaned living room", "tidy living room", "tidied sitting room"],
        },
        {
            "id": "load_dishwasher", "label": "Load/Unload Dishwasher", "points": 8,
            "cooldown_hours": 8, "verification": "camera",
            "camera_entity_id": "camera.tangu_home_kitchen",
            "camera_prompt": "Look at the kitchen. Does it look like the dishwasher has been loaded or unloaded — dishes put away, sink area tidy? Answer YES or NO.",
            "requires_approval": False,
            "reminders": [{"time": "19:30", "message": "Don't forget to load or empty the dishwasher!"}],
            "keywords": ["dishwasher", "loaded dishwasher", "unloaded dishwasher", "emptied dishwasher", "put away dishes"],
        },
        {
            "id": "wipe_kitchen", "label": "Wipe Kitchen Surfaces", "points": 8,
            "cooldown_hours": 16, "verification": "camera",
            "camera_entity_id": "camera.tangu_home_kitchen",
            "camera_prompt": "Look at the kitchen surfaces and worktops. Do they look clean and wiped down? Answer YES if they look clean, NO if they look dirty or cluttered.",
            "requires_approval": False,
            "reminders": [{"time": "20:00", "message": "Please wipe down the kitchen surfaces!"}],
            "keywords": ["wiped kitchen", "cleaned kitchen", "wiped surfaces", "cleaned surfaces", "wiped the kitchen"],
        },
        {
            "id": "clear_table", "label": "Clear Table After Meal", "points": 5,
            "cooldown_hours": 4, "verification": "camera",
            "camera_entity_id": "camera.reolink_living_room_profile000_mainstream",
            "camera_prompt": "Look at the dining table or eating area. Has it been cleared after a meal — no plates, cups or food left on it? Answer YES if cleared, NO if still has items on it.",
            "requires_approval": False,
            "reminders": [],
            "keywords": ["cleared table", "cleared the table", "tidied table", "cleared up after dinner", "cleared up after eating"],
        },
        {
            "id": "hoover_living_room", "label": "Hoover Living Room", "points": 12,
            "cooldown_hours": 48, "verification": "camera",
            "camera_entity_id": "camera.reolink_living_room_profile000_mainstream",
            "camera_prompt": "Look at the living room floor. Does the carpet/floor look clean and freshly hoovered? Answer YES if it looks clean, NO if it looks dirty.",
            "requires_approval": True,
            "reminders": [{"time": "10:00", "day": "saturday", "message": "Time to hoover the living room — it's Saturday cleaning day!"}],
            "keywords": ["hoovered", "vacuumed", "hoovered living room", "vacuumed living room", "done the hoovering"],
        },
        {
            "id": "take_recycling", "label": "Take Recycling Out", "points": 8,
            "cooldown_hours": 48, "verification": "honour", "camera_entity_id": None,
            "requires_approval": False,
            "reminders": [],
            "keywords": ["recycling", "took out recycling", "put out recycling", "taken recycling out"],
        },
    ],
}


class ScoreboardService:
    def __init__(self, db_path: Path, config_path: Path) -> None:
        self._db_path = db_path
        self._config_path = config_path
        self._face_service = None  # set by startup after init
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(_DEFAULT_CONFIG, indent=2) + "\n")
            _LOGGER.info("scoreboard.config_created", path=str(config_path))

    async def get_members(self) -> list[str]:
        """Return known face names from CPAI, falling back to config members."""
        if self._face_service is not None:
            try:
                faces = await self._face_service.list_known_faces()
                if faces:
                    return sorted(f.lower() for f in faces)
            except Exception:
                pass
        return self.get_config().get("members", [])

    # ── DB ────────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS chore_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_label TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    ts REAL NOT NULL
                )
            """)
            db.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Config ─────────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        try:
            return json.loads(self._config_path.read_text())
        except Exception:
            return _DEFAULT_CONFIG

    def save_config(self, config: dict[str, Any]) -> None:
        self._config_path.write_text(json.dumps(config, indent=2) + "\n")


    def get_penalties(self) -> list[dict]:
        return self.get_config().get("penalties", [])

    def get_penalty(self, penalty_id: str) -> dict | None:
        for p in self.get_penalties():
            if p["id"] == penalty_id:
                return p
        return None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for t in self.get_config().get("tasks", []):
            if t["id"] == task_id:
                return t
        return None

    # ── Cooldown / duplicate check ─────────────────────────────────────────────

    def already_logged_today(self, task_id: str, person: str) -> bool:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM chore_logs WHERE task_id=? AND person=? AND ts>=?",
                (task_id, person.lower(), midnight)
            ).fetchone()
            return (row[0] or 0) > 0

    def cooldown_remaining(self, task_id: str, person: str, cooldown_hours: float) -> float:
        """Returns seconds remaining in cooldown, or 0 if free to log."""
        cutoff = time.time() - cooldown_hours * 3600
        with self._conn() as db:
            row = db.execute(
                "SELECT MAX(ts) FROM chore_logs WHERE task_id=? AND person=?",
                (task_id, person.lower())
            ).fetchone()
            last_ts = row[0] if row and row[0] else 0
        if last_ts < cutoff:
            return 0.0
        return (last_ts + cooldown_hours * 3600) - time.time()

    # ── Logging ────────────────────────────────────────────────────────────────

    def record_chore(self, person: str, task_id: str, task_label: str, points: int, verified: bool) -> int:
        with self._conn() as db:
            cur = db.execute(
                "INSERT INTO chore_logs (person, task_id, task_label, points, verified, ts) VALUES (?,?,?,?,?,?)",
                (person.lower(), task_id, task_label, points, int(verified), time.time())
            )
            db.commit()
            _LOGGER.info("scoreboard.chore_logged", person=person, task=task_id, points=points)
            return cur.lastrowid

    def delete_log(self, log_id: int) -> bool:
        with self._conn() as db:
            db.execute("DELETE FROM chore_logs WHERE id=?", (log_id,))
            db.commit()
        return True

    # ── Leaderboard ────────────────────────────────────────────────────────────

    def _week_bounds(self) -> tuple[float, float]:
        cfg = self.get_config()
        week_start_day = cfg.get("week_start", "monday").lower()
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target_wd = days.index(week_start_day) if week_start_day in days else 0
        now = datetime.now()
        current_wd = now.weekday()
        days_back = (current_wd - target_wd) % 7
        week_start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        return week_start.timestamp(), week_end.timestamp()

    def weekly_scores(self) -> list[dict[str, Any]]:
        start, end = self._week_bounds()
        with self._conn() as db:
            rows = db.execute(
                "SELECT person, SUM(points) as total, COUNT(*) as tasks FROM chore_logs WHERE ts>=? AND ts<? GROUP BY person ORDER BY total DESC",
                (start, end)
            ).fetchall()
        return [{"person": r["person"], "points": r["total"], "tasks": r["tasks"]} for r in rows]

    def recent_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, person, task_id, task_label, points, verified, ts FROM chore_logs ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_logs(self, days: int = 7) -> list[dict[str, Any]]:
        cutoff = time.time() - days * 86400
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, person, task_id, task_label, points, verified, ts FROM chore_logs WHERE ts>=? ORDER BY ts DESC",
                (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Main entry point called by ha_proxy ────────────────────────────────────

    async def handle_log_chore(self, args: dict[str, Any], ha_proxy: Any, llm_service: Any) -> str:
        """Validate, optionally verify via camera, and record a chore. Returns a natural-language result string."""
        from avatar_backend.models.tool_result import ToolResult  # noqa: F401

        task_id = str(args.get("task_id") or "").strip().lower()
        person = str(args.get("person") or "").strip().lower()

        if not task_id or not person:
            return "I need both a task name and a person to log a chore."

        task = self.get_task(task_id)
        if task is None:
            # Try fuzzy match against labels/keywords
            cfg = self.get_config()
            person_lower = person
            for t in cfg.get("tasks", []):
                for kw in t.get("keywords", []):
                    if kw in task_id or task_id in kw:
                        task = t
                        task_id = t["id"]
                        break
                if task:
                    break
            if task is None:
                return f"I don't recognise '{task_id}' as a tracked chore. Known tasks: {', '.join(t['id'] for t in cfg.get('tasks', []))}."

        # Check assignment — if task is assigned to specific members, only they can log it
        assigned_to = task.get("assigned_to", [])
        if assigned_to and person not in [m.lower() for m in assigned_to]:
            assigned_names = ", ".join(m.title() for m in assigned_to)
            return f"This task is assigned to {assigned_names}, not {person.title()}."

        label = task["label"]
        points = task["points"]
        cooldown_h = task.get("cooldown_hours", 16)
        verification = task.get("verification", "honour")
        camera_entity = task.get("camera_entity_id")
        requires_approval = task.get("requires_approval", False)

        # Cooldown check
        remaining = self.cooldown_remaining(task_id, person, cooldown_h)
        if remaining > 0:
            hrs = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            time_str = f"{hrs}h {mins}m" if hrs else f"{mins} minutes"
            return f"Nice try! {person.title()} already logged {label} recently. They can log it again in {time_str}."

        # Camera verification
        verified = verification == "honour"
        if verification == "camera" and camera_entity and ha_proxy is not None and llm_service is not None:
            try:
                image_bytes = await ha_proxy.fetch_camera_image(camera_entity)
                if image_bytes:
                    prompt = task.get("camera_prompt", f"Has the {label} task been completed? Answer YES or NO.")
                    description = await llm_service.describe_image(image_bytes, prompt=prompt)
                    desc_upper = description.upper()
                    if "YES" in desc_upper:
                        verified = True
                    elif "NO" in desc_upper:
                        return f"Hmm, I checked the camera and it doesn't look like {label} has been done yet. Give it another go and tell me when it's done!"
                    else:
                        # Uncertain — award anyway but flag
                        verified = True
                        _LOGGER.info("scoreboard.camera_uncertain", task=task_id, desc=description[:80])
                else:
                    verified = True  # Camera offline — honour
            except Exception as exc:
                _LOGGER.warning("scoreboard.camera_check_failed", task=task_id, exc=str(exc)[:80])
                verified = True

        if requires_approval and not verified:
            return f"I've noted that {person.title()} says they completed {label}. A parent will need to confirm before the points land."

        self.record_chore(person, task_id, label, points, verified)

        # Build leaderboard context for response
        scores = self.weekly_scores()
        leader = scores[0]["person"].title() if scores else person.title()
        person_score = next((s["points"] for s in scores if s["person"] == person.lower()), points)

        if leader.lower() == person.lower():
            return f"Great job {person.title()}! +{points} points for {label}. You're leading the scoreboard this week with {person_score} points! 🏆"
        else:
            return f"Nice one {person.title()}! +{points} points for {label}. You now have {person_score} points this week. Keep going — {leader} is in the lead!"
