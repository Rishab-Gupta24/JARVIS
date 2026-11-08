"""SQLite connection and schema. Everything Jarvis knows is this one file.

Schema changes are appended to MIGRATIONS; never edit one that has shipped.
`PRAGMA user_version` records how many have been applied.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    # 1: calendar
    """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        start_at    TEXT    NOT NULL,          -- UTC ISO 8601
        end_at      TEXT    NOT NULL,          -- UTC ISO 8601, exclusive
        all_day     INTEGER NOT NULL DEFAULT 0,
        location    TEXT,
        notes       TEXT,
        important   INTEGER NOT NULL DEFAULT 0,
        recurrence  TEXT,                      -- RRULE, milestone 8
        source      TEXT    NOT NULL DEFAULT 'text',
        client_uuid TEXT    UNIQUE,            -- offline items from the phone, for dedupe
        raw_text    TEXT,                      -- the words the event came from
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        deleted_at  TEXT                       -- soft delete
    );
    CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);

    CREATE TABLE IF NOT EXISTS reminders (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        fire_at  TEXT    NOT NULL,             -- UTC ISO 8601
        channel  TEXT    NOT NULL DEFAULT 'push',
        status   TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'sent', 'acknowledged')),
        UNIQUE (event_id, fire_at, channel)
    );
    CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, fire_at);
    """,
    # 2: documents and mess menus
    """
    CREATE TABLE documents (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        kind           TEXT    NOT NULL
                       CHECK (kind IN ('mess_menu', 'timetable', 'exam_schedule', 'other')),
        filename       TEXT    NOT NULL,
        stored_path    TEXT,                   -- copy of the original under data/files/
        sha256         TEXT    UNIQUE,         -- same file sent twice is stored once
        source         TEXT    NOT NULL DEFAULT 'upload',
        received_at    TEXT    NOT NULL,       -- UTC ISO 8601; newest menu wins on overlap
        valid_from     TEXT,                   -- local date YYYY-MM-DD
        valid_to       TEXT,                   -- local date; NULL with repeats_weekly = open-ended
        repeats_weekly INTEGER,                -- NULL = not known yet
        status         TEXT    NOT NULL DEFAULT 'ready'
                       CHECK (status IN ('ready', 'needs_week')),
        raw_text       TEXT    NOT NULL DEFAULT ''
    );

    CREATE TABLE menu_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- Monday = 0
        date        TEXT,                      -- set for a dated menu
        meal        TEXT    NOT NULL CHECK (meal IN ('breakfast', 'lunch', 'snacks', 'dinner')),
        items       TEXT    NOT NULL           -- exactly as written in the file
    );
    CREATE INDEX idx_menu_items_doc ON menu_items(document_id, weekday);

    CREATE VIRTUAL TABLE documents_fts USING fts5(filename, body);  -- rowid = documents.id
    """,
    # 3: small key/value store (e.g. when holidays were last fetched)
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 4: reminders get delivery tracking (SQLite can't change a CHECK, so rebuild the table)
    """
    CREATE TABLE reminders_v4 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        fire_at    TEXT    NOT NULL,           -- UTC ISO 8601
        channel    TEXT    NOT NULL DEFAULT 'push',
        status     TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'sending', 'sent', 'acknowledged', 'missed', 'failed')),
        attempts   INTEGER NOT NULL DEFAULT 0,
        sent_at    TEXT,
        last_error TEXT,
        UNIQUE (event_id, fire_at, channel)
    );
    INSERT INTO reminders_v4 (id, event_id, fire_at, channel, status)
        SELECT id, event_id, fire_at, channel, status FROM reminders;
    DROP TABLE reminders;
    ALTER TABLE reminders_v4 RENAME TO reminders;
    CREATE INDEX idx_reminders_due ON reminders(status, fire_at);
    """,
    # 5: things captured on the iPhone with no connection, kept the moment they arrive
    """
    CREATE TABLE offline_items (
        id          TEXT PRIMARY KEY,          -- made on the phone; a resend is ignored
        raw         TEXT NOT NULL,             -- what the user said
        captured_at TEXT NOT NULL,             -- UTC ISO; relative dates are read as of this
        received_at TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        reply       TEXT
    );
    """,
    # 6: alarms and timers (alarms.py); a repeating alarm keeps one row and moves its fire_at on
    """
    CREATE TABLE alarms (
        id          INTEGER PRIMARY KEY,
        fire_at     TEXT NOT NULL,             -- UTC ISO
        label       TEXT NOT NULL DEFAULT '',
        repeat      TEXT CHECK (repeat IN ('daily', 'weekdays')),
        kind        TEXT NOT NULL DEFAULT 'alarm' CHECK (kind IN ('alarm', 'timer')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ringing', 'done', 'missed', 'cancelled')),
        raw_text    TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_alarms_due ON alarms(status, fire_at);
    """,
]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database, creating the file and applying migrations if needed.

    `check_same_thread=False` because FastAPI may run a request's dependency and
    handler on different worker threads; each request still gets its own connection.
    """
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
    return conn
