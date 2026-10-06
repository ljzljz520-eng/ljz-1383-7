"""Session auth for the management UI/API (separate from the public archive)."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from . import db

SESSION_TTL_S = 60 * 60 * 8


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return salt, dk.hex()


def create_user(username: str, password: str, role: str = "admin") -> int:
    salt, h = hash_password(password)
    return db.execute(
        "INSERT INTO users(username,salt,passhash,role) VALUES (?,?,?,?)",
        (username, salt, h, role),
    )


def verify(username: str, password: str) -> int | None:
    row = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if row is None:
        return None
    _, h = hash_password(password, row["salt"])
    if hmac.compare_digest(h, row["passhash"]):
        return row["id"]
    return None


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=SESSION_TTL_S)
    db.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
        (token, user_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         exp.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    return token


def destroy_session(token: str):
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def session_user(token: str | None):
    if not token:
        return None
    row = db.q1(
        """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token=? AND s.expires_at > ?""",
        (token, db.now()),
    )
    return dict(row) if row else None
