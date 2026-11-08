"""Alarms and timers: when they ring, the tools, and the ringing loop."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from conftest import NOW, at, nov
from jarvis.calendar import alarms, db
from jarvis.core import tools
from jarvis.core.dates import DateParseError
from jarvis.core.toolkit import ToolContext


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def ctx(conn, now=NOW, raw="set an alarm", allow=True):
    return ToolContext(conn=conn, now=now, raw_text=raw, allow_pc=allow, source="voice")


# Thursday 5 Nov 2026, 09:00
@pytest.mark.parametrize("time_text, date_text, now, expected", [
    ("7", None, NOW, at(nov(5), 19)),               # morning: the next 7 o'clock is 7 pm
    ("7", None, at(nov(5), 23), at(nov(6), 7)),     # at night: 7 am
    ("6:30 am", None, NOW, at(nov(6), 6, 30)),       # already past today: tomorrow
    ("10", None, NOW, at(nov(5), 10)),
    ("7", "tomorrow", NOW, at(nov(6), 7)),           # a named day: am unless said
    ("7 pm", "friday", NOW, at(nov(6), 19)),
    ("noon", None, NOW, at(nov(5), 12)),
    ("21:15", None, NOW, at(nov(5), 21, 15)),
])
def test_when_an_alarm_rings(time_text, date_text, now, expected):
    assert alarms.when_alarm(time_text, date_text, now) == expected


def test_weekday_alarm_skips_the_weekend():
    assert alarms.when_alarm("7 am", None, at(nov(6), 9), "weekdays") == at(nov(9), 7)  # Fri 9am -> Mon


def test_a_passed_time_on_a_named_day_is_refused():
    with pytest.raises(DateParseError):
        alarms.when_alarm("8 am", "today", NOW + timedelta(hours=1)) if False else \
            alarms.when_alarm("8 am", "5 nov", NOW)


@pytest.mark.parametrize("said, seconds", [
    ("10 minutes", 600), ("1 hour 30 min", 5400), ("half an hour", 1800), ("an hour and a half", 5400),
    ("one and a half hours", 5400), ("90 seconds", 90), ("2h", 7200), ("five minutes", 300), ("a minute", 60),
])
def test_durations(said, seconds):
    assert alarms.duration(said) == timedelta(seconds=seconds)


@pytest.mark.parametrize("said", ["soon", "", "25 hours"])
def test_bad_durations(said):
    with pytest.raises(DateParseError):
        alarms.duration(said)


def test_set_list_cancel(conn):
    r = tools.execute("set_alarm", {"time": "6:30 am", "label": "gym"}, ctx(conn, raw="wake me at 6:30 for gym"))
    assert r.confirmation == "Alarm set for 6:30 am tomorrow for gym."
    r = tools.execute("set_alarm", {"time": "10"}, ctx(conn, raw="alarm at 10"))
    assert r.confirmation == "Alarm set for 10 am today, that's in 1 hour."
    r = tools.execute("set_timer", {"duration": "10 minutes", "label": "pasta"}, ctx(conn, raw="pasta timer 10 min"))
    assert r.confirmation == "Pasta timer set for 10 minutes."
    r = tools.execute("list_alarms", {}, ctx(conn, raw="what alarms do I have"))
    assert r.confirmation == ("You have a pasta timer with 10 minutes left, an alarm at 10 am today and an alarm "
                              "at 6:30 am tomorrow for gym.")
    assert tools.execute("cancel_alarm", {"label": "pasta"}, ctx(conn)).confirmation == "Cancelled the pasta timer."
    assert tools.execute("cancel_alarm", {"time": "6:30"}, ctx(conn)).confirmation == \
        "Cancelled your alarm for 6:30 am tomorrow for gym."
    assert not tools.execute("cancel_alarm", {"time": "5 pm"}, ctx(conn)).ok
    assert tools.execute("cancel_alarm", {}, ctx(conn)).confirmation == \
        "Cancelled your alarm for 10 am today."  # only one left
    assert tools.execute("list_alarms", {}, ctx(conn)).confirmation == "You don't have any alarms or timers set."


def test_repeating_alarm_from_the_users_words(conn):
    r = tools.execute("set_alarm", {"time": "7 am"}, ctx(conn, raw="wake me up at 7 every weekday"))
    assert r.confirmation == "Alarm set for 7 am on weekdays."


def test_alarms_are_not_for_shared_messages(conn):
    assert not tools.execute("set_alarm", {"time": "7"}, ctx(conn, allow=False)).ok
    offered = {s["function"]["name"] for s in tools.specs_for(False)}
    assert offered and not offered & {"set_alarm", "set_timer", "list_alarms", "cancel_alarm", "stop_alarm"}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_ringing_pushes_rings_and_moves_repeats(conn, tmp_path):
    path = tmp_path / "j.db"
    c = db.connect(str(path))
    alarms.create(c, at(nov(5), 9, 1), NOW, "standup")
    alarms.create(c, at(nov(5), 9, 1), NOW, "", repeat="daily")
    alarms.create(c, at(nov(5), 8, 0), NOW, "old")          # 61 minutes late: missed, not rung
    alarms.create(c, NOW + timedelta(minutes=1), NOW, "tea", kind="timer")
    c.close()
    clock, pushed, rung = Clock(NOW), [], []
    ac = alarms.AlarmClock(lambda: db.connect(str(path)), clock, ring=lambda said, stop: rung.append(said),
                           push=lambda title, said: pushed.append(title), log=lambda _: None)
    assert ac.run_once() == []  # 08:00 one is missed silently
    clock.now = at(nov(5), 9, 1)
    rang = ac.run_once()
    assert len(rang) == 3
    assert pushed == ["Alarm, 9:01 am: standup", "Alarm, 9:01 am", "Timer done: tea"]
    assert rung == ["It's 9:01 am. Time for standup.", "It's 9:01 am. Time to get up.", "Your tea timer is done."]
    assert ac.run_once() == []  # each rings once
    c = db.connect(str(path))
    statuses = dict(c.execute("SELECT label, status FROM alarms").fetchall())
    assert statuses == {"standup": "done", "": "pending", "old": "missed", "tea": "done"}
    daily = alarms.upcoming(c)[0]
    assert daily.fire_at == at(nov(6), 9, 1)  # tomorrow, same time
    c.close()


def test_silence_stops_the_ring():
    played, said = [], []

    class FakeSpeaker:
        def play(self, audio, rate):
            played.append(len(audio))

        def say(self, text):
            said.append(text)

    stop = threading.Event()
    ring = alarms.ring_with(FakeSpeaker(), cycles=3)
    ring("It's 7 am.", stop)
    assert len(played) == 3 and said == ["It's 7 am."]
    stop.set()
    played.clear()
    ring("It's 7 am.", stop)
    assert played == []


def test_stop_and_snooze(conn):
    r = tools.execute("stop_alarm", {"snooze_minutes": 9}, ctx(conn, raw="snooze"))
    assert r.confirmation == "Snoozing. I'll ring again at 9:09 am."
    assert alarms.upcoming(conn)[0].label == "snooze"
    assert tools.execute("stop_alarm", {}, ctx(conn, raw="stop")).confirmation == "Alarm off."


def test_alarm_tone_is_audio():
    tone, rate = alarms.alarm_tone()
    assert rate == 24000 and 2.0 < len(tone) / rate < 4 and abs(tone).max() <= 0.36


@pytest.mark.llm
@pytest.mark.parametrize("said, tool, check", [
    ("Wake me up at 7 tomorrow", "set_alarm", lambda a: "7" in a["time"]),
    ("Set an alarm for 6:30 every weekday", "set_alarm", lambda a: "6:30" in a["time"]),
    ("Set a timer for ten minutes for the pasta", "set_timer", lambda a: "10" in a["duration"] or
     "ten" in a["duration"].lower()),
    ("What alarms do I have?", "list_alarms", lambda a: True),
])
def test_model_uses_the_alarm_tools(conn, ollama, said, tool, check):
    from jarvis.core import agent

    r = agent.handle(said, conn, ollama, now=NOW, source="voice", session=f"t-{said}")
    called = [(a["tool"], a["arguments"]) for a in r.actions]
    assert called and called[0][0] == tool, (said, called, r.text)
    assert check(called[0][1]), called
    if tool == "set_alarm" and "tomorrow" in said:
        assert r.text == "Alarm set for 7 am tomorrow."  # no "for wake up" label
