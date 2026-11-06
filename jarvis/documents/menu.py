"""Turn a mess menu into rows of (day, meal, items), and work out which week it covers.

Grids (PDF tables, spreadsheets, Markdown tables from a photo) are parsed in
plain Python, so a stored row is exactly what the cell said. Only menus written
as running text go through the model, and every dish it returns must appear in
the source text or it is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from jarvis.core.dates import MONTHS, WEEKDAYS, DateParseError, monday_of, resolve_date
from jarvis.core.llm import JsonModel
from jarvis.documents.readers import Table

MEALS = ("breakfast", "lunch", "snacks", "dinner")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MEAL_NAMES = {
    "breakfast": "breakfast", "break fast": "breakfast", "bf": "breakfast", "b fast": "breakfast",
    "b'fast": "breakfast", "morning breakfast": "breakfast",
    "lunch": "lunch", "afternoon lunch": "lunch",
    "snacks": "snacks", "snack": "snacks", "evening snacks": "snacks", "evening snack": "snacks",
    "high tea": "snacks", "hi tea": "snacks", "tea": "snacks", "evening tea": "snacks", "tiffin": "snacks",
    "dinner": "dinner", "supper": "dinner", "night dinner": "dinner",
}
_EMPTY_CELLS = {"-", "–", "—", "--", "x", "na", "n/a", "nil"}
_WD_RE = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MON_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class MenuRow:
    weekday: int  # Monday = 0
    date: date | None
    meal: str
    items: str


@dataclass(frozen=True)
class WeekInfo:
    valid_from: date | None = None
    valid_to: date | None = None
    repeats_weekly: bool | None = None

    @property
    def known(self) -> bool:
        return self.valid_from is not None


# ---------- cells ----------

def meal_of(cell: str) -> str | None:
    """'Breakfast (7:30-9:30 AM)' -> 'breakfast'. Must be just a meal name, so a dish
    such as 'Masala tea' is never mistaken for the 'tea' (snacks) label."""
    s = re.sub(r"\(.*?\)", " ", cell.lower())
    s = re.sub(r"\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\s*(?:-|–|to)\s*\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?", " ", s)
    s = " ".join(re.sub(r"[^a-z' ]", " ", s).split())
    for suffix in (" menu", " timings", " time"):
        s = s.removesuffix(suffix)
    return MEAL_NAMES.get(s)


def weekday_of(cell: str, today: date) -> tuple[int, date | None] | None:
    """'Monday', 'MON 9/11', 'Wednesday (11.11.2026)' -> (weekday, date or None).
    A cell with anything besides a weekday and a date ('Sunday special') is not a header."""
    s = cell.lower().strip()
    if not s or len(s) > 40:
        return None
    days = {WEEKDAYS[w] for w in re.findall(r"[a-z]+", s) if w in WEEKDAYS}
    if len(days) != 1:
        return None
    wd = days.pop()
    rest = re.sub(rf"\b(?:{_WD_RE})\b\.?", " ", s)
    rest = re.sub(r"(\d)\.(\d)", r"\1/\2", re.sub(r"[()\[\],]", " ", rest)).strip(" -:/.\n")
    if not rest:
        return wd, None
    try:
        d = resolve_date(rest, today)
    except DateParseError:
        return None
    return wd, d if d.weekday() == wd else None


def clean_items(cell: str) -> str:
    """Normalise one cell's text without changing its words.

    Line breaks in a cell that uses commas are wrapped lines ('Masala\\ndosa, chutney');
    in a cell with no commas they separate dishes ('Biryani\\nRaita' -> 'Biryani, Raita').
    """
    text = cell.replace("\r", "").strip()
    if text.lower() in _EMPTY_CELLS:
        return ""
    if "," in text:
        text = re.sub(r"\s*\n\s*", " ", text)
    else:
        text = ", ".join(line.strip() for line in text.split("\n") if line.strip())
    return " ".join(text.split()).strip(" ,;")


# ---------- grids ----------

def parse_tables(tables: list[Table], today: date) -> list[MenuRow]:
    """Every (day, meal) found across the tables, Monday breakfast first."""
    found: dict[tuple[int, date | None, str], list[str]] = {}
    for table in tables:
        width = max((len(r) for r in table), default=0)
        grid = [r + [""] * (width - len(r)) for r in table]
        for key, parts in (_days_across(grid, today) or _days_down(grid, today)).items():
            found.setdefault(key, []).extend(parts)
    rows = [MenuRow(wd, d, meal, ", ".join(parts)) for (wd, d, meal), parts in found.items() if parts]
    if len({r.weekday for r in rows}) < 3:
        return []  # not a weekly menu
    return sorted(rows, key=lambda r: (r.date or date.min, r.weekday, MEALS.index(r.meal)))


def _first_label(row: list[str], cols: list[int]) -> str:
    return next((row[c] for c in cols if row[c].strip()), "")


def _days_across(grid: Table, today: date) -> dict:
    """Days are columns, meals are rows (labels in a column on the left)."""
    for hi, row in enumerate(grid):
        day_cols = {c: wd for c, cell in enumerate(row) if (wd := weekday_of(cell, today))}
        if len(day_cols) >= 3:
            break
    else:
        return {}
    label_cols = [c for c in range(min(day_cols))]
    found: dict = {}
    meal = None
    for row in grid[hi + 1:]:
        if sum(weekday_of(cell, today) is not None for cell in row) >= 3:
            continue  # header repeated after a page break
        label = _first_label(row, label_cols)
        if label:
            meal = meal_of(label)  # a non-meal label ("Timings", "Note") ends the previous meal
        if meal is None:
            continue
        for c, (wd, d) in day_cols.items():
            if items := clean_items(row[c]):
                found.setdefault((wd, d, meal), []).append(items)
    return found


def _days_down(grid: Table, today: date) -> dict:
    """Days are rows, meals are columns (a header row of meal names)."""
    for hi, row in enumerate(grid):
        meal_cols = {c: m for c, cell in enumerate(row) if (m := meal_of(cell))}
        if len(meal_cols) >= 2:
            break
    else:
        return {}
    label_cols = [c for c in range(min(meal_cols))]
    found: dict = {}
    day = None
    for row in grid[hi + 1:]:
        label = _first_label(row, label_cols)
        if label:
            day = weekday_of(label, today)
        if day is None:
            continue
        for c, meal in meal_cols.items():
            if items := clean_items(row[c]):
                found.setdefault((day[0], day[1], meal), []).append(items)
    return found


# ---------- which week ----------

_PIECE = (
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s*(?:of\s+)?(?:{_MON_RE})\.?(?:,?\s*\d{{4}})?"
    rf"|(?:{_MON_RE})\.?\s*\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?"
    rf"|\d{{1,2}}[/.-]\d{{1,2}}(?:[/.-]\d{{2,4}})?)"
)
_SEP = r"\s*(?:-|–|—|to|till|until|through|thru)\s*"
_RANGE = re.compile(rf"\b({_PIECE}){_SEP}({_PIECE})\b", re.I)
_SHORT_RANGE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?{_SEP}(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MON_RE})\.?(?:,?\s*(\d{{4}}))?",
    re.I,
)
_WEEK_OF = re.compile(rf"\bweek\s+(?:of|starting|commencing|beginning|from)\s*:?\s*({_PIECE})", re.I)
_FROM = re.compile(
    rf"\b(?:w\.?\s?e\.?\s?f\.?|with effect from|effective from|valid from|applicable from|starting from)"
    rf"\s*:?\s*({_PIECE})",
    re.I,
)
_REPEATS = re.compile(
    r"\b(every week|each week|repeats?|repeating|rotat\w*|cyclic\w*|weekly cycle|same every week|"
    r"until further notice|regular menu)\b",
    re.I,
)


def _date(piece: str, today: date) -> date | None:
    try:
        return resolve_date(re.sub(r"(\d)\.(\d)", r"\1/\2", piece), today)
    except DateParseError:
        return None


def _has_year(piece: str) -> bool:
    return bool(re.search(r"\d{4}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2}", piece))


def detect_week(text: str, today: date) -> WeekInfo:
    """Find the week a menu covers from its title text, e.g. '9th Nov to 15th Nov 2026',
    '9-15 November', 'week of 9 Nov', 'w.e.f. 9/11' or 'repeats every week'."""
    for m in _RANGE.finditer(text):
        a, b = _date(m[1], today), _date(m[2], today)
        if a and b:
            if not _has_year(m[1]) and _has_year(m[2]):
                a = a.replace(year=b.year) if (a.month, a.day) <= (b.month, b.day) else a.replace(year=b.year - 1)
            if 0 <= (b - a).days <= 13:
                return WeekInfo(a, b, False)
    for m in _SHORT_RANGE.finditer(text):
        month = f"{m[3]} {m[4]}" if m[4] else m[3]
        a, b = _date(f"{m[1]} {month}", today), _date(f"{m[2]} {month}", today)
        if a and b and 0 <= (b - a).days <= 13:
            return WeekInfo(a, b, False)
    if (m := _WEEK_OF.search(text)) and (d := _date(m[1], today)):
        return WeekInfo(monday_of(d), monday_of(d) + timedelta(days=6), False)
    if (m := _FROM.search(text)) and (d := _date(m[1], today)):
        return WeekInfo(d, None, True)
    if _REPEATS.search(text):
        return WeekInfo(None, None, True)
    return WeekInfo()


def date_rows(rows: list[MenuRow], first: date, last: date) -> list[MenuRow]:
    """Give each weekday-only row its date inside a dated week (first..last, at most 7 days)."""
    if (last - first).days > 6:
        return rows
    out = []
    for r in rows:
        if r.date is None:
            d = first + timedelta(days=(r.weekday - first.weekday()) % 7)
            r = MenuRow(r.weekday, d if d <= last else None, r.meal, r.items)
        out.append(r)
    return out


# ---------- running text, through the model ----------

_MENU_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "enum": list(DAY_NAMES)},
                    "meals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "meal": {"type": "string", "enum": list(MEALS)},
                                "items": {"type": "string"},
                            },
                            "required": ["meal", "items"],
                        },
                    },
                },
                "required": ["day", "meals"],
            },
        }
    },
    "required": ["days"],
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", text.lower())


def grounded(items: str, source: str) -> bool:
    """At least 80% of the dish words appear in the source text."""
    words = _words(items)
    source_words = set(_words(source))
    return bool(words) and sum(w in source_words for w in words) / len(words) >= 0.8


def extract_with_model(text: str, llm: JsonModel) -> list[MenuRow]:
    answer = llm.json(
        [
            {"role": "system", "content": "You copy mess menus out of text into JSON. Never invent dishes."},
            {"role": "user", "content": (
                "Extract the mess menu from the text below. For each day and meal, copy the dishes "
                "exactly as written, separated by commas. Skip any day or meal the text doesn't list.\n\n"
                f"TEXT:\n{text[:12000]}"
            )},
        ],
        _MENU_SCHEMA,
    )
    rows = []
    for day in answer.get("days", []):
        if day.get("day") not in DAY_NAMES:
            continue
        for meal in day.get("meals", []):
            items = clean_items(str(meal.get("items", "")))
            if meal.get("meal") in MEALS and items and grounded(items, text):
                rows.append(MenuRow(DAY_NAMES.index(day["day"]), None, meal["meal"], items))
    return rows if len({r.weekday for r in rows}) >= 3 else []
