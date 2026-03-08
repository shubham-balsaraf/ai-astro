"""
db.py — SQLite database layer for Krama.

Tables:
  users    — one row per unique username, stores birth details
  readings — one row per generated reading, linked to a user
  chats    — every follow-up question + answer, linked to a user

The database file (krama.db) lives in the project root.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "krama.db"


@contextmanager
def get_db():
    """Yield a database connection that auto-commits and closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    NOT NULL UNIQUE,
                birth_date  TEXT,
                birth_time  TEXT,
                birth_place TEXT,
                latitude    TEXT,
                longitude   TEXT,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                reading     TEXT    NOT NULL,
                raw_data    TEXT,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                question    TEXT    NOT NULL,
                answer      TEXT    NOT NULL,
                lang        TEXT    DEFAULT 'en',
                created_at  TEXT    NOT NULL
            );
        """)


# ── User operations ──────────────────────────────────────────────────────

def get_or_create_user(username):
    """Find a user by username, or create a new one. Returns the user row."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        if row:
            return dict(row)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)",
            (username, now, now),
        )
        return dict(conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone())


def update_user_birth(username, birth_date, birth_time, birth_place, latitude, longitude):
    """Save birth details for an existing user."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            UPDATE users
            SET birth_date = ?, birth_time = ?, birth_place = ?,
                latitude = ?, longitude = ?, updated_at = ?
            WHERE username = ?
        """, (birth_date, birth_time, birth_place, latitude, longitude, now, username))


def get_user(username):
    """Get a user by username. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


# ── Reading operations ───────────────────────────────────────────────────

def save_reading(username, reading_text, raw_data=None):
    """Save a generated reading for a user."""
    user = get_user(username)
    if not user:
        return None

    now = datetime.now(timezone.utc).isoformat()
    raw_json = json.dumps(raw_data) if raw_data else None

    with get_db() as conn:
        conn.execute(
            "INSERT INTO readings (user_id, reading, raw_data, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], reading_text, raw_json, now),
        )


def get_readings(username, limit=10):
    """Get past readings for a user, newest first."""
    user = get_user(username)
    if not user:
        return []

    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, reading, raw_data, created_at
            FROM readings
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (user["id"], limit)).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            if r["raw_data"]:
                r["raw_data"] = json.loads(r["raw_data"])
            results.append(r)
        return results


# ── Chat operations ──────────────────────────────────────────────────────

def save_chat(username, question, answer, lang="en"):
    """Log a follow-up question and its answer."""
    user = get_user(username)
    if not user:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chats (user_id, question, answer, lang, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], question, answer, lang, now),
        )


def get_chats(username, limit=50):
    """Get chat history for a user, newest first."""
    user = get_user(username)
    if not user:
        return []
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, question, answer, lang, created_at
            FROM chats WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (user["id"], limit)).fetchall()
        return [dict(r) for r in rows]
