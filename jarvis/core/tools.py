"""Tools the model can call. Each one validates its arguments in Python before
touching the calendar; the model never writes to the database itself.
Document tools live in jarvis/documents/tools.py and are registered here.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import Field

from jarvis.calendar import repo
from jarvis.calendar.repo import Event, local_midnight
from jarvis.config import TZ
from jarvis.core.dates import (
    resolve_date,
    resolve_period,
    resolve_time,
    resolve_time_range,
    resolve_when,
    spoken_date,
    spoken_time,
)
from jarvis.core.toolkit import Args as _Args
from jarvis.core.toolkit import Tool, ToolContext, ToolError, ToolResult
from jarvis.core.toolkit import run as _run
from jarvis.core.toolkit import spec as _fn
from jarvis.core.toolkit import string as _s
from jarvis.calendar import alarm_tools
from jarvis.core import web
from jarvis.documents import tools as document_tools
from jarvis.pc import tools as pc_tools

__all__ = ["TOOL_SPECS", "ToolContext", "ToolError", "ToolResult", "execute"]

DEFAULT_DURATION = timedelta(hours=1)
MAX_WRAP = timedelta(hours=16)  # an end time "before" the start may run past midnight, up to this long


# ---------- argument models ----------

class CreateArgs(_Args):
    title: str = Field(min_length=1, max_length=200)
    date: str
    start_time: str | None = None
    end_time: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=24 * 60)
    location: str | None = None
    notes: str | None = None
    important: bool = False


class ListArgs(_Args):
    period: str = "today"


class MatchArgs(_Args):
    event_id: int | None = None
    match_title: str | None = None
    match_date: str | None = None


class UpdateArgs(MatchArgs):
    new_title: str | None = Field(default=None, max_length=200)
    new_date: str | None = None
    new_start_time: str | None = None
    new_end_time: str | None = None
    new_duration_minutes: int | None = Field(default=None, gt=0, le=24 * 60)
    new_location: str | None = None
    new_notes: str | None = None
    important: bool | None = None


class ImportantArgs(MatchArgs):
    important: bool = True


# ---------- helpers ----------

def _tidy_title(title: str) -> str:
    title = " ".join(title.split()).strip(" .")
    return title[:1].upper() + title[1:] if title[:1].islower() else title


def _brief(event: Event, today: date) -> dict:
    d = {"id": event.id, "title": event.title, "when": event.when(today)}
    if not event.all_day:
        d["ends"] = event.end.strftime("%H:%M")
    if event.location:
        d["location"] = event.location
    if event.important:
        d["important"] = True
    return d


def _join(parts: list[str]) -> str:
    text = " and ".join(parts)
    return text[:1].upper() + text[1:] + "."


def _event_times(
    day: date, start_text: str | None, end_text: str | None, duration_min: int | None
) -> tuple[datetime, datetime, bool]:
    """Build start/end for a new event. No time at all means an all-day event."""
    if start_text is None and end_text:
        # "by Sunday 11:59pm": only an end was given, so it's a deadline at that time.
        start_text, end_text = end_text, None
    if start_text is None:
        start = local_midnight(day)
        return start, start + timedelta(days=1), True
    st, et = resolve_time_range(start_text, end_text)
    start = datetime.combine(day, st, TZ)
    if et is not None:
        end = _end_on(start, et)
    elif duration_min:
        end = start + timedelta(minutes=duration_min)
    else:
        # Default length, but a late event without an end doesn't spill into tomorrow.
        end = min(start + DEFAULT_DURATION, local_midnight(day + timedelta(days=1)))
    return start, end, False


_IMPORTANCE_CUES = re.compile(
    r"\b(important|imp|urgent|remind|reminder|forget|priority|critical|crucial|asap|must|"
    r"vital|essential|mandatory|alert|flag)", re.I
)


def _asked_important(raw_text: str | None) -> bool:
    """The model over-flags things like due dates; only honour important=true if the user asked.
    With no raw words (direct API calls) there's nothing to check, so trust the caller."""
    return raw_text is None or bool(_IMPORTANCE_CUES.search(raw_text))


def _mentioned(phrase: str, raw_text: str | None) -> bool:
    """Did the user's own words contain this day phrase? Unknown words count as yes."""
    if raw_text is None:
        return True
    said = set(re.findall(r"[a-z0-9]+", raw_text.lower()))
    words = [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in ("on", "the", "this", "next")]
    return bool(words) and all(w in said for w in words)


def _end_on(start: datetime, end_time) -> datetime:
    end = datetime.combine(start.date(), end_time, TZ)
    if end <= start:
        end += timedelta(days=1)
        if end - start > MAX_WRAP:
            raise ToolError("The end time is before the start time.")
    return end


def _find_target(args: MatchArgs, ctx: ToolContext) -> Event:
    if args.event_id is not None:
        event = repo.get_event(ctx.conn, args.event_id)
        if event is None:
            raise ToolError(f"There is no event with id {args.event_id}.")
        return event
    if not args.match_title and not args.match_date:
        raise ToolError("Say which event: give match_title (words from its title) and/or match_date.")
    day = resolve_date(args.match_date, ctx.today) if args.match_date else None
    if args.match_title:
        found = repo.find_events(ctx.conn, args.match_title, ctx.now, day)
        if not found and day is not None and not _mentioned(args.match_date, ctx.raw_text):
            # The model added a day the user never said; search without it.
            found = repo.find_events(ctx.conn, args.match_title, ctx.now)
            day = None
    else:
        found = [(1.0, e) for e in repo.list_on_day(ctx.conn, day)]
    if not found:
        what = f"'{args.match_title}'" if args.match_title else "anything"
        on = f" on {spoken_date(day, ctx.today)}" if day else ""
        raise ToolError(f"I couldn't find {what}{on} in the calendar.")
    if len(found) == 1 or found[0][0] - found[1][0] >= 0.25:
        return found[0][1]
    options = "; ".join(f"id {e.id}: {e.title}, {e.when(ctx.today)}" for _, e in found[:5])
    raise ToolError(f"More than one event matches. Ask the user which one they mean: {options}.")


# ---------- tools ----------

def create_event(args: CreateArgs, ctx: ToolContext) -> ToolResult:
    day, start_text = resolve_when(args.date, args.start_time, ctx.now)
    start, end, all_day = _event_times(day, start_text, args.end_time, args.duration_minutes)
    client_uuid = None
    if ctx.client_key:  # offline item: a stable id per event, so a retry can't add it twice
        ctx.created += 1
        client_uuid = f"{ctx.client_key}:{ctx.created}"
    event = repo.create_event(
        ctx.conn,
        repo.NewEvent(
            title=_tidy_title(args.title), start=start, end=end, all_day=all_day,
            location=args.location, notes=args.notes,
            important=args.important and _asked_important(ctx.raw_text),
            source=ctx.source, raw_text=ctx.raw_text, client_uuid=client_uuid,
        ),
        ctx.now,
    )
    confirmation = f"Added {event.title}, {event.when(ctx.today)}"
    confirmation += ", marked important." if event.important else "."
    return ToolResult(True, {"created": _brief(event, ctx.today)}, confirmation, True, [event.id])


def list_events(args: ListArgs, ctx: ToolContext) -> ToolResult:
    first, last = resolve_period(args.period, ctx.today)
    events = repo.list_events(ctx.conn, local_midnight(first), local_midnight(last + timedelta(days=1)))
    period = spoken_date(first, ctx.today)
    if last != first:
        period = f"{period} to {spoken_date(last, ctx.today)}"
    return ToolResult(True, {"period": period, "events": [_brief(e, ctx.today) for e in events]})


class FindArgs(_Args):
    query: str = Field(min_length=1, max_length=100)


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def find_event(args: FindArgs, ctx: ToolContext) -> ToolResult:
    """'When is Diwali?' -> the nearest upcoming event with that name (festivals included)."""
    found = repo.find_events(ctx.conn, args.query, ctx.now)
    if not found:
        return ToolResult(True, {"found": []}, f"I couldn't find {args.query} in your calendar.")
    q = _words(args.query)
    # "Diwali" means Diwali, not Choti Diwali; "choti diwali" matches "Choti Diwali (Naraka Chaturdasi)".
    exact = [e for _, e in found if q in (_words(e.title), _words(e.title.split("(")[0]))]
    pool = exact or [e for score, e in found if score == found[0][0]]
    nearest: dict[str, Event] = {}
    for e in pool:  # already nearest-first: keep one date per name
        nearest.setdefault(e.title, e)
    picks = sorted(nearest.values(), key=lambda e: e.start)[:3]

    def sentence(e: Event) -> str:
        when = e.when(ctx.today)
        on = when if when.split()[0] in ("today", "tomorrow", "yesterday") else f"on {when}"
        verb = "was" if e.end <= ctx.now else "is"
        return f"{e.title} {verb} {on}"

    said = [sentence(e) for e in picks]
    answer = (said[0] if len(said) == 1 else ", ".join(said[:-1]) + (", and " if len(said) > 2 else " and ")
              + said[-1]) + "."
    return ToolResult(True, {"found": [_brief(e, ctx.today) for e in picks]}, answer)


_END_CUES = re.compile(r"\b(until|till|til|ends?|ending|finish(es)?|over by|extend|longer|shorter)\b", re.I)


def update_event(args: UpdateArgs, ctx: ToolContext) -> ToolResult:
    if args.new_end_time and not args.new_start_time and ctx.raw_text is not None \
            and not _END_CUES.search(ctx.raw_text):
        # "Push it to 6pm" moves the start; the model sometimes sends it as the end time.
        args = args.model_copy(update={"new_start_time": args.new_end_time, "new_end_time": None})
    event = _find_target(args, ctx)
    changes: dict[str, Any] = {}
    if args.new_title:
        changes["title"] = _tidy_title(args.new_title)
    if args.new_location is not None:
        changes["location"] = args.new_location
    if args.new_notes is not None:
        changes["notes"] = args.new_notes
    if args.important is not None:
        changes["important"] = args.important

    if args.new_date or args.new_start_time or args.new_end_time or args.new_duration_minutes:
        changes.update(_moved_times(event, args, ctx))
    if not changes:
        raise ToolError("Nothing to change was given.")

    updated = repo.update_event(ctx.conn, event.id, ctx.now, **changes)

    parts = []
    if updated.title != event.title:
        parts.append(f"renamed {event.title} to {updated.title}")
    if (updated.start, updated.all_day) == (event.start, event.all_day) and updated.end != event.end:
        parts.append(f"{updated.title} now ends at {spoken_time(updated.end.time())}")
    elif (updated.start, updated.end, updated.all_day) != (event.start, event.end, event.all_day):
        parts.append(f"moved {updated.title} to {updated.when(ctx.today)}")
    if updated.location != event.location:
        parts.append(f"set the place for {updated.title} to {updated.location}")
    if updated.notes != event.notes:
        parts.append(f"updated the notes on {updated.title}")
    if updated.important != event.important:
        parts.append(f"marked {updated.title} important" if updated.important
                     else f"unmarked {updated.title} as important")
    confirmation = _join(parts) if parts else f"{updated.title} already looked like that."
    return ToolResult(True, {"updated": _brief(updated, ctx.today)}, confirmation, True, [updated.id])


def _moved_times(event: Event, args: UpdateArgs, ctx: ToolContext) -> dict:
    if args.new_date:
        day, start_text = resolve_when(args.new_date, args.new_start_time, ctx.now)
    else:
        day, start_text = event.start.date(), args.new_start_time
    length = event.end - event.start

    if start_text is None and not args.new_end_time and not args.new_duration_minutes:
        # Only the day moves: keep the time of day and the length.
        start = datetime.combine(day, event.start.time(), TZ)
        return {"start": start, "end": start + length, "all_day": event.all_day}
    if start_text is None and event.all_day:
        raise ToolError("Give a start time to turn this all-day event into a timed one.")

    if start_text is not None:
        st, et = resolve_time_range(start_text, args.new_end_time)
    else:
        st = event.start.time()
        et = resolve_time(args.new_end_time) if args.new_end_time else None
    start = datetime.combine(day, st, TZ)
    if et is not None:
        end = _end_on(start, et)
    elif args.new_duration_minutes:
        end = start + timedelta(minutes=args.new_duration_minutes)
    else:
        end = start + (DEFAULT_DURATION if event.all_day else length)
    return {"start": start, "end": end, "all_day": False}


def delete_event(args: MatchArgs, ctx: ToolContext) -> ToolResult:
    event = _find_target(args, ctx)
    repo.delete_event(ctx.conn, event.id, ctx.now)
    return ToolResult(True, {"deleted": _brief(event, ctx.today)},
                      f"Deleted {event.title}, {event.when(ctx.today)}.", True, [event.id])


def set_important(args: ImportantArgs, ctx: ToolContext) -> ToolResult:
    event = _find_target(args, ctx)
    updated = repo.update_event(ctx.conn, event.id, ctx.now, important=args.important)
    if args.important:
        confirmation = f"Marked {updated.title}, {updated.when(ctx.today)}, as important."
    else:
        confirmation = f"{updated.title} is no longer marked important."
    return ToolResult(True, {"updated": _brief(updated, ctx.today)}, confirmation, True, [updated.id])


# ---------- schemas shown to the model ----------

_MATCH = {
    "event_id": {"type": "integer", "description": "The event's id, if known from an earlier tool result."},
    "match_title": _s("Words from the existing event's title, e.g. 'dentist'."),
    "match_date": _s("The day the existing event is on, in the user's words, only if they said one, e.g. 'sunday'."),
}

TOOL_SPECS = [
    _fn("create_event", "Add a new event to the calendar.", {
        "title": _s("Short name of the event, with no date or time words, e.g. 'DBMS quiz'."),
        "date": _s("The day in the user's own words, e.g. 'tomorrow', 'monday', 'next friday', "
                   "'in 3 days', '20 november', 'the 12th'."),
        "start_time": _s("Start time in the user's words, e.g. '10am', '4:30 pm', '14:00', 'evening'. "
                         "Leave out when no time was said (all-day event or deadline)."),
        "end_time": _s("End time, only if the user said one, e.g. '3pm'."),
        "duration_minutes": {"type": "integer", "description": "Length in minutes, if the user gave a length."},
        "location": _s("Where it happens, if said."),
        "notes": _s("Other details worth keeping, if any."),
        "important": {"type": "boolean",
                      "description": "Only true if the user's words ask for it: 'important', 'urgent', "
                                     "'don't let me forget', 'remind me'. Due dates and exams are NOT "
                                     "important unless the user says so. Otherwise leave this out."},
    }, ("title", "date")),
    _fn("list_events", "List the events in a period, to answer questions about the schedule.", {
        "period": _s("The period in the user's words, e.g. 'today', 'tomorrow', 'this week', "
                     "'next monday', '12 nov to 15 nov'."),
    }, ("period",)),
    _fn("find_event", "Find when something is, by name: 'when is Diwali?', 'when is my dentist appointment?'. "
                      "The calendar includes festivals and public holidays.", {
        "query": _s("The name to look for, e.g. 'Diwali', 'choti diwali', 'Eid', 'dentist'."),
    }, ("query",)),
    _fn("update_event", "Change an existing event: move it to another day or time, rename it, "
                        "or change its place, notes or importance.", {
        **_MATCH,
        "new_title": _s("New title, only if renaming."),
        "new_date": _s("New day in the user's words, only if it moves to another day."),
        "new_start_time": _s("New start time in the user's words. Use this when the user moves, pushes, "
                             "shifts or reschedules the event to a time, e.g. 'push it to 6pm' -> '6pm'."),
        "new_end_time": _s("New end time, only if the user says when it should end, e.g. 'until 7pm'."),
        "new_duration_minutes": {"type": "integer", "description": "New length in minutes, if said."},
        "new_location": _s("New place, if said."),
        "new_notes": _s("New notes, if said."),
        "important": {"type": "boolean", "description": "Set only if the user changes importance."},
    }),
    _fn("delete_event", "Delete (cancel) an existing event.", _MATCH),
    _fn("set_important", "Mark an existing event as important (the user gets reminders) or not.", {
        **_MATCH,
        "important": {"type": "boolean", "description": "true to mark important (default), false to unmark."},
    }),
] + document_tools.SPECS + alarm_tools.SPECS + web.SPECS + pc_tools.SPECS

_TOOLS: dict[str, Tool] = {
    "create_event": (CreateArgs, create_event),
    "list_events": (ListArgs, list_events),
    "find_event": (FindArgs, find_event),
    "update_event": (UpdateArgs, update_event),
    "delete_event": (MatchArgs, delete_event),
    "set_important": (ImportantArgs, set_important),
    **document_tools.TOOLS,
    **alarm_tools.TOOLS,
    **web.TOOLS,
    **pc_tools.TOOLS,
}

PC_TOOLS = pc_tools.NAMES | alarm_tools.NAMES | web.NAMES  # only for the user's own requests


def specs_for(allow_pc: bool) -> list[dict]:
    """The tool list shown to the model; PC control only for the user's own requests."""
    return TOOL_SPECS if allow_pc else [s for s in TOOL_SPECS if s["function"]["name"] not in PC_TOOLS]


def execute(name: str, raw_args: Any, ctx: ToolContext) -> ToolResult:
    """Run one tool call. Problems come back as an error result for the model, never an exception."""
    return _run(_TOOLS, name, raw_args, ctx)
