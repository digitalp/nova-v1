"""
User management and session service for the Nova admin panel.

Users stored in config/users.json with PBKDF2-SHA256 password hashing (stdlib only).

Roles
-----
admin  — full access: config, prompt, ACL, restart, user management
viewer — read-only:   dashboard, logs, sessions
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger()

Role = Literal["admin", "viewer"]
_SESSION_TTL = 24 * 3600  # 24 hours


# ── Password helpers ──────────────────────────────���───────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:260000:{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, iters, salt, dk_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── UserService ─────────────────────────────��─────────────────────────────────

class UserService:
    def __init__(self, users_file: Path) -> None:
        self._file     = users_file
        self._users:    list[dict] = []
        self._sessions: dict[str, dict] = {}  # token → {username, role, expires}
        self._load()

    # ── Persistence ────────────��──────────────────────────────────────────────

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            self._users = json.loads(self._file.read_text()).get("users", [])
        except Exception as exc:
            logger.error("user_service.load_failed", exc=str(exc))

    def _save(self) -> None:
        import os as _os
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps({"users": self._users}, indent=2))
        # L5 security fix: restrict file permissions to owner-only
        try:
            _os.chmod(self._file, 0o600)
        except OSError:
            pass  # best-effort on platforms that don't support chmod

    # ── User CRUD ──────────────────────────────────────────────────────────���──

    def has_users(self) -> bool:
        return bool(self._users)

    def list_users(self) -> list[dict]:
        return [{"username": u["username"], "role": u["role"]} for u in self._users]

    def create_user(self, username: str, password: str, role: Role) -> None:
        if any(u["username"] == username for u in self._users):
            raise ValueError(f"User '{username}' already exists")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        self._users.append({
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
        })
        self._save()
        logger.info("user_service.created", username=username, role=role)

    def delete_user(self, username: str) -> None:
        user = self._find(username)
        if user["role"] == "admin" and self._admin_count() <= 1:
            raise ValueError("Cannot delete the last admin account")
        self._users = [u for u in self._users if u["username"] != username]
        self._save()
        # Invalidate all sessions for this user
        gone = [t for t, s in self._sessions.items() if s["username"] == username]
        for t in gone:
            del self._sessions[t]
        logger.info("user_service.deleted", username=username)

    def change_password(self, username: str, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters")
        self._find(username)["password_hash"] = hash_password(new_password)
        self._save()

    def change_role(self, username: str, new_role: Role) -> None:
        user = self._find(username)
        if user["role"] == "admin" and new_role != "admin" and self._admin_count() <= 1:
            raise ValueError("Cannot demote the last admin account")
        user["role"] = new_role
        self._save()
        # Invalidate sessions — role change takes effect on next login
        gone = [t for t, s in self._sessions.items() if s["username"] == username]
        for t in gone:
            del self._sessions[t]

    # ── Authentication ────────────────────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> dict | None:
        user = next((u for u in self._users if u["username"] == username), None)
        if not user or not verify_password(password, user["password_hash"]):
            return None
        return {"username": user["username"], "role": user["role"]}

    # ── Sessions ────────────────────���──────────────────────────────���──────────

    def create_session(self, username: str, role: str) -> str:
        self._purge_expired()
        token = secrets.token_hex(32)
        self._sessions[token] = {
            "username": username,
            "role":     role,
            "expires":  time.monotonic() + _SESSION_TTL,
        }
        return token

    def validate_session(self, token: str) -> dict | None:
        self._purge_expired()
        sess = self._sessions.get(token)
        if not sess or sess["expires"] < time.monotonic():
            self._sessions.pop(token, None)
            return None
        return sess

    def invalidate_session(self, token: str) -> None:
        self._sessions.pop(token, None)

    # ── Helpers ───────────────────────────���────────────────────────────��──────

    def _find(self, username: str) -> dict:
        user = next((u for u in self._users if u["username"] == username), None)
        if not user:
            raise ValueError(f"User '{username}' not found")
        return user

    def _admin_count(self) -> int:
        return sum(1 for u in self._users if u["role"] == "admin")

    def _purge_expired(self) -> None:
        now     = time.monotonic()
        expired = [t for t, s in self._sessions.items() if s["expires"] < now]
        for t in expired:
            del self._sessions[t]
