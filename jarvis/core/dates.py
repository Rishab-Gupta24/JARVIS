"""Turn spoken date and time phrases into real dates, in the user's time zone.

The model copies the user's own words into tool arguments ("next friday",
"10am"); this module decides what they mean, so the rules live in one tested
place instead of inside the model. The rules are listed in CLAUDE.md under
"How dates are resolved".
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from jarvis.config import TZ


class DateParseError(ValueError):
    """The phrase could not be turned into a date or time."""


WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
COUNTS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}
PARTS_OF_DAY = {
    "morning": time(9), "noon": time(12), "midday": time(12), "lunch": time(13),
    "lunchtime": time(13), "afternoon": time(15), "evening": time(18),
    "tonight": time(20), "night": time(20),
    "midnight": time(23, 59),  # "by midnight" is a deadline at the end of that day
}
RELATIVE_DAYS = {
    "today": 0, "now": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1, "tomorow": 1,
    "day after tomorrow": 2, "overmorrow": 2, "yesterday": -1, "day before yesterday": -2,
}

_FILLER = {"on", "the", "at", "by", "of", "due", "before", "for", "around", "about"}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))

_WEEKDAY_RE = re.compile(
    rf"(?:(this coming|this|coming|upcoming|next|last) )?({_WD})(?: (this week|next week|after next))?"
)
_WEEK_THEN_WEEKDAY_RE = re.compile(rf"(this|next) week ({_WD})")
_IN_N_RE = re.compile(r"(?:in )?(?:a )?(\d+|[a-z]+) (days?|weeks?)(?: from (?:now|today)| later| time)?")
_TIME_IN_TEXT_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\b(?:noon|midday|midnight)\b"
)
_PART_OF_DAY_IN_TEXT_RE = re.compile(r"\b(morning|afternoon|evening|tonight|night)\b")
_ISO_DATETIME_RE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2})[t ](\d{1,2}:\d{2})(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?"
)


def now_local() -> datetime:
    return datetime.now(TZ)


def _clean(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", s)          # a.m. -> am
    s = re.sub(r"(\d)\.(\d{2})\b", r"\1:\2", s)             # 10.30 -> 10:30
    s = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s)    # 12th -> 12
    s = s.replace("o'clock", "").replace("oclock", "")
    s = re.sub(r"[,;!?]", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)                # stray full stops
    return " ".join(w for w in s.split() if w not in _FILLER)


def _count(word: str) -> int | None:
    return int(word) if word.isdigit() else COUNTS.get(word)


def _make(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise DateParseError(f"{day}/{month}/{year} is not a real date.") from None


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


_monday = monday_of


def _first_of_next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _infer_year(month: int, day: int, today: date) -> date:
    d = _make(today.year, month, day)
    if d < today - timedelta(days=30):
        d = _make(today.year + 1, month, day)
    return d


def _year(text: str | None, month: int, day: int, today: date) -> date:
    if text is None:
        return _infer_year(month, day, today)
    year = int(text)
    return _make(year + 2000 if year < 100 else year, month, day)


def resolve_date(text: str, today: date) -> date:
    """Resolve a day phrase such as "next friday" or "20 nov" to a date."""
    s = _clean(text)
    s = re.sub(r"\bthis (morning|afternoon|evening)\b", "today", s)
    s = re.sub(r"\btonight\b", "today", s)
    s = " ".join(w for w in s.split() if w not in ("morning", "afternoon", "evening", "night"))
    if not s:
        raise DateParseError(f"There's no day in {text!r}.")
    d = _resolve_clean_date(s, today)
    if d is None:
        raise DateParseError(f"I couldn't work out which day {text!r} means.")
    return d


def _resolve_clean_date(s: str, today: date) -> date | None:
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return _make(int(m[1]), int(m[2]), int(m[3]))
    if s in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[s])
    if (m := _IN_N_RE.fullmatch(s)) and (n := _count(m[1])) is not None:
        return today + timedelta(days=n * (7 if m[2].startswith("week") else 1))
    if s == "this week":
        return today
    if s == "next week":
        return _monday(today) + timedelta(days=7)
    if s in ("weekend", "this weekend"):
        return max(today, _monday(today) + timedelta(days=5))
    if s == "next weekend":
        return _monday(today) + timedelta(days=12)
    if s == "next month":
        return _first_of_next_month(today)

    if m := _WEEK_THEN_WEEKDAY_RE.fullmatch(s):
        s = f"{m[1]} {m[2]}"
    if m := _WEEKDAY_RE.fullmatch(s):
        prefix, wd, suffix = m[1], WEEKDAYS[m[2]], m[3]
        if suffix == "after next":
            return _monday(today) + timedelta(days=14 + wd)
        if prefix == "next" or suffix == "next week":
            return _monday(today) + timedelta(days=7 + wd)
        if prefix == "last":
            return today - timedelta(days=(today.weekday() - wd) % 7 or 7)
        if prefix or suffix == "this week":  # this / coming / upcoming: may be today
            return today + timedelta(days=(wd - today.weekday()) % 7)
        return today + timedelta(days=(wd - today.weekday()) % 7 or 7)  # bare: never today

    s = re.sub(rf"^(?:{_WD}) (?=\d|{_MON})", "", s)  # "monday 9 nov" -> "9 nov"
    if m := re.fullmatch(rf"(\d{{1,2}}) ({_MON})(?: (\d{{4}}))?", s):
        return _year(m[3], MONTHS[m[2]], int(m[1]), today)
    if m := re.fullmatch(rf"({_MON}) (\d{{1,2}})(?: (\d{{4}}))?", s):
        return _year(m[3], MONTHS[m[1]], int(m[2]), today)
    if m := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?", s):  # day/month
        return _year(m[3], int(m[2]), int(m[1]), today)
    if m := re.fullmatch(r"(\d{1,2})", s):  # "the 12th"
        day = int(m[1])
        year, month = today.year, today.month
        for _ in range(3):
            try:
                d = date(year, month, day)
            except ValueError:
                d = None
            if d is not None and d >= today:
                return d
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        raise DateParseError(f"No month has a day {day}.")
    return None


def _parse_time(text: str) -> tuple[time, bool]:
    """Return the time and whether am/pm was certain (said, or 24-hour)."""
    s = _clean(text)
    s = re.sub(r"\b(hrs|hours|h)\b", "", s).strip()
    s = s.replace("12 noon", "noon").replace("12 midnight", "midnight")
    if s in PARTS_OF_DAY:
        return PARTS_OF_DAY[s], True
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm)?", s)
    if not m and (m4 := re.fullmatch(r"(\d{2})(\d{2})", s)):  # 1430
        h, mi = int(m4[1]), int(m4[2])
        if h > 23 or mi > 59:
            raise DateParseError(f"{text!r} is not a real time.")
        return time(h, mi), True
    if not m:
        raise DateParseError(f"I couldn't understand the time {text!r}.")
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if mi > 59:
        raise DateParseError(f"{text!r} is not a real time.")
    if ap:
        if not 1 <= h <= 12:
            raise DateParseError(f"{text!r} is not a real time.")
        return time(h % 12 + (12 if ap == "pm" else 0), mi), True
    if h > 23:
        raise DateParseError(f"{text!r} is not a real time.")
    if m[1].startswith("0") or h == 0 or h >= 12:
        return time(h, mi), True
    if h <= 7:  # "at 5" means 5 pm
        return time(h + 12, mi), False
    return time(h, mi), False  # "at 9" means 9 am


def resolve_time(text: str) -> time:
    return _parse_time(text)[0]


def resolve_time_range(start_text: str, end_text: str | None = None) -> tuple[time, time | None]:
    """Resolve a start time and optional end time, using one to settle the other's am/pm."""
    st, st_sure = _parse_time(start_text)
    if not end_text:
        return st, None
    et, et_sure = _parse_time(end_text)
    if not st_sure:
        if st.hour < 12 and et.hour >= 12 and st.hour + 12 < et.hour:  # "8 to 10pm"
            st = st.replace(hour=st.hour + 12)
        elif st.hour >= 12 and et.hour < 12 and st.hour - 12 < et.hour:  # "6 to 9am"
            st = st.replace(hour=st.hour - 12)
    if not et_sure and et <= st and et.hour + 12 > st.hour and et.hour < 12:  # "7pm to 9"
        et = et.replace(hour=et.hour + 12)
    return st, et


def split_datetime(text: str) -> tuple[str, str | None]:
    """Split "tomorrow at 4pm" into ("tomorrow", "4pm"); the time part may be None."""
    s = _clean(text)
    if m := _ISO_DATETIME_RE.fullmatch(s):
        return m[1], m[2]
    if m := _TIME_IN_TEXT_RE.search(s):
        return (s[: m.start()] + s[m.end():]).strip(), m[0]
    if m := _PART_OF_DAY_IN_TEXT_RE.search(s):
        return s, m[1]
    return s, None


def resolve_when(date_text: str | None, time_text: str | None, now: datetime) -> tuple[date, str | None]:
    """Resolve a day phrase plus optional time phrase.

    Returns the date and the time phrase to use, which may have been found
    inside the day phrase ("tomorrow 4pm", "tonight"). With only a time, picks
    today if that time is still ahead, otherwise tomorrow.
    """
    today = now.date()
    d_text, implied = split_datetime(date_text or "")
    t_text = time_text or implied
    if d_text:
        return resolve_date(d_text, today), t_text
    if t_text:
        t = resolve_time(t_text)
        later_today = datetime.combine(today, t, TZ) > now
        return (today if later_today else today + timedelta(days=1)), t_text
    raise DateParseError("No day was given.")


def resolve_period(text: str, today: date) -> tuple[date, date]:
    """Resolve "this week", "tomorrow", "12 nov to 15 nov" etc. to an inclusive date range."""
    s = _clean(text)
    if m := re.fullmatch(r"(?:from |between )?(.+?) (?:to|until|till|through|thru|-|and) (.+)", s):
        a, b = resolve_date(m[1], today), resolve_date(m[2], today)
        if b < a:
            raise DateParseError(f"{text!r} ends before it starts.")
        return a, b
    monday = _monday(today)
    if s in ("this week", "week"):
        return today, monday + timedelta(days=6)
    if s == "next week":
        return monday + timedelta(days=7), monday + timedelta(days=13)
    if s in ("weekend", "this weekend"):
        return max(today, monday + timedelta(days=5)), monday + timedelta(days=6)
    if s == "next weekend":
        return monday + timedelta(days=12), monday + timedelta(days=13)
    if s == "this month":
        return today, _first_of_next_month(today) - timedelta(days=1)
    if s == "next month":
        first = _first_of_next_month(today)
        return first, _first_of_next_month(first) - timedelta(days=1)
    if s in ("upcoming", "coming days", "coming week", "soon", "next few days"):
        return today, today + timedelta(days=6)
    if (m := re.fullmatch(r"next (\d+|[a-z]+) days", s)) and (n := _count(m[1])) is not None:
        return today, today + timedelta(days=n)
    d = resolve_date(text, today)
    return d, d


def spoken_date(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if d == today - timedelta(days=1):
        return "yesterday"
    s = f"{d:%A} {d.day} {d:%B}"
    return s if d.year == today.year else f"{s} {d.year}"


def spoken_time(t: time) -> str:
    if t.hour == 12 and t.minute == 0:
        return "noon"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    h = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{h} {suffix}" if t.minute == 0 else f"{h}:{t.minute:02d} {suffix}"
