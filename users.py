"""Multi-user auth scaffold for the kite dashboard.

Identity model: a user is identified by their Kite user_id (e.g. "AB1234"),
which we obtain by calling kite.profile() once with their API key + access
token. Each user record stores their Kite credentials so the daily token
refresh + order placement can run per-user.

No passwords — anyone who can present valid Kite credentials becomes the
owner of that user_id record. Subsequent visits use a signed-cookie session.
Suitable for small private deployments (1-3 trusted users).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import session

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "kite.db")
_DB_LOCK = threading.Lock()    # SQLite is fine across threads; writers should serialize


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    kite_user_id    TEXT PRIMARY KEY,
    display_name    TEXT,
    api_key         TEXT NOT NULL,
    api_secret      TEXT NOT NULL,
    access_token    TEXT,
    totp_secret     TEXT,
    kite_password   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db() -> None:
    """Create the SQLite file and tables if missing. Idempotent."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    with _DB_LOCK, _conn() as c:
        c.executescript(_SCHEMA)
    try:
        os.chmod(_DB_PATH, 0o600)
    except OSError:
        pass


# ── User CRUD ────────────────────────────────────────────────────────────────

def upsert_user(*,
                kite_user_id: str,
                api_key: str,
                api_secret: str,
                access_token: Optional[str] = None,
                totp_secret: Optional[str] = None,
                kite_password: Optional[str] = None,
                display_name: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _DB_LOCK, _conn() as c:
        existing = c.execute(
            "SELECT kite_user_id FROM users WHERE kite_user_id = ?",
            (kite_user_id,),
        ).fetchone()
        if existing:
            sets, vals = [], []
            for field, val in (
                ("display_name",  display_name),
                ("api_key",       api_key),
                ("api_secret",    api_secret),
                ("access_token",  access_token),
                ("totp_secret",   totp_secret),
                ("kite_password", kite_password),
            ):
                if val is not None:
                    sets.append(f"{field} = ?")
                    vals.append(val)
            sets.append("updated_at = ?")
            vals.append(now)
            vals.append(kite_user_id)
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE kite_user_id = ?", vals)
        else:
            c.execute(
                """INSERT INTO users
                   (kite_user_id, display_name, api_key, api_secret,
                    access_token, totp_secret, kite_password,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kite_user_id, display_name, api_key, api_secret,
                 access_token, totp_secret, kite_password, now, now),
            )


def get_user(kite_user_id: str) -> Optional[dict]:
    with _DB_LOCK, _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE kite_user_id = ?", (kite_user_id,)
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT kite_user_id, display_name, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def update_access_token(kite_user_id: str, new_token: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _DB_LOCK, _conn() as c:
        c.execute(
            "UPDATE users SET access_token = ?, updated_at = ? WHERE kite_user_id = ?",
            (new_token, now, kite_user_id),
        )


def delete_user(kite_user_id: str) -> None:
    with _DB_LOCK, _conn() as c:
        c.execute("DELETE FROM users WHERE kite_user_id = ?", (kite_user_id,))


# ── Session helpers ──────────────────────────────────────────────────────────

_SESSION_KEY = "kite_user_id"


def current_user_id() -> Optional[str]:
    return session.get(_SESSION_KEY)


def current_user() -> Optional[dict]:
    uid = current_user_id()
    return get_user(uid) if uid else None


def login_user(kite_user_id: str) -> None:
    session[_SESSION_KEY] = kite_user_id
    session.permanent = True


def logout_user() -> None:
    session.pop(_SESSION_KEY, None)
