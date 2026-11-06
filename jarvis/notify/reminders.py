"""Deliver due reminders: push to the phone (ntfy) and, in voice mode, say it aloud.

The `reminders` table is the schedule: rows are created by repo.sync_reminders
when an event is important, and survive restarts. A loop checks every 20 s.

- A row is claimed atomically (pending -> sending), so `serve` and `voice` can both
  run the loop without sending anything twice.
- PC was off: of the overdue reminders for one event, only the latest goes out; the
  earlier ones are marked missed. If the event has already started (or, for an
  all-day event, the day is over) nothing is sent late; they're marked missed.
- A failed push is retried on the next pass, up to 3 attempts, then marked failed.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from jarvis.calendar import repo
from jarvis.calendar.repo import Event, from_db, to_db
from jarvis.core.dates import spoken_date, spoken_time

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Notice:
    reminder_id: int
    event: Event
    title: str
    message: str
    priority: str  # "default" or "high"
    spoken: str


class Sender(Protocol):
    def send(self, notice: Notice) -> None: ...


def _lead(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"in {hours} hour{'s' if hours != 1 else ''}"


def describe(event: Event, now: datetime, reminder_id: int = 0) -> Notice:
    """What the phone shows and what Jarvis says, e.g. 'DBMS quiz' / 'In 10 minutes, at 10 am'."""
    today = now.date()
    day = spoken_date(event.start.date(), today)
    if event.all_day:
        message = f"{day[:1].upper()}{day[1:]}"
        priority = "high" if event.start.date() == today else "default"
        spoken = f"Reminder: {event.title}, {day}."
    else:
        lead = event.start - now
        at = spoken_time(event.start.time())
        if lead <= timedelta(hours=12):
            when = f"{_lead(lead)}, at {at}"
        else:
            when = f"{day} at {at}"
        message = when[:1].upper() + when[1:]
        priority = "high" if lead <= timedelta(minutes=15) else "default"
        spoken = f"Reminder: {event.title}, {when}."
    if event.location:
        message += f", {event.location}"
    return Notice(reminder_id, event, event.title, message, priority, spoken)


def _too_late(event: Event, now: datetime) -> bool:
    if event.all_day:
        return now.date() > event.start.date()
    return now >= event.start


class PushSender:
    """The phone, via the ntfy server on this PC."""

    def __init__(self, ntfy):
        self._ntfy = ntfy

    def send(self, notice: Notice) -> None:
        self._ntfy.publish(notice.title, notice.message, notice.priority, tags=["alarm_clock"])

    def send_text(self, title: str, message: str) -> None:
        """Anything else for the phone, e.g. the result of reading a shared photo."""
        self._ntfy.publish(title, message, "default", tags=["robot"])


class SpeakSender:
    """Says it aloud on the PC (voice mode). Best effort: never blocks the push."""

    def __init__(self, say: Callable[[str], None]):
        self._say = say

    def send(self, notice: Notice) -> None:
        self._say(notice.spoken)


class Dispatcher:
    def __init__(self, open_conn: Callable[[], sqlite3.Connection], clock: Callable[[], datetime],
                 push: Sender | None, extras: list[Sender] | None = None,
                 log: Callable[[str], None] = print):
        """`push` decides whether a reminder counts as sent; `extras` (speaking) are best effort.
        With no push configured, the first extra takes its place."""
        extras = list(extras or [])
        if push is None and extras:
            push = extras.pop(0)
        if push is None:
            raise ValueError("No way to deliver reminders is configured.")
        self._open, self._clock, self._push, self._extras, self._log = open_conn, clock, push, extras, log
        self._stop = threading.Event()

    def recover(self) -> None:
        """A 'sending' row left by a crash goes back to pending."""
        conn = self._open()
        try:
            conn.execute("UPDATE reminders SET status = 'pending' WHERE status = 'sending'")
            conn.commit()
        finally:
            conn.close()

    def run_once(self) -> list[Notice]:
        conn = self._open()
        try:
            return self._deliver(conn, self._clock())
        finally:
            conn.close()

    def _claim(self, conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
        due = conn.execute(
            "SELECT id FROM reminders WHERE status = 'pending' AND fire_at <= ? ORDER BY fire_at",
            (to_db(now),),
        ).fetchall()
        claimed = []
        for row in due:
            cur = conn.execute("UPDATE reminders SET status = 'sending' WHERE id = ? AND status = 'pending'",
                               (row["id"],))
            if cur.rowcount == 1:  # another process didn't get there first
                claimed.append(conn.execute("SELECT * FROM reminders WHERE id = ?", (row["id"],)).fetchone())
        conn.commit()
        return claimed

    def _set(self, conn: sqlite3.Connection, rid: int, status: str, now: datetime | None = None,
             error: str | None = None, attempt: bool = False) -> None:
        conn.execute(
            """UPDATE reminders SET status = ?, sent_at = COALESCE(?, sent_at), last_error = ?,
                                    attempts = attempts + ? WHERE id = ?""",
            (status, to_db(now) if now else None, error, int(attempt), rid),
        )

    def _deliver(self, conn: sqlite3.Connection, now: datetime) -> list[Notice]:
        by_event: dict[int, list[sqlite3.Row]] = {}
        for row in self._claim(conn, now):
            by_event.setdefault(row["event_id"], []).append(row)
        sent = []
        for event_id, rows in by_event.items():
            rows.sort(key=lambda r: r["fire_at"])
            event = repo.get_event(conn, event_id)
            latest = rows[-1]
            for older in rows[:-1]:  # piled up while the PC was off: one notice is enough
                self._set(conn, older["id"], "missed")
            if event is None or not event.important or _too_late(event, now):
                self._set(conn, latest["id"], "missed")
                if event is not None and _too_late(event, now):
                    self._log(f"reminder: missed {event.title} (fired late, at {spoken_time(now.time())})")
                continue
            notice = describe(event, now, latest["id"])
            try:
                self._push.send(notice)
            except Exception as e:
                failed = latest["attempts"] + 1 >= MAX_ATTEMPTS
                self._set(conn, latest["id"], "failed" if failed else "pending", error=str(e)[:300], attempt=True)
                self._log(f"reminder: couldn't send {event.title}: {e}{' (giving up)' if failed else ''}")
                continue
            self._set(conn, latest["id"], "sent", now=now, attempt=True)
            sent.append(notice)
            self._log(f"reminder sent: {notice.title} - {notice.message}")
            for extra in self._extras:
                try:
                    extra.send(notice)
                except Exception as e:
                    self._log(f"reminder: {type(extra).__name__} failed: {e}")
        conn.commit()
        return sent

    def start(self, every: float = 20.0) -> threading.Thread:
        self.recover()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:  # keep going whatever one pass hit
                    self._log(f"reminder loop: {e!r}")
                self._stop.wait(every)

        thread = threading.Thread(target=loop, name="reminders", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def history(conn: sqlite3.Connection, event_id: int) -> list[tuple[datetime, str]]:
    rows = conn.execute("SELECT fire_at, status FROM reminders WHERE event_id = ? ORDER BY fire_at",
                        (event_id,)).fetchall()
    return [(from_db(r["fire_at"]), r["status"]) for r in rows]
