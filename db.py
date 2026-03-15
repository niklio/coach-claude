import os
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "strava_cda.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        with _conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    athlete_id      INTEGER PRIMARY KEY,
                    phone_number    TEXT    NOT NULL,
                    access_token    TEXT    NOT NULL,
                    refresh_token   TEXT    NOT NULL,
                    expires_at      INTEGER NOT NULL DEFAULT 0,
                    weight_kg       REAL,
                    awaiting_weight INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()


def upsert_user(athlete_id: int, phone: str, access_token: str, refresh_token: str, expires_at: int) -> None:
    with _lock:
        with _conn() as conn:
            conn.execute("""
                INSERT INTO users (athlete_id, phone_number, access_token, refresh_token, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id) DO UPDATE SET
                    phone_number  = excluded.phone_number,
                    access_token  = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at    = excluded.expires_at
            """, (athlete_id, phone, access_token, refresh_token, expires_at))
            conn.commit()


def get_user_by_athlete(athlete_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE athlete_id = ?", (athlete_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_phone(phone: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE phone_number = ?", (phone,)).fetchone()
        return dict(row) if row else None


def update_tokens(athlete_id: int, access_token: str, refresh_token: str, expires_at: int) -> None:
    with _lock:
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET access_token=?, refresh_token=?, expires_at=? WHERE athlete_id=?",
                (access_token, refresh_token, expires_at, athlete_id),
            )
            conn.commit()


def set_weight(athlete_id: int, weight_kg: float) -> None:
    with _lock:
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET weight_kg=?, awaiting_weight=0 WHERE athlete_id=?",
                (weight_kg, athlete_id),
            )
            conn.commit()


def set_awaiting_weight(athlete_id: int, awaiting: bool) -> None:
    with _lock:
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET awaiting_weight=? WHERE athlete_id=?",
                (1 if awaiting else 0, athlete_id),
            )
            conn.commit()
