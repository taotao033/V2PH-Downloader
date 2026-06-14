"""User accounts + subscriptions, stored in a separate read-write SQLite DB.

Kept entirely apart from the archive's ``v2ph_profiles.sqlite3`` (which stays
read-only) so browsing data and user data never mix.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from . import config

# Usernames listed here (env V2PH_ADMIN, comma-separated) are always admins.
ADMIN_USERNAMES = {
    u.strip() for u in os.environ.get("V2PH_ADMIN", "").split(",") if u.strip()
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    plan          TEXT NOT NULL DEFAULT 'free',
    plan_expires  TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    plan        TEXT NOT NULL,
    days        INTEGER NOT NULL,
    amount      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'paid',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS favorites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,          -- 'album' | 'actor'
    slug       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, kind, slug),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS history (
    user_id    INTEGER NOT NULL,
    album_slug TEXT NOT NULL,
    viewed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, album_slug),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS password_resets (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.USER_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(config.USER_DB), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # Migrate older DBs that predate the is_admin column.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def get_secret_key() -> str:
    """Return a stable secret key, persisting a random one on first run."""
    if config.SECRET_KEY:
        return config.SECRET_KEY
    init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key='secret_key'").fetchone()
        if row:
            return row["value"]
        key = secrets.token_hex(32)
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('secret_key', ?)", (key,))
        conn.commit()
        return key
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(username: str, email: str, password: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        # First-ever user becomes admin; so do env-designated usernames.
        is_first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        is_admin = 1 if (is_first or username in ADMIN_USERNAMES) else 0
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
            (username, email, generate_password_hash(password), is_admin,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_by_id(user_id: int) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def authenticate(login: str, password: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? OR email=?", (login, login)
        ).fetchone()
    finally:
        conn.close()
    if row and check_password_hash(row["password_hash"], password):
        return row
    return None


def get_by_login(login: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username=? OR email=?", (login, login)
        ).fetchone()
    finally:
        conn.close()


def set_password(user_id: int, password: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (generate_password_hash(password), user_id))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Password reset tokens (local: no email; token surfaced via console / CLI)
# --------------------------------------------------------------------------- #
def create_reset_token(user_id: int, ttl_minutes: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
        conn.execute("INSERT INTO password_resets (token, user_id, expires_at) VALUES (?,?,?)",
                     (token, user_id, expires))
        conn.commit()
        return token
    finally:
        conn.close()


def get_reset(token: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM password_resets WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            return None
    except ValueError:
        return None
    return row


def consume_reset(token: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM password_resets WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #
def is_vip(user: sqlite3.Row | None) -> bool:
    if not user or user["plan"] != "vip" or not user["plan_expires"]:
        return False
    try:
        return datetime.fromisoformat(user["plan_expires"]) > datetime.utcnow()
    except ValueError:
        return False


def activate_subscription(user_id: int, plan: str) -> str:
    """Apply a plan (extending from current expiry if still active). Returns new expiry ISO date."""
    spec = config.PLANS[plan]
    conn = _connect()
    try:
        row = conn.execute("SELECT plan_expires FROM users WHERE id=?", (user_id,)).fetchone()
        now = datetime.utcnow()
        base = now
        if row and row["plan_expires"]:
            try:
                cur_exp = datetime.fromisoformat(row["plan_expires"])
                if cur_exp > now:
                    base = cur_exp
            except ValueError:
                pass
        new_exp = base + timedelta(days=spec["days"])
        conn.execute(
            "UPDATE users SET plan='vip', plan_expires=? WHERE id=?",
            (new_exp.isoformat(timespec="seconds"), user_id),
        )
        conn.execute(
            "INSERT INTO orders (user_id, plan, days, amount, status, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, plan, spec["days"], spec["price"], "paid", now.isoformat(timespec="seconds")),
        )
        conn.commit()
        return new_exp.isoformat(timespec="seconds")
    finally:
        conn.close()


def list_orders(user_id: int) -> list[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user_id,)
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def is_admin(user: sqlite3.Row | None) -> bool:
    return bool(user and user["is_admin"])


def list_users(search: str | None = None, limit: int = 30, offset: int = 0) -> list[sqlite3.Row]:
    where, params = _user_filter(search)
    sql = f"SELECT * FROM users {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    conn = _connect()
    try:
        return conn.execute(sql, (*params, limit, offset)).fetchall()
    finally:
        conn.close()


def count_users(search: str | None = None) -> int:
    where, params = _user_filter(search)
    conn = _connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM users {where}", params).fetchone()[0]
    finally:
        conn.close()


def _user_filter(search: str | None) -> tuple[str, tuple]:
    if search:
        like = f"%{search}%"
        return "WHERE username LIKE ? OR email LIKE ?", (like, like)
    return "", ()


def admin_grant_vip(user_id: int, plan: str) -> str:
    """Grant a plan's duration without recording a paid order (admin action)."""
    spec = config.PLANS[plan]
    conn = _connect()
    try:
        row = conn.execute("SELECT plan_expires FROM users WHERE id=?", (user_id,)).fetchone()
        now = datetime.utcnow()
        base = now
        if row and row["plan_expires"]:
            try:
                cur_exp = datetime.fromisoformat(row["plan_expires"])
                if cur_exp > now:
                    base = cur_exp
            except ValueError:
                pass
        new_exp = base + timedelta(days=spec["days"])
        conn.execute("UPDATE users SET plan='vip', plan_expires=? WHERE id=?",
                     (new_exp.isoformat(timespec="seconds"), user_id))
        conn.execute(
            "INSERT INTO orders (user_id, plan, days, amount, status, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, plan, spec["days"], 0, "granted", now.isoformat(timespec="seconds")),
        )
        conn.commit()
        return new_exp.isoformat(timespec="seconds")
    finally:
        conn.close()


def admin_revoke_vip(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE users SET plan='free', plan_expires=NULL WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def admin_set_admin(user_id: int, value: bool) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if value else 0, user_id))
        conn.commit()
    finally:
        conn.close()


def admin_stats() -> dict:
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        vip = conn.execute(
            "SELECT COUNT(*) FROM users WHERE plan='vip' AND plan_expires > ?",
            (datetime.utcnow().isoformat(timespec="seconds"),),
        ).fetchone()[0]
        orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'"
        ).fetchone()[0]
        return {"users": total, "vip": vip, "orders": orders, "revenue": revenue}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Favorites
# --------------------------------------------------------------------------- #
def toggle_favorite(user_id: int, kind: str, slug: str) -> bool:
    """Add or remove a favorite. Returns True if now favorited, False if removed."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM favorites WHERE user_id=? AND kind=? AND slug=?",
            (user_id, kind, slug),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM favorites WHERE id=?", (row["id"],))
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO favorites (user_id, kind, slug, created_at) VALUES (?,?,?,?)",
            (user_id, kind, slug, datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def is_favorite(user_id: int, kind: str, slug: str) -> bool:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND kind=? AND slug=?",
            (user_id, kind, slug),
        ).fetchone() is not None
    finally:
        conn.close()


def favorite_slugs(user_id: int, kind: str, limit: int = 500, offset: int = 0) -> list[str]:
    conn = _connect()
    try:
        return [r["slug"] for r in conn.execute(
            "SELECT slug FROM favorites WHERE user_id=? AND kind=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, kind, limit, offset),
        )]
    finally:
        conn.close()


def favorite_count(user_id: int, kind: str) -> int:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id=? AND kind=?", (user_id, kind)
        ).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Watch history
# --------------------------------------------------------------------------- #
def record_history(user_id: int, album_slug: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO history (user_id, album_slug, viewed_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id, album_slug) DO UPDATE SET viewed_at=excluded.viewed_at",
            (user_id, album_slug, datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def history_slugs(user_id: int, limit: int = 60, offset: int = 0) -> list[str]:
    conn = _connect()
    try:
        return [r["album_slug"] for r in conn.execute(
            "SELECT album_slug FROM history WHERE user_id=? ORDER BY viewed_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )]
    finally:
        conn.close()


def history_count(user_id: int) -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM history WHERE user_id=?", (user_id,)).fetchone()[0]
    finally:
        conn.close()
