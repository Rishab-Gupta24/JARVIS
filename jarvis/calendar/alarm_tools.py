"""Alarm and timer tools for the model. Like the calendar, the model copies the user's words
("7", "tomorrow", "10 minutes") and Python works out the times (alarms.py).

Offered only for the user's own requests (like PC control), never for shared messages."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from jarvis.calendar import alarms
from jarvis.core.dates import DateParseError, spoken_time
from jarvis.core.toolkit import Args, Tool, ToolContext, ToolError, ToolResult, spec, string

NAMES = {"set_alarm", "set_timer", "list_alarms", "cancel_alarm", "stop_alarm"}


class AlarmArgs(Args):
    time: str = Field(min_length=1, max_length=40)
    date: str | None = Field(default=None, max_length=60)
    label: str | None = Field(default=None, max_length=120)
    repeat: str | None = Field(default=None, max_length=20)


class TimerArgs(Args):
    duration: str = Field(min_length=1, max_length=60)
    label: str | None = Field(default=None, max_length=120)


class CancelArgs(Args):
    time: str | None = Field(default=None, max_length=40)
    label: str | None = Field(default=None, max_length=120)
    kind: str | None = Field(default=None, max_length=10)
    all: bool = False


class StopArgs(Args):
    snooze_minutes: int | None = Field(default=None, ge=1, le=60)


class NoArgs(Args):
    pass


def _guard(ctx: ToolContext) -> None:
    if not ctx.allow_pc:
        raise ToolError("Alarms can only be set when the user asks directly.")


def _repeat(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if "weekday" in t or "work" in t or "monday to friday" in t:
        return "weekdays"
    if any(w in t for w in ("daily", "every day", "everyday", "each day", "every morning", "every night")):
        return "daily"
    return None


_NOT_A_LABEL = {"wake up", "wakeup", "wake me up", "wake me", "alarm", "my alarm", "get up", "morning", "reminder"}


def _label(label: str | None) -> str:
    """The model fills label with 'wake up' for 'wake me up at 7' (seen live); that says nothing."""
    text = (label or "").strip()
    return "" if text.lower().strip(".!") in _NOT_A_LABEL else text


def set_alarm(args: AlarmArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    repeat = _repeat(args.repeat) or _repeat(ctx.raw_text)
    try:
        at = alarms.when_alarm(args.time, None if repeat else args.date, ctx.now, repeat)
    except DateParseError as e:
        raise ToolError(str(e)) from None
    alarm = alarms.create(ctx.conn, at, ctx.now, _label(args.label), repeat, raw_text=ctx.raw_text)
    left = at - ctx.now
    soon = f", that's in {alarms.spoken_length(timedelta(minutes=round(left.total_seconds() / 60)))}" \
        if timedelta(minutes=1) <= left < timedelta(hours=12) and not repeat else ""
    return ToolResult(True, {"alarm": alarm.id, "at": at.isoformat()},
                      f"Alarm set for {alarm.spoken(ctx.now)}{soon}.", mutated=True)


def set_timer(args: TimerArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    try:
        length = alarms.duration(args.duration)
    except DateParseError as e:
        raise ToolError(str(e)) from None
    at = ctx.now + length
    alarms.create(ctx.conn, at, ctx.now, args.label or "", kind="timer", raw_text=ctx.raw_text)
    what = f"{args.label} timer" if args.label else "Timer"
    return ToolResult(True, {"ends": at.isoformat()},
                      f"{what[:1].upper()}{what[1:]} set for {alarms.spoken_length(length)}.", mutated=True)


def list_alarms(args: NoArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    found = alarms.upcoming(ctx.conn)
    if not found:
        return ToolResult(True, {"alarms": []}, "You don't have any alarms or timers set.")
    said = []
    for a in found:
        if a.kind == "timer":
            left = a.fire_at - ctx.now
            said.append(f"a {a.label + ' ' if a.label else ''}timer with "
                        f"{alarms.spoken_length(timedelta(seconds=max(60, round(left.total_seconds() / 60) * 60)))} left")
        else:
            said.append(f"an alarm at {a.spoken(ctx.now)}")
    text = said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
    return ToolResult(True, {"alarms": [a.fire_at.isoformat() for a in found]}, f"You have {text}.")


def cancel_alarm(args: CancelArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    pending = alarms.upcoming(ctx.conn)
    kind = args.kind if args.kind in ("alarm", "timer") else None
    found = [a for a in pending if kind is None or a.kind == kind] if args.all else \
        alarms.matching(pending, args.time, args.label, kind)
    if not found and not args.all and not args.time and not args.label and len(pending) == 1:
        found = pending  # "cancel my alarm" with just one set
    if not found:
        if not pending:
            return ToolResult(True, {"cancelled": 0}, "You don't have any alarms or timers set.")
        raise ToolError("No alarm matches that. Call list_alarms and ask the user which one.")
    if len(found) > 1 and not args.all:
        options = ", ".join(a.spoken(ctx.now) for a in found)
        return ToolResult(True, {"cancelled": 0}, f"You have {len(found)} that match: {options}. Which one?")
    alarms.cancel(ctx.conn, [a.id for a in found])
    if len(found) == 1:
        a = found[0]
        what = f"the {a.label + ' ' if a.label else ''}timer" if a.kind == "timer" else \
            f"your alarm for {a.spoken(ctx.now)}"
        return ToolResult(True, {"cancelled": 1}, f"Cancelled {what}.", mutated=True)
    return ToolResult(True, {"cancelled": len(found)}, f"Cancelled {len(found)} alarms and timers.", mutated=True)


def stop_alarm(args: StopArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    alarms.silence()
    if args.snooze_minutes:
        at = ctx.now + timedelta(minutes=args.snooze_minutes)
        alarms.create(ctx.conn, at, ctx.now, "snooze", raw_text=ctx.raw_text)
        return ToolResult(True, {"snoozed": at.isoformat()},
                          f"Snoozing. I'll ring again at {spoken_time(at.time())}.", mutated=True)
    return ToolResult(True, {"stopped": True}, "Alarm off.")


SPECS = [
    spec("set_alarm", "Set an alarm that rings on the PC and the user's phone ('wake me up at 7', 'alarm for 6:30 "
         "tomorrow', 'alarm every weekday at 7'). Copy the user's own words; don't convert times.",
         {"time": string("The time as the user said it, e.g. '7', '6:30 am', 'noon'."),
          "date": string("The day only if the user said one, e.g. 'tomorrow', 'friday'."),
          "label": string("What it's for, only if the user said, e.g. 'gym'."),
          "repeat": string("'daily' or 'weekdays', only if the user said it repeats.")}, ("time",)),
    spec("set_timer", "Start a countdown timer ('set a timer for 10 minutes', 'remind me in half an hour').",
         {"duration": string("How long, as the user said it, e.g. '10 minutes', 'an hour and a half'."),
          "label": string("What it's for, only if the user said, e.g. 'pasta'.")}, ("duration",)),
    spec("list_alarms", "Which alarms and timers are set ('what alarms do I have?', 'how long is left on my "
         "timer?').", {}),
    spec("cancel_alarm", "Cancel (delete) an alarm or timer that is set but hasn't rung yet.",
         {"time": string("The alarm's time, if the user said it, e.g. '7 am'."),
          "label": string("What it was for, if the user said, e.g. 'gym' or 'pasta'."),
          "kind": string("'alarm' or 'timer', if the user said which."),
          "all": {"type": "boolean", "description": "True only if the user said all of them."}}),
    spec("stop_alarm", "Stop an alarm that is ringing right now, or snooze it ('stop', 'snooze for 5 minutes').",
         {"snooze_minutes": {"type": "integer", "description": "Minutes to snooze, only if the user said snooze. "
                             "'snooze' alone means 9."}}),
]

TOOLS: dict[str, Tool] = {
    "set_alarm": (AlarmArgs, set_alarm), "set_timer": (TimerArgs, set_timer), "list_alarms": (NoArgs, list_alarms),
    "cancel_alarm": (CancelArgs, cancel_alarm), "stop_alarm": (StopArgs, stop_alarm),
}
