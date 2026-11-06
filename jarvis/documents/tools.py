"""Document tools the model can call.

Menu answers are built here from stored rows and spoken word for word; the
model only picks the tool and copies the user's day words. It never supplies
the dishes, so it can't guess them.
"""

from __future__ import annotations

import difflib
import re
from datetime import timedelta

from pydantic import Field

from jarvis.core.dates import resolve_date, resolve_period, spoken_date
from jarvis.core.toolkit import FOOD, Args, Tool, ToolContext, ToolError, ToolResult, spec, string
from jarvis.documents import store
from jarvis.documents.ingest import describe_menu, week_from_words
from jarvis.documents.menu import MEALS, meal_of

NO_MENU = "I don't have that week's menu."
_PLURAL = {"breakfast": "breakfast is", "lunch": "lunch is", "snacks": "snacks are", "dinner": "dinner is"}


class MenuArgs(Args):
    date: str = "today"
    meal: str | None = None


class FindDishArgs(Args):
    dish: str = Field(min_length=1, max_length=100)
    period: str = "upcoming"


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=300)


class WeekArgs(Args):
    week: str | None = None
    repeats_weekly: bool | None = None
    document_id: int | None = None


def _meal(arg: str | None) -> str | None:
    if not arg:
        return None
    if arg.lower() in MEALS:
        return arg.lower()
    return meal_of(arg)  # "evening snacks", "supper"; anything else ("food") means the whole day


def get_menu(args: MenuArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.mentions(FOOD):
        # "what's on tomorrow." (as Whisper writes it) sent the model here; that's the calendar.
        raise ToolError("The user didn't ask about food or the mess. For their schedule "
                        "('what's on', 'what do I have'), call list_events instead.")
    day = resolve_date(args.date, ctx.today)
    meal = _meal(args.meal)
    when = spoken_date(day, ctx.today)
    doc, rows = store.menu_for_day(ctx.conn, day)
    if doc is None:
        return ToolResult(True, {"date": day.isoformat(), "menu": None}, NO_MENU)
    if meal:
        rows = [r for r in rows if r.meal == meal]
    on_day = when if when in ("today", "tomorrow", "yesterday") else f"on {when}"
    if not rows:
        answer = f"The menu doesn't list {meal or 'anything'} {on_day}."
    elif meal:
        answer = f"{meal.capitalize()} {on_day}: {rows[0].items}."
    else:
        parts = "; ".join(f"{_PLURAL[r.meal]} {r.items}" for r in rows)
        answer = f"{on_day[:1].upper()}{on_day[1:]}, {parts}."
    data = {"date": day.isoformat(), "meals": {r.meal: r.items for r in rows}, "file": doc.filename}
    return ToolResult(True, data, answer)


def _dish_found(dish: str, items: str) -> bool:
    words = re.findall(r"[a-z0-9]+", items.lower())
    wanted = [w for w in re.findall(r"[a-z0-9]+", dish.lower()) if len(w) > 1]
    return bool(wanted) and all(
        any(w == q or (len(q) >= 4 and w.startswith(q)) or difflib.SequenceMatcher(None, w, q).ratio() >= 0.85
            for w in words)
        for q in wanted
    )


def find_in_menu(args: FindDishArgs, ctx: ToolContext) -> ToolResult:
    first, last = resolve_period(args.period, ctx.today)
    if (last - first).days > 31:
        raise ToolError("Ask about a shorter period, at most a month.")
    covered, hits = 0, []
    day = first
    while day <= last:
        doc, rows = store.menu_for_day(ctx.conn, day)
        if doc is not None:
            covered += 1
            hits += [(day, r) for r in rows if _dish_found(args.dish, r.items)]
        day += timedelta(days=1)
    period = spoken_date(first, ctx.today) if first == last else \
        f"{spoken_date(first, ctx.today)} to {spoken_date(last, ctx.today)}"
    if not covered:
        answer = NO_MENU
    elif not hits:
        answer = f"I don't see {args.dish} on the menu from {period}." if first != last else \
            f"I don't see {args.dish} on the menu for {period}."
    else:
        places = [f"{r.meal} on {spoken_date(d, ctx.today)} ({r.items})" for d, r in hits]
        answer = f"{args.dish[:1].upper()}{args.dish[1:]} is at " + ", and at ".join(places) + "."
    data = {"dish": args.dish, "found": [{"date": d.isoformat(), "meal": r.meal, "items": r.items} for d, r in hits]}
    return ToolResult(True, data, answer)


def search_documents(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    results = store.search(ctx.conn, args.query)
    if not results:
        return ToolResult(True, {"results": []}, "I couldn't find anything about that in your files.")
    return ToolResult(True, {"results": [
        {"file": doc.filename, "kind": doc.kind, "received": doc.received_at.date().isoformat(), "excerpt": excerpt}
        for doc, excerpt in results
    ]})


def set_menu_week(args: WeekArgs, ctx: ToolContext) -> ToolResult:
    if args.document_id is not None:
        doc = store.get(ctx.conn, args.document_id)
        if doc is None or doc.kind != "mess_menu":
            raise ToolError(f"There is no mess menu with id {args.document_id}.")
    else:
        waiting = store.waiting_for_week(ctx.conn)
        if not waiting:
            raise ToolError("No mess menu is waiting to be told its week.")
        doc = waiting[0]
    week = week_from_words(args.week, args.repeats_weekly, ctx.today)
    if not week.known:
        raise ToolError("Say which week the menu is for (e.g. 'next week'), or that it repeats every week.")
    doc = store.set_week(ctx.conn, doc.id, week)
    meals = len(store.rows_of(ctx.conn, doc.id))
    return ToolResult(True, {"document": doc.to_dict()}, describe_menu(doc, meals, ctx.today), mutated=True)


SPECS = [
    spec("get_menu", "Look up the mess menu for a day: what's for breakfast, lunch, snacks or dinner.", {
        "date": string("The day in the user's own words, e.g. 'today', 'tomorrow', 'this wednesday', "
                       "'next monday'. Default today."),
        "meal": string("breakfast, lunch, snacks or dinner. Leave out for the whole day."),
    }),
    spec("find_in_menu", "Find when a dish is on the mess menu, e.g. 'when is biryani this week?'.", {
        "dish": string("The dish, e.g. 'biryani'."),
        "period": string("The period in the user's words, e.g. 'this week', 'next week'. "
                         "Leave out for the next 7 days."),
    }, ("dish",)),
    spec("search_documents", "Search the files the user has sent (notices, timetables, exam schedules and "
                             "others) to answer a question about them.", {
        "query": string("Key words to look for, e.g. 'laundry days'."),
    }, ("query",)),
    spec("set_menu_week", "Record which week a mess menu the user sent is for, or that it repeats every week. "
                          "Use when the user answers that question.", {
        "week": string("The week in the user's words, e.g. 'this week', 'next week', '9 november'."),
        "repeats_weekly": {"type": "boolean", "description": "true if the user says it repeats every week."},
        "document_id": {"type": "integer", "description": "The menu's document id, if known."},
    }),
]

TOOLS: dict[str, Tool] = {
    "get_menu": (MenuArgs, get_menu),
    "find_in_menu": (FindDishArgs, find_in_menu),
    "search_documents": (SearchArgs, search_documents),
    "set_menu_week": (WeekArgs, set_menu_week),
}
