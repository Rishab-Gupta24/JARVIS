"""Milestone 1 done-criterion: typed phrases, through the real local model, land
as the right events. Needs Ollama running with the configured model; skipped
otherwise. Today is Thursday 5 November 2026, 09:00 in Dubai.

Run with:  .venv\\Scripts\\python -m pytest -m llm -v
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import NOW, all_events, at, nov, seed
from jarvis.calendar import repo
from jarvis.core import agent

pytestmark = pytest.mark.llm


def ask(conn, llm, text: str) -> str:
    return agent.handle(text, conn, llm, now=NOW).text


def only_new_event(conn) -> repo.Event:
    events = all_events(conn)
    assert len(events) == 1, [(e.title, e.start) for e in events]
    return events[0]


def check(ev: repo.Event, words: str, day: date, start=None, end=None, important=False, all_day=None):
    title = ev.title.lower()
    assert all(w in title for w in words.split()), ev.title
    assert ev.start.date() == day, ev.start
    if start is not None:
        assert not ev.all_day and ev.start == at(day, *start), ev.start
    if end is not None:
        assert ev.end == at(day, *end), ev.end
    if all_day is not None:
        assert ev.all_day == all_day
    assert ev.important == important


# ---------- creating ----------

CREATE_CASES = [
    ("DBMS quiz Monday 10am, important",
     dict(words="dbms quiz", day=nov(9), start=(10, 0), important=True)),
    ("Dentist appointment tomorrow at 4:30 pm",
     dict(words="dentist", day=nov(6), start=(16, 30))),
    ("Team meeting next Friday from 2 to 3pm",
     dict(words="team meeting", day=nov(13), start=(14, 0), end=(15, 0))),
    ("Lunch with Aisha this Saturday at 1pm",
     dict(words="lunch aisha", day=nov(7), start=(13, 0))),
    ("Physics lab report due on 20 November",
     dict(words="physics lab report", day=nov(20), all_day=True)),
    ("Gym in 3 days at 6pm",
     dict(words="gym", day=nov(8), start=(18, 0))),
    ("Call mom the day after tomorrow at 8pm",
     dict(words="call mom", day=nov(7), start=(20, 0))),
    ("Project review on the 12th at 11am for 2 hours",
     dict(words="project review", day=nov(12), start=(11, 0), end=(13, 0))),
    ("Movie night tonight at 9pm",
     dict(words="movie", day=nov(5), start=(21, 0))),
    ("Haircut next Tuesday at 5:30pm",
     dict(words="haircut", day=nov(10), start=(17, 30))),
    ("OS mid-sem exam on 3 December at 9am, mark it important",
     dict(words="os exam", day=date(2026, 12, 3), start=(9, 0), important=True)),
    ("Hackathon on Wednesday after next, it's important",
     dict(words="hackathon", day=nov(18), all_day=True, important=True)),
    ("Submit scholarship form by Sunday 11:59pm, don't let me forget",
     dict(words="scholarship", day=nov(8), start=(23, 59), important=True)),
]


@pytest.mark.parametrize("text, expected", CREATE_CASES, ids=[c[0] for c in CREATE_CASES])
def test_create(conn, ollama, text, expected):
    reply = ask(conn, ollama, text)
    check(only_new_event(conn), **expected)
    assert reply


def test_create_with_location(conn, ollama):
    ask(conn, ollama, "Coffee with Rahul at Starbucks tomorrow 10:30am")
    ev = only_new_event(conn)
    assert ev.start == at(nov(6), 10, 30)
    assert "starbucks" in ((ev.location or "") + ev.title).lower()


# ---------- editing ----------

def test_move_to_another_day(conn, ollama):
    ev = seed(conn, "DBMS quiz", nov(9), (10, 0))
    ask(conn, ollama, "Move the DBMS quiz to Tuesday")
    got = repo.get_event(conn, ev.id)
    assert got.start == at(nov(10), 10) and got.end == at(nov(10), 11)


def test_change_time(conn, ollama):
    ev = seed(conn, "Dentist appointment", nov(6), (16, 30))
    ask(conn, ollama, "Push my dentist appointment to 6pm")
    got = repo.get_event(conn, ev.id)
    assert got.start == at(nov(6), 18) and got.end == at(nov(6), 19)


def test_rename(conn, ollama):
    ev = seed(conn, "Team meeting", nov(13), (14, 0), (15, 0))
    ask(conn, ollama, "Rename the team meeting to Sprint planning")
    got = repo.get_event(conn, ev.id)
    assert got.title.lower() == "sprint planning" and got.start == at(nov(13), 14)


def test_mark_important(conn, ollama):
    ev = seed(conn, "Physics lab report", nov(20))
    ask(conn, ollama, "Mark the physics lab report as important")
    assert repo.get_event(conn, ev.id).important


# ---------- deleting ----------

def test_cancel_on_a_day(conn, ollama):
    ev = seed(conn, "Gym", nov(8), (18, 0))
    keep = seed(conn, "Haircut", nov(10), (17, 30))
    ask(conn, ollama, "Cancel gym on Sunday")
    assert repo.get_event(conn, ev.id) is None
    assert repo.get_event(conn, keep.id) is not None


def test_delete_by_name(conn, ollama):
    ev = seed(conn, "Lunch with Aisha", nov(7), (13, 0))
    ask(conn, ollama, "Delete lunch with Aisha")
    assert repo.get_event(conn, ev.id) is None


def test_delete_the_right_one_of_two(conn, ollama):
    fri = seed(conn, "Football practice", nov(6), (17, 0))
    sat = seed(conn, "Football practice", nov(7), (17, 0))
    ask(conn, ollama, "Cancel football practice on Saturday")
    assert repo.get_event(conn, sat.id) is None
    assert repo.get_event(conn, fri.id) is not None


# ---------- questions ----------

@pytest.mark.parametrize("question", [
    "What's on tomorrow?",
    "what's on tomorrow.",          # exactly as Whisper transcribed it by voice
    "what do i have tomorrow",
    "Am I busy tomorrow?",
])
def test_whats_on_tomorrow(conn, ollama, tmp_path, question):
    from conftest import ingest_fixture

    ingest_fixture(conn, "mess_menu_rotating.xlsx", tmp_path)  # a menu covers tomorrow: the trap is set
    seed(conn, "Dentist appointment", nov(6), (16, 30))
    seed(conn, "Football practice", nov(6), (17, 0))
    seed(conn, "Gym", nov(8), (18, 0))
    reply = ask(conn, ollama, question).lower()
    assert "dentist" in reply and "football" in reply and "gym" not in reply, reply
    assert len(all_events(conn)) == 3  # a question changes nothing
