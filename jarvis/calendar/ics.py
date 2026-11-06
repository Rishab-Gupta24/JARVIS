"""Jarvis's calendar as an iCalendar feed (RFC 5545), for the iPhone Calendar's
"subscribe" option. Read-only: Jarvis stays the source of truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jarvis.calendar.repo import Event
from jarvis.config import TZ

STAR = "★ "  # important events stand out in Apple Calendar, which shows titles only


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """Lines longer than 75 octets continue on the next line after a space."""
    out, current = [], ""
    for ch in line:
        if len((current + ch).encode()) > 75:
            out.append(current)
            current = " " + ch
        else:
            current += ch
    return out + [current]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calendar_ics(events: list[Event], now: datetime, name: str = "Jarvis") -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jarvis//Local assistant//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{_escape(name)}", f"X-WR-TIMEZONE:{TZ.key}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:jarvis-{e.id}@jarvis.local", f"DTSTAMP:{_utc(now)}",
                  f"LAST-MODIFIED:{_utc(e.updated_at)}"]
        if e.all_day:
            lines += [f"DTSTART;VALUE=DATE:{e.start.astimezone(TZ):%Y%m%d}",
                      f"DTEND;VALUE=DATE:{e.end.astimezone(TZ):%Y%m%d}", "TRANSP:TRANSPARENT"]
        else:
            lines += [f"DTSTART:{_utc(e.start)}", f"DTEND:{_utc(e.end)}"]
        lines.append(f"SUMMARY:{_escape((STAR if e.important else '') + e.title)}")
        if e.location:
            lines.append(f"LOCATION:{_escape(e.location)}")
        if e.notes:
            lines.append(f"DESCRIPTION:{_escape(e.notes)}")
        if e.important:
            lines.append("PRIORITY:1")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"


FEED_PAST = timedelta(days=60)
FEED_FUTURE = timedelta(days=400)
