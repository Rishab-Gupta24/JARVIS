"""Date and time phrase resolution. Today is Thursday 5 November 2026."""

from __future__ import annotations

from datetime import date, time

import pytest

from conftest import NOW, TODAY, nov
from jarvis.core.dates import (
    DateParseError,
    resolve_date,
    resolve_period,
    resolve_time,
    resolve_time_range,
    resolve_when,
    spoken_date,
    spoken_time,
)


@pytest.mark.parametrize("text, expected", [
    ("today", nov(5)),
    ("tonight", nov(5)),
    ("this evening", nov(5)),
    ("tomorrow", nov(6)),
    ("Tomorrow morning", nov(6)),
    ("tmrw", nov(6)),
    ("day after tomorrow", nov(7)),
    ("the day after tomorrow", nov(7)),
    ("yesterday", nov(4)),
    ("in 3 days", nov(8)),
    ("in a week", nov(12)),
    ("in two weeks", nov(19)),
    ("a week from today", nov(12)),
    ("in a couple of days", nov(7)),
    # bare weekday: the next one after today
    ("monday", nov(9)),
    ("Monday", nov(9)),
    ("on mon", nov(9)),
    ("friday", nov(6)),
    ("thursday", nov(12)),
    # "this"/"coming": may be today
    ("this thursday", nov(5)),
    ("this friday", nov(6)),
    ("this Saturday", nov(7)),
    ("coming sunday", nov(8)),
    ("friday this week", nov(6)),
    # "next": in next week (weeks start Monday)
    ("next friday", nov(13)),
    ("Next Friday", nov(13)),
    ("next monday", nov(9)),
    ("next thursday", nov(12)),
    ("friday next week", nov(13)),
    ("next week friday", nov(13)),
    ("next week on tuesday", nov(10)),
    ("wednesday after next", nov(18)),
    ("last friday", date(2026, 10, 30)),
    ("next week", nov(9)),
    ("this weekend", nov(7)),
    ("next weekend", nov(14)),
    ("next month", date(2026, 12, 1)),
    # explicit dates, day/month order
    ("20 november", nov(20)),
    ("20 Nov", nov(20)),
    ("November 20th", nov(20)),
    ("the 20th of November", nov(20)),
    ("20/11", nov(20)),
    ("20/11/2026", nov(20)),
    ("20-11-26", nov(20)),
    ("2026-11-20", nov(20)),
    ("Monday 9 Nov", nov(9)),
    ("mon, 9th november", nov(9)),
    ("3 december", date(2026, 12, 3)),
    ("1 nov", nov(1)),                       # 4 days ago: stays this year
    ("15 sept", date(2027, 9, 15)),          # well past: next year
    ("1 jan", date(2027, 1, 1)),
    ("12th", nov(12)),                       # day only: this month...
    ("the 3rd", date(2026, 12, 3)),          # ...or next month if already past
    ("on the 5th", nov(5)),
    ("by sunday", nov(8)),
])
def test_resolve_date(text, expected):
    assert resolve_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", "blursday", "31 feb", "32/11", "the 40th", "someday"])
def test_resolve_date_rejects(text):
    with pytest.raises(DateParseError):
        resolve_date(text, TODAY)


@pytest.mark.parametrize("text, expected", [
    ("10am", time(10)),
    ("10 am", time(10)),
    ("10 a.m.", time(10)),
    ("10:30pm", time(22, 30)),
    ("10.30 pm", time(22, 30)),
    ("4:30 PM", time(16, 30)),
    ("14:00", time(14)),
    ("14:00 hrs", time(14)),
    ("1430", time(14, 30)),
    ("noon", time(12)),
    ("12 noon", time(12)),
    ("midnight", time(23, 59)),
    ("12am", time(0)),
    ("12pm", time(12)),
    ("morning", time(9)),
    ("afternoon", time(15)),
    ("evening", time(18)),
    ("tonight", time(20)),
    ("5", time(17)),              # bare 1-7 means pm
    ("5:30", time(17, 30)),
    ("7", time(19)),
    ("8", time(8)),               # bare 8-11 means am
    ("9", time(9)),
    ("12", time(12)),
    ("07:00", time(7)),           # leading zero means 24-hour
    ("6 o'clock", time(18)),
    ("at 11:59pm", time(23, 59)),
])
def test_resolve_time(text, expected):
    assert resolve_time(text) == expected


@pytest.mark.parametrize("text", ["25:00", "13pm", "10:75", "whenever", "0pm"])
def test_resolve_time_rejects(text):
    with pytest.raises(DateParseError):
        resolve_time(text)


@pytest.mark.parametrize("start, end, expected", [
    ("2", "3pm", (time(14), time(15))),
    ("8", "10pm", (time(20), time(22))),
    ("11", "1pm", (time(11), time(13))),
    ("6", "9am", (time(6), time(9))),
    ("9", "5", (time(9), time(17))),
    ("7pm", "9", (time(19), time(21))),
    ("10am", None, (time(10), None)),
])
def test_resolve_time_range(start, end, expected):
    assert resolve_time_range(start, end) == expected


@pytest.mark.parametrize("date_text, time_text, expected", [
    ("tomorrow at 4:30 pm", None, (nov(6), "4:30 pm")),
    ("monday 10am", None, (nov(9), "10am")),
    ("tonight", None, (nov(5), "tonight")),
    ("tomorrow morning", None, (nov(6), "morning")),
    ("2026-11-09T10:00:00", None, (nov(9), "10:00")),
    ("2026-11-09T10:00:00+04:00", None, (nov(9), "10:00")),
    ("friday", "3pm", (nov(6), "3pm")),
    ("tonight", "9pm", (nov(5), "9pm")),        # an explicit time wins
    ("", "5pm", (nov(5), "5pm")),               # no day, time still ahead: today
    ("", "8am", (nov(6), "8am")),               # no day, time passed: tomorrow
])
def test_resolve_when(date_text, time_text, expected):
    assert resolve_when(date_text, time_text, NOW) == expected


def test_resolve_when_needs_something():
    with pytest.raises(DateParseError):
        resolve_when("", None, NOW)


@pytest.mark.parametrize("text, expected", [
    ("today", (nov(5), nov(5))),
    ("tomorrow", (nov(6), nov(6))),
    ("this week", (nov(5), nov(8))),
    ("next week", (nov(9), nov(15))),
    ("this weekend", (nov(7), nov(8))),
    ("next weekend", (nov(14), nov(15))),
    ("this month", (nov(5), nov(30))),
    ("next month", (date(2026, 12, 1), date(2026, 12, 31))),
    ("12 nov to 15 nov", (nov(12), nov(15))),
    ("from monday to wednesday", (nov(9), nov(11))),
    ("between today and sunday", (nov(5), nov(8))),
    ("next 3 days", (nov(5), nov(8))),
    ("next friday", (nov(13), nov(13))),
])
def test_resolve_period(text, expected):
    assert resolve_period(text, TODAY) == expected


def test_resolve_period_backwards():
    with pytest.raises(DateParseError):
        resolve_period("friday to monday 2 nov", TODAY)


def test_spoken():
    assert spoken_date(nov(5), TODAY) == "today"
    assert spoken_date(nov(6), TODAY) == "tomorrow"
    assert spoken_date(nov(9), TODAY) == "Monday 9 November"
    assert spoken_date(date(2027, 1, 4), TODAY) == "Monday 4 January 2027"
    assert spoken_time(time(10)) == "10 am"
    assert spoken_time(time(16, 30)) == "4:30 pm"
    assert spoken_time(time(12)) == "noon"
    assert spoken_time(time(0)) == "midnight"
    assert spoken_time(time(23, 59)) == "11:59 pm"
