"""Alarms and timers, kept in the `alarms` table so they survive restarts.

- "Wake me up at 7" with no am/pm means the next 7 o'clock (7 am if said at night).
- No day: the next time that clock time comes round (today, else tomorrow).
- Repeats: daily, or weekdays (Mon-Fri). After ringing, a repeating alarm moves to
  its next day; a one-off is marked done.
- Ringing (AlarmClock): rows are claimed atomically (pending -> ringing), so two
  Jarvis processes never ring the same alarm twice. It pushes to the phone (urgent)
  and, where there is a speaker, beeps and speaks until silenced: "hey jarvis",
  "stop the alarm", or after about a minute. An alarm found more than 10 minutes
  late (the PC was off) is marked missed instead of ringing.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from jarvis.calendar.repo import from_db, to_db
from jarvis.core.dates import TZ, DateParseError, _parse_time, resolve_date, spoken_date, spoken_time

LATE = timedelta(minutes=10)
REPEATS = ("daily", "weekdays")


@dataclass(frozen=True)
class Alarm:
    id: int
    fire_at: datetime   # aware, Asia/Dubai
    label: str
    repeat: str | None
    kind: str           # alarm / timer
    status: str

    def spoken(self, now: datetime) -> str:
        when = f"{spoken_time(self.fire_at.time())} {spoken_date(self.fire_at.date(), now.date())}"
        if self.repeat:
            when = f"{spoken_time(self.fire_at.time())} {'every day' if self.repeat == 'daily' else 'on weekdays'}"
        return f"{when}{f' for {self.label}' if self.label else ''}"


def _row(r: sqlite3.Row) -> Alarm:
    return Alarm(r["id"], from_db(r["fire_at"]).astimezone(TZ), r["label"] or "", r["repeat"], r["kind"],
                 r["status"])


def _next_repeat(at: datetime, repeat: str) -> datetime:
    nxt = at + timedelta(days=1)
    while repeat == "weekdays" and nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def when_alarm(time_text: str, date_text: str | None, now: datetime, repeat: str | None = None) -> datetime:
    """When an alarm said as words should ring, in Asia/Dubai."""
    t, sure = _parse_time(time_text)
    if date_text and date_text.strip().lower() not in ("", "today", "next", "tonight"):
        day = resolve_date(date_text, now.date())
        # A named day: "tomorrow at 7" is 7 am unless said otherwise.
        at = datetime.combine(day, t if sure else t.replace(hour=t.hour % 12), TZ)
        if at <= now:
            raise DateParseError("That time has already passed.")
        return at
    options = [t] if sure else [t.replace(hour=t.hour % 12), t.replace(hour=t.hour % 12 + 12)]
    candidates = []
    for d in (now.date(), now.date() + timedelta(days=1)):
        candidates += [datetime.combine(d, o, TZ) for o in options]
    at = min(c for c in candidates if c > now)
    if repeat == "weekdays":
        while at.weekday() >= 5:
            at += timedelta(days=1)
    return at


_UNIT = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                 "forty": 40, "forty-five": 45, "sixty": 60, "ninety": 90}


def duration(text: str) -> timedelta:
    """'10 minutes', '1 hour 30 min', 'half an hour', 'an hour and a half', '2h' -> a length of time."""
    s = text.lower().replace("half an hour", "30 minutes").replace("half a minute", "30 seconds")
    s = s.replace("and a half", "andahalf")
    s = re.sub(r"\b[a-z]+(?:-[a-z]+)?\b", lambda m: str(_NUMBER_WORDS.get(m[0], m[0])), s)
    s = re.sub(r"(\d+) andahalf (\w+)", r"\1.5 \2", s)        # one and a half hours
    s = re.sub(r"(\d+) (\w+) andahalf", r"\1.5 \2", s)        # an hour and a half
    units = "|".join(sorted(_UNIT, key=len, reverse=True))
    total = sum(float(n) * _UNIT[u] for n, u in re.findall(rf"(\d+(?:\.\d+)?)\s*({units})\b", s))
    if total <= 0:
        raise DateParseError(f"I couldn't tell how long {text!r} is.")
    if total > 24 * 3600:
        raise DateParseError("A timer can be at most 24 hours. Set an alarm instead.")
    return timedelta(seconds=round(total))


def spoken_length(d: timedelta) -> str:
    secs = int(d.total_seconds())
    h, rest = divmod(secs, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{n} {u}{'' if n == 1 else 's'}" for n, u in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " and ".join(parts) if len(parts) <= 2 else f"{parts[0]}, {parts[1]} and {parts[2]}"


def create(conn: sqlite3.Connection, fire_at: datetime, now: datetime, label: str = "", repeat: str | None = None,
           kind: str = "alarm", raw_text: str | None = None) -> Alarm:
    cur = conn.execute(
        "INSERT INTO alarms (fire_at, label, repeat, kind, status, raw_text, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (to_db(fire_at), label.strip()[:120], repeat, kind, raw_text, to_db(now)))
    conn.commit()
    return get(conn, cur.lastrowid)


def get(conn: sqlite3.Connection, alarm_id: int) -> Alarm:
    return _row(conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone())


def upcoming(conn: sqlite3.Connection) -> list[Alarm]:
    rows = conn.execute("SELECT * FROM alarms WHERE status = 'pending' ORDER BY fire_at").fetchall()
    return [_row(r) for r in rows]


def cancel(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE alarms SET status = 'cancelled' WHERE id = ? AND status IN ('pending', 'ringing')",
                     [(i,) for i in ids])
    conn.commit()


def matching(alarms: list[Alarm], time_text: str | None, label: str | None, kind: str | None) -> list[Alarm]:
    """The alarms a 'cancel my 7 am alarm' / 'cancel the pasta timer' means."""
    found = [a for a in alarms if kind is None or a.kind == kind]
    if time_text:
        try:
            t, sure = _parse_time(time_text)
            hours = {t.hour} if sure else {t.hour % 12, t.hour % 12 + 12}
            found = [a for a in found if a.fire_at.hour in hours and a.fire_at.minute == t.minute]
        except DateParseError:
            pass
    if label:
        words = set(re.findall(r"[a-z0-9]+", label.lower())) - {"alarm", "timer", "the", "my", "for"}
        if words:
            found = [a for a in found if words & set(re.findall(r"[a-z0-9]+", a.label.lower()))] or (
                found if time_text else [])
    return found


# ---------- ringing ----------

_silence = threading.Event()


def silence() -> bool:
    """Stop whatever is ringing ('hey jarvis' or 'stop the alarm'). True if something was."""
    was = RINGING.is_set()
    _silence.set()
    return was


RINGING = threading.Event()


def alarm_tone(rate: int = 24000, seconds: float = 2.4):
    """Three rising beeps, repeated: clearly an alarm, not Jarvis's listening chime."""
    import numpy as np

    beep_len, gap = int(rate * 0.16), int(rate * 0.09)
    out = []
    while sum(len(x) for x in out) < rate * seconds:
        for freq in (880, 988, 1175):
            t = np.arange(beep_len) / rate
            out.append(0.35 * np.sin(2 * np.pi * freq * t) * np.hanning(beep_len))
            out.append(np.zeros(gap))
        out.append(np.zeros(int(rate * 0.35)))
    return np.concatenate(out).astype(np.float32), rate


def message(alarm: Alarm) -> tuple[str, str]:
    """(phone title, spoken words)."""
    if alarm.kind == "timer":
        title = f"Timer done{f': {alarm.label}' if alarm.label else ''}"
        return title, f"Your {alarm.label + ' ' if alarm.label else ''}timer is done."
    clock = spoken_time(alarm.fire_at.time())
    title = f"Alarm, {clock}{f': {alarm.label}' if alarm.label else ''}"
    said = f"It's {clock}." + (f" Time for {alarm.label}." if alarm.label else
                               " Time to get up." if alarm.fire_at.hour < 11 else " This is your alarm.")
    return title, said


class AlarmClock:
    """Checks every few seconds; rings what is due. `ring(words, stop)` plays it on the PC (optional),
    `push(title, words)` sends it to the phone (optional)."""

    def __init__(self, open_conn: Callable[[], sqlite3.Connection], clock: Callable[[], datetime],
                 ring: Callable[[str, threading.Event], None] | None = None,
                 push: Callable[[str, str], None] | None = None, log: Callable[[str], None] = print):
        self._open, self._clock, self._ring, self._push, self._log = open_conn, clock, ring, push, log
        self._stop = threading.Event()

    def recover(self) -> None:
        conn = self._open()
        try:
            conn.execute("UPDATE alarms SET status = 'pending' WHERE status = 'ringing'")
            conn.commit()
        finally:
            conn.close()

    def run_once(self) -> list[Alarm]:
        conn = self._open()
        try:
            due = self._claim(conn, self._clock())
        finally:
            conn.close()
        for alarm in due:
            self._sound(alarm)
        return due

    def _claim(self, conn: sqlite3.Connection, now: datetime) -> list[Alarm]:
        claimed = []
        for r in conn.execute("SELECT * FROM alarms WHERE status = 'pending' AND fire_at <= ? ORDER BY fire_at",
                              (to_db(now),)).fetchall():
            alarm = _row(r)
            late = now - alarm.fire_at > LATE
            if alarm.repeat:
                nxt = alarm.fire_at
                while nxt <= now:
                    nxt = _next_repeat(nxt, alarm.repeat)
                cur = conn.execute("UPDATE alarms SET fire_at = ? WHERE id = ? AND status = 'pending' AND fire_at = ?",
                                   (to_db(nxt), alarm.id, r["fire_at"]))
            else:
                cur = conn.execute("UPDATE alarms SET status = ? WHERE id = ? AND status = 'pending'",
                                   ("missed" if late else "done", alarm.id))
            if cur.rowcount == 1 and not late:  # another process didn't take it first
                claimed.append(alarm)
            elif late and cur.rowcount == 1:
                self._log(f"alarm: missed {alarm.spoken(now)} (the PC was off)")
        conn.commit()
        return claimed

    def _sound(self, alarm: Alarm) -> None:
        title, said = message(alarm)
        self._log(f"alarm: {title}")
        if self._push is not None:
            try:
                self._push(title, said)
            except Exception as e:
                self._log(f"alarm: couldn't reach the phone: {e}")
        if self._ring is not None:
            _silence.clear()
            RINGING.set()
            try:
                self._ring(said, _silence)
            except Exception as e:
                self._log(f"alarm: couldn't ring: {e!r}")
            finally:
                RINGING.clear()

    def start(self, every: float = 5.0) -> threading.Thread:
        self.recover()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:
                    self._log(f"alarm loop: {e!r}")
                self._stop.wait(every)

        thread = threading.Thread(target=loop, name="alarms", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def ring_with(speaker, cycles: int = 8) -> Callable[[str, threading.Event], None]:
    """Beep and speak on the PC until silenced, about a minute at most."""

    def ring(said: str, stop: threading.Event) -> None:
        tone, rate = alarm_tone()
        for i in range(cycles):
            if stop.is_set():
                return
            speaker.play(tone, rate)
            if stop.wait(0.6):
                return
            if i % 3 == 0:
                speaker.say(said)
                if stop.wait(0.6):
                    return

    return ring
