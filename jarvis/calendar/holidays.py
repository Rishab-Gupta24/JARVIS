"""Festivals and public holidays from the internet, kept in Jarvis's own calendar.

Source: Google's public holiday calendars, as iCalendar files (no account or key).
Only these files are downloaded; nothing about the user is sent. Each sync imports
this year and next year as all-day events (source "holiday"), then:

- the same day in two regions' lists (New Year's Day) is stored once;
- a holiday that moved (tentative lunar dates) or vanished from the list is removed,
  and the new date added;
- a holiday the user deleted stays deleted, and one they marked important keeps it
  (an event is identified by `client_uuid` = date + name, never re-created).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import httpx

from jarvis.calendar import db, repo
from jarvis.calendar.repo import local_midnight

GOOGLE_ICS = "https://calendar.google.com/calendar/ical/en.{code}%23holiday%40group.v.calendar.google.com/public/basic.ics"
REGIONS = {  # friendly name -> Google holiday calendar code
    "india": "indian", "uae": "ae", "uk": "uk", "usa": "usa", "pakistan": "pk", "nepal": "np",
    "bangladesh": "bd", "sri lanka": "lk", "saudi arabia": "saudiarabian", "singapore": "singapore",
}
LABELS = {"india": "India", "uae": "UAE", "uk": "UK", "usa": "USA"}
# Names as people say them, so "when is choti diwali?" finds it.
ALIASES = {
    "Naraka Chaturdasi": "Choti Diwali (Naraka Chaturdasi)",
    "Diwali/Deepavali": "Diwali",
    "Dussehra": "Dussehra (Vijayadashami)",
}
SOURCE = "holiday"
LAST_SYNC = "holidays.last_sync"


@dataclass(frozen=True)
class Holiday:
    day: date
    days: int  # length in days (almost always 1)
    title: str
    public: bool  # public holiday, or just an observance
    tentative: bool


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    kept: int = 0
    years: tuple[int, ...] = ()
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        span = "-".join(map(str, (self.years[0], self.years[-1]))) if self.years else ""
        text = f"Holidays {span}: {self.added} added, {self.updated} updated, {self.removed} removed, {self.kept} unchanged."
        return text + (f" Problems: {'; '.join(self.errors)}" if self.errors else "")


def feed_url(region: str) -> str:
    return GOOGLE_ICS.format(code=REGIONS.get(region.lower().strip(), region.lower().strip()))


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(text: str) -> list[Holiday]:
    """All-day events in an iCalendar file. Treated strictly as data."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    found = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields = dict(re.findall(r"^([A-Z-]+)(?:;[^:\r\n]*)?:(.*?)\r?$", block.strip(), re.M))
        start, title = fields.get("DTSTART", ""), _unescape(fields.get("SUMMARY", "")).strip()
        if not re.fullmatch(r"\d{8}", start) or not title or fields.get("STATUS") == "CANCELLED":
            continue
        day = date(int(start[:4]), int(start[4:6]), int(start[6:]))
        end = fields.get("DTEND", "")
        days = 1
        if re.fullmatch(r"\d{8}", end):
            days = max(1, min(31, (date(int(end[:4]), int(end[4:6]), int(end[6:])) - day).days))
        about = _unescape(fields.get("DESCRIPTION", "")).lower()
        # Google sometimes writes "Eid al-Fitr (tentative)": keep that in the notes, not the name.
        title, marked = re.subn(r"\s*\(tentative\)\s*$", "", title, flags=re.I)
        found.append(Holiday(day, days, title[:150], about.startswith("public holiday"),
                             bool(marked) or "tentative" in about))
    return found


def _key(h: Holiday) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", h.title.lower()).strip("-")
    return f"{SOURCE}:{h.day.isoformat()}:{slug}"


def _notes(public: bool, regions: list[str], tentative: bool) -> str:
    kind = "Public holiday" if public else "Observance"
    note = f"{kind} ({', '.join(regions)})"
    return note + ". Date is tentative." if tentative else note + "."


def sync(conn: sqlite3.Connection, feeds: dict[str, str], now: datetime) -> SyncResult:
    """Bring the calendar in line with downloaded feeds ({region label: ics text})."""
    years = (now.year, now.year + 1)
    result = SyncResult(years=years)
    wanted: dict[str, tuple[Holiday, list[str], bool]] = {}
    for region, text in feeds.items():
        for h in parse_ics(text):
            if h.day.year not in years:
                continue
            h = Holiday(h.day, h.days, ALIASES.get(h.title, h.title), h.public, h.tentative)
            key = _key(h)
            if key in wanted:  # same holiday in two regions' lists
                first, regions, public = wanted[key]
                wanted[key] = (first, regions + [region], public or h.public)
            else:
                wanted[key] = (h, [region], h.public)

    for key, (h, regions, public) in sorted(wanted.items()):
        existing = repo.get_by_client_uuid(conn, key)
        notes = _notes(public, regions, h.tentative)
        if existing is None:
            start = local_midnight(h.day)
            repo.create_event(conn, repo.NewEvent(
                title=h.title, start=start, end=start + timedelta(days=h.days), all_day=True,
                notes=notes, source=SOURCE, client_uuid=key), now)
            result.added += 1
        elif existing.deleted:
            result.kept += 1  # the user deleted it: leave it deleted
        elif existing.notes != notes:
            repo.update_event(conn, existing.id, now, notes=notes)
            result.updated += 1
        else:
            result.kept += 1

    # Holidays in these years that are no longer listed (moved or dropped) go.
    first = local_midnight(date(years[0], 1, 1))
    last = local_midnight(date(years[-1] + 1, 1, 1))
    for event in repo.list_events(conn, first, last):
        if event.source == SOURCE and event.client_uuid not in wanted:
            repo.delete_event(conn, event.id, now)
            result.removed += 1
    return result


def fetch(regions: list[str], timeout: float = 20.0) -> tuple[dict[str, str], list[str]]:
    feeds, errors = {}, []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for region in regions:
            label = LABELS.get(region.lower().strip(), region.strip().title())
            try:
                r = client.get(feed_url(region))
                r.raise_for_status()
                if "BEGIN:VCALENDAR" not in r.text[:200]:
                    raise ValueError("not a calendar file")
                feeds[label] = r.text
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{label}: {e}")
    return feeds, errors


def sync_from_internet(conn: sqlite3.Connection, regions: list[str], now: datetime,
                       fetcher: Callable[[list[str]], tuple[dict[str, str], list[str]]] = fetch) -> SyncResult:
    feeds, errors = fetcher(regions)
    if not feeds:
        return SyncResult(years=(now.year, now.year + 1), errors=errors or ["nothing downloaded"])
    result = sync(conn, feeds, now)
    result.errors = errors
    if not errors:
        db.set_meta(conn, LAST_SYNC, now.isoformat())
    return result


def is_stale(conn: sqlite3.Connection, now: datetime, every: timedelta) -> bool:
    last = db.get_meta(conn, LAST_SYNC)
    if last is None:
        return True
    last_dt = datetime.fromisoformat(last)
    # also refresh as soon as a new year starts, so "next year" moves along
    return now - last_dt >= every or last_dt.year != now.year


def refresh_in_background(open_conn: Callable[[], sqlite3.Connection], regions: list[str],
                          clock: Callable[[], datetime], every: timedelta,
                          log: Callable[[str], None] = print) -> threading.Thread:
    """Check now and then every few hours; fetch only when the last sync is old."""

    def run() -> None:
        while True:
            try:
                conn = open_conn()
                try:
                    if regions and is_stale(conn, clock(), every):
                        log(f"  {sync_from_internet(conn, regions, clock()).summary()}")
                finally:
                    conn.close()
            except Exception as e:  # never take the assistant down over holidays
                log(f"  holidays: refresh failed: {e!r}")
            threading.Event().wait(6 * 3600)

    thread = threading.Thread(target=run, name="holidays", daemon=True)
    thread.start()
    return thread
