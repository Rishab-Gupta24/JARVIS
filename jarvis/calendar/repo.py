"""Events and reminders: the only code that writes to the calendar tables.

Every write is validated here, whichever way it arrives (model tool call,
direct API edit, offline sync).
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone

from jarvis.config import TZ
from jarvis.core.dates import spoken_date, spoken_time

EDITABLE = {"title", "start", "end", "all_day", "location", "notes", "important", "recurrence"}
REMINDER_OFFSETS = (timedelta(days=1), timedelta(hours=1), timedelta(minutes=10))
# All-day events: the evening before and the morning of.
ALL_DAY_REMINDERS = ((-1, time(20, 0)), (0, time(8, 0)))
# Anything outside this window from "now" is almost certainly a misread date.
MAX_PAST = timedelta(days=400)
MAX_FUTURE = timedelta(days=5 * 366)

_STOP_WORDS = {"my", "the", "a", "an", "event", "appointment", "thing"}


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Datetimes must carry a time zone.")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_db(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(TZ)


def local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), TZ)


@dataclass
class NewEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    important: bool = False
    recurrence: str | None = None
    source: str = "text"
    client_uuid: str | None = None
    raw_text: str | None = None


@dataclass
class Event:
    id: int
    title: str
    start: datetime  # local (Asia/Dubai)
    end: datetime
    all_day: bool
    location: str | None
    notes: str | None
    important: bool
    recurrence: str | None
    source: str
    client_uuid: str | None
    raw_text: str | None
    updated_at: datetime
    deleted: bool

    def when(self, today: date) -> str:
        day = spoken_date(self.start.date(), today)
        if self.all_day:
            last = (self.end - timedelta(days=1)).date()
            return day if last <= self.start.date() else f"{day} to {spoken_date(last, today)}"
        return f"{day} at {spoken_time(self.start.time())}"

    def to_dict(self, today: date | None = None) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location,
            "notes": self.notes,
            "important": self.important,
            "recurrence": self.recurrence,
            "source": self.source,
            "client_uuid": self.client_uuid,
            "updated_at": self.updated_at.isoformat(),
        }
        if today is not None:
            d["when"] = self.when(today)
        return d


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        title=row["title"],
        start=from_db(row["start_at"]),
        end=from_db(row["end_at"]),
        all_day=bool(row["all_day"]),
        location=row["location"],
        notes=row["notes"],
        important=bool(row["important"]),
        recurrence=row["recurrence"],
        source=row["source"],
        client_uuid=row["client_uuid"],
        raw_text=row["raw_text"],
        updated_at=from_db(row["updated_at"]),
        deleted=row["deleted_at"] is not None,
    )


def validate(title: str, start: datetime, end: datetime, all_day: bool, now: datetime) -> None:
    if not title or not title.strip():
        raise ValueError("The event needs a title.")
    if len(title) > 200:
        raise ValueError("The title is too long.")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Start and end must carry a time zone.")
    if end <= start:
        raise ValueError("The end must be after the start.")
    if all_day:
        s, e = start.astimezone(TZ), end.astimezone(TZ)
        if s.time() != time(0) or e.time() != time(0):
            raise ValueError("An all-day event must start and end at midnight.")
    if not now - MAX_PAST <= start <= now + MAX_FUTURE:
        raise ValueError(f"{start.astimezone(TZ).date()} is too far from today; that date looks wrong.")


def get_event(conn: sqlite3.Connection, event_id: int, include_deleted: bool = False) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None or (row["deleted_at"] is not None and not include_deleted):
        return None
    return _row_to_event(row)


def get_by_client_uuid(conn: sqlite3.Connection, client_uuid: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE client_uuid = ?", (client_uuid,)).fetchone()
    return _row_to_event(row) if row else None


def create_event(conn: sqlite3.Connection, new: NewEvent, now: datetime) -> Event:
    """Insert an event. A repeated client_uuid returns the existing event instead."""
    if new.client_uuid and (existing := get_by_client_uuid(conn, new.client_uuid)):
        return existing
    title = new.title.strip()
    validate(title, new.start, new.end, new.all_day, now)
    stamp = to_db(now)
    cur = conn.execute(
        """INSERT INTO events (title, start_at, end_at, all_day, location, notes, important,
                               recurrence, source, client_uuid, raw_text, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, to_db(new.start), to_db(new.end), int(new.all_day), new.location, new.notes,
         int(new.important), new.recurrence, new.source, new.client_uuid, new.raw_text, stamp, stamp),
    )
    event = get_event(conn, cur.lastrowid)
    sync_reminders(conn, event, now)
    conn.commit()
    return event


def update_event(conn: sqlite3.Connection, event_id: int, now: datetime, **changes) -> Event:
    event = get_event(conn, event_id)
    if event is None:
        raise LookupError(f"No event with id {event_id}.")
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValueError(f"Can't change {', '.join(sorted(unknown))}.")
    if "title" in changes:
        changes["title"] = (changes["title"] or "").strip()
    merged = replace(event, **changes)
    validate(merged.title, merged.start, merged.end, merged.all_day, now)
    conn.execute(
        """UPDATE events SET title = ?, start_at = ?, end_at = ?, all_day = ?, location = ?,
                             notes = ?, important = ?, recurrence = ?, updated_at = ?
           WHERE id = ?""",
        (merged.title, to_db(merged.start), to_db(merged.end), int(merged.all_day), merged.location,
         merged.notes, int(merged.important), merged.recurrence, to_db(now), event_id),
    )
    event = get_event(conn, event_id)
    sync_reminders(conn, event, now)
    conn.commit()
    return event


def delete_event(conn: sqlite3.Connection, event_id: int, now: datetime) -> Event:
    """Soft delete, so a resent offline item with the same client_uuid stays deleted."""
    event = get_event(conn, event_id)
    if event is None:
        raise LookupError(f"No event with id {event_id}.")
    conn.execute("UPDATE events SET deleted_at = ?, updated_at = ? WHERE id = ?",
                 (to_db(now), to_db(now), event_id))
    conn.execute("DELETE FROM reminders WHERE event_id = ? AND status = 'pending'", (event_id,))
    conn.commit()
    return event


def list_events(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[Event]:
    """Events overlapping [start, end), oldest first."""
    rows = conn.execute(
        """SELECT * FROM events
           WHERE deleted_at IS NULL AND start_at < ? AND end_at > ?
           ORDER BY start_at, id""",
        (to_db(end), to_db(start)),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_on_day(conn: sqlite3.Connection, day: date) -> list[Event]:
    return list_events(conn, local_midnight(day), local_midnight(day + timedelta(days=1)))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _title_score(query: str, title: str) -> float:
    q = [t for t in _tokens(query) if t not in _STOP_WORDS] or _tokens(query)
    words = _tokens(title)
    if not q or not words:
        return 0.0

    def hit(qt: str) -> bool:
        return any(
            w == qt
            or (len(qt) >= 3 and w.startswith(qt))                            # "foot" -> "football"
            or (len(w) >= 3 and qt.startswith(w) and len(qt) - len(w) <= 2)   # "dentists" -> "dentist", not "ramadan" -> "rama"
            or difflib.SequenceMatcher(None, w, qt).ratio() >= 0.85
            for w in words
        )

    return sum(hit(t) for t in q) / len(q)


def find_events(
    conn: sqlite3.Connection, query: str, now: datetime, day: date | None = None
) -> list[tuple[float, Event]]:
    """Events whose title matches `query`, best match first, then nearest in time.

    Without `day`, looks at upcoming events first and falls back to recent past ones.
    """
    if day is not None:
        pools = [list_on_day(conn, day)]
    else:
        pools = [
            list_events(conn, now - timedelta(days=1), now + MAX_FUTURE),
            list_events(conn, now - MAX_PAST, now + MAX_FUTURE),
        ]
    for pool in pools:
        scored = [(s, e) for e in pool if (s := _title_score(query, e.title)) >= 0.5]
        if scored:
            scored.sort(key=lambda x: (-x[0], abs((x[1].start - now).total_seconds())))
            return scored
    return []


def reminder_times(event: Event, now: datetime) -> list[datetime]:
    if not event.important or event.deleted:
        return []
    if event.all_day:
        d = event.start.date()
        times = [datetime.combine(d + timedelta(days=off), t, TZ) for off, t in ALL_DAY_REMINDERS]
    else:
        times = [event.start - off for off in REMINDER_OFFSETS]
    return [t for t in times if t > now]


def sync_reminders(conn: sqlite3.Connection, event: Event, now: datetime) -> None:
    """Rebuild the event's pending reminders. Sent/acknowledged ones are kept as history."""
    conn.execute("DELETE FROM reminders WHERE event_id = ? AND status = 'pending'", (event.id,))
    for fire_at in reminder_times(event, now):
        conn.execute(
            "INSERT OR IGNORE INTO reminders (event_id, fire_at, channel) VALUES (?, ?, 'push')",
            (event.id, to_db(fire_at)),
        )


def pending_reminders(conn: sqlite3.Connection, event_id: int) -> list[datetime]:
    rows = conn.execute(
        "SELECT fire_at FROM reminders WHERE event_id = ? AND status = 'pending' ORDER BY fire_at",
        (event_id,),
    ).fetchall()
    return [from_db(r["fire_at"]) for r in rows]
