"""Tool calls run directly, without a model: the exact calls the model is expected
to make for each kind of request, and how bad arguments come back as errors."""

from __future__ import annotations

import json

import pytest

from conftest import NOW, all_events, at, nov, seed
from jarvis.calendar import repo
from jarvis.core import tools


def run(conn, name, raw_text="test", **args):
    return tools.execute(name, args, tools.ToolContext(conn=conn, now=NOW, raw_text=raw_text))


@pytest.mark.parametrize("said, flagged", [
    ("Physics lab report due on 20 November", False),
    ("Physics lab report due 20 Nov, it's important", True),
    ("lab report 20 nov, don't let me forget", True),
    ("remind me about the lab report on 20 nov", True),
    (None, True),  # direct API call: no words to check
])
def test_important_only_when_asked(conn, said, flagged):
    run(conn, "create_event", raw_text=said, title="Lab report", date="20 nov", important=True)
    assert all_events(conn)[0].important is flagged


def test_create_timed_important(conn):
    r = run(conn, "create_event", raw_text="DBMS quiz Monday 10am, important",
            title="DBMS quiz", date="monday", start_time="10am", important=True)
    assert r.ok and r.mutated
    assert r.confirmation == "Added DBMS quiz, Monday 9 November at 10 am, marked important."
    [ev] = all_events(conn)
    assert (ev.start, ev.end, ev.important) == (at(nov(9), 10), at(nov(9), 11), True)
    assert ev.raw_text == "DBMS quiz Monday 10am, important"
    assert len(repo.pending_reminders(conn, ev.id)) == 3


def test_create_all_day_when_no_time(conn):
    r = run(conn, "create_event", title="physics lab report due", date="20 november")
    [ev] = all_events(conn)
    assert ev.all_day and ev.start == at(nov(20), 0) and ev.end == at(nov(21), 0)
    assert ev.title == "Physics lab report due"
    assert r.confirmation == "Added Physics lab report due, Friday 20 November."


def test_create_with_range_duration_and_time_inside_date(conn):
    run(conn, "create_event", title="Team meeting", date="next friday", start_time="2", end_time="3pm")
    run(conn, "create_event", title="Project review", date="the 12th", start_time="11am", duration_minutes=120)
    run(conn, "create_event", title="Dentist", date="tomorrow at 4:30 pm")
    run(conn, "create_event", title="Movie night", date="tonight")
    got = {e.title: (e.start, e.end) for e in all_events(conn)}
    assert got["Team meeting"] == (at(nov(13), 14), at(nov(13), 15))
    assert got["Project review"] == (at(nov(12), 11), at(nov(12), 13))
    assert got["Dentist"] == (at(nov(6), 16, 30), at(nov(6), 17, 30))
    assert got["Movie night"] == (at(nov(5), 20), at(nov(5), 21))


def test_end_time_alone_is_a_deadline(conn):
    r = run(conn, "create_event", raw_text="submit form by sunday 11:59pm, don't forget",
            title="Submit form", date="sunday", end_time="11:59pm", important=True)
    assert r.confirmation == "Added Submit form, Sunday 8 November at 11:59 pm, marked important."
    [ev] = all_events(conn)
    assert ev.start == at(nov(8), 23, 59) and ev.end == at(nov(9), 0)  # doesn't spill into Monday
    assert repo.list_on_day(conn, nov(9)) == []


def test_create_past_midnight(conn):
    run(conn, "create_event", title="Party", date="saturday", start_time="10pm", end_time="1am")
    [ev] = all_events(conn)
    assert ev.end == at(nov(8), 1)


def test_small_model_quirks_are_tolerated(conn):
    r = tools.execute("create_event", json.dumps({
        "title": "Gym", "date": "in 3 days", "start_time": "6pm",
        "end_time": "", "location": "none", "important": "false", "duration_minutes": None,
    }), tools.ToolContext(conn=conn, now=NOW))
    assert r.ok, r.data
    [ev] = all_events(conn)
    assert ev.start == at(nov(8), 18) and ev.location is None and not ev.important


@pytest.mark.parametrize("args, fragment", [
    ({"title": "X", "date": "blursday"}, "which day"),
    ({"title": "X", "date": "monday", "start_time": "25:00"}, "not a real time"),
    ({"title": "", "date": "monday"}, "title"),
    ({"date": "monday"}, "title"),
    ({"title": "X", "date": "1 jan 2040"}, "looks wrong"),
    ({"title": "X", "date": "monday", "start_time": "3pm", "end_time": "2pm"}, "before the start"),
])
def test_create_errors_go_back_to_model(conn, args, fragment):
    r = tools.execute("create_event", args, tools.ToolContext(conn=conn, now=NOW))
    assert not r.ok and fragment in r.data["error"]
    assert all_events(conn) == []


def test_unknown_tool_and_bad_json(conn):
    ctx = tools.ToolContext(conn=conn, now=NOW)
    assert "no tool" in tools.execute("rm_rf", {}, ctx).data["error"]
    assert "JSON" in tools.execute("create_event", "{oops", ctx).data["error"]


def test_move_to_another_day_keeps_time(conn):
    ev = seed(conn, "DBMS quiz", nov(9), (10, 0))
    r = run(conn, "update_event", match_title="DBMS quiz", new_date="tuesday")
    assert r.ok
    assert r.confirmation == "Moved DBMS quiz to Tuesday 10 November at 10 am."
    got = repo.get_event(conn, ev.id)
    assert (got.start, got.end) == (at(nov(10), 10), at(nov(10), 11))


def test_change_time_keeps_day_and_length(conn):
    ev = seed(conn, "Dentist appointment", nov(6), (16, 30), (17, 15))
    run(conn, "update_event", match_title="dentist", new_start_time="6pm")
    got = repo.get_event(conn, ev.id)
    assert (got.start, got.end) == (at(nov(6), 18), at(nov(6), 18, 45))


def test_push_to_a_time_moves_the_start_even_if_sent_as_end(conn):
    ev = seed(conn, "Dentist appointment", nov(6), (16, 30))
    r = run(conn, "update_event", raw_text="Push my dentist appointment to 6pm",
            match_title="dentist", new_end_time="6pm")
    got = repo.get_event(conn, ev.id)
    assert (got.start, got.end) == (at(nov(6), 18), at(nov(6), 19))
    assert r.confirmation == "Moved Dentist appointment to tomorrow at 6 pm."


def test_changing_only_the_end_says_so(conn):
    ev = seed(conn, "Dentist appointment", nov(6), (16, 30))
    r = run(conn, "update_event", raw_text="the dentist will run until 6pm",
            match_title="dentist", new_end_time="6pm")
    got = repo.get_event(conn, ev.id)
    assert (got.start, got.end) == (at(nov(6), 16, 30), at(nov(6), 18))
    assert r.confirmation == "Dentist appointment now ends at 6 pm."


def test_rename_and_relocate(conn):
    ev = seed(conn, "Team meeting", nov(13), (14, 0), (15, 0))
    r = run(conn, "update_event", match_title="team meeting", new_title="Sprint planning", new_location="Room 4")
    assert r.confirmation == "Renamed Team meeting to Sprint planning and set the place for Sprint planning to Room 4."
    got = repo.get_event(conn, ev.id)
    assert (got.title, got.location, got.start) == ("Sprint planning", "Room 4", at(nov(13), 14))


def test_all_day_becomes_timed(conn):
    ev = seed(conn, "Lab report", nov(20))
    run(conn, "update_event", match_title="lab report", new_start_time="5pm")
    got = repo.get_event(conn, ev.id)
    assert not got.all_day and (got.start, got.end) == (at(nov(20), 17), at(nov(20), 18))


def test_set_important_and_unset(conn):
    ev = seed(conn, "Physics lab report", nov(20))
    r = run(conn, "set_important", match_title="physics lab report")
    assert r.confirmation == "Marked Physics lab report, Friday 20 November, as important."
    assert repo.get_event(conn, ev.id).important
    run(conn, "set_important", match_title="physics", important=False)
    assert not repo.get_event(conn, ev.id).important


def test_delete_by_title_and_day(conn):
    seed(conn, "Football practice", nov(6), (17, 0))
    sat = seed(conn, "Football practice", nov(7), (17, 0))
    r = run(conn, "delete_event", match_title="football practice", match_date="saturday")
    assert r.ok and r.confirmation == "Deleted Football practice, Saturday 7 November at 5 pm."
    assert [e.start for e in all_events(conn)] == [at(nov(6), 17)]
    assert repo.get_event(conn, sat.id) is None


def test_ambiguous_match_asks(conn):
    a = seed(conn, "Football practice", nov(6), (17, 0))
    seed(conn, "Football practice", nov(7), (17, 0))
    r = run(conn, "delete_event", match_title="football")
    assert not r.ok and "Ask the user" in r.data["error"] and f"id {a.id}" in r.data["error"]
    assert len(all_events(conn)) == 2
    assert run(conn, "delete_event", event_id=a.id).ok
    assert len(all_events(conn)) == 1


def test_no_match(conn):
    seed(conn, "Gym", nov(8), (18, 0))
    r = run(conn, "delete_event", match_title="swimming")
    assert not r.ok and "couldn't find 'swimming'" in r.data["error"]
    r = run(conn, "delete_event", raw_text="cancel gym on monday", match_title="gym", match_date="monday")
    assert not r.ok and "Monday 9 November" in r.data["error"]  # a day the user said is respected
    assert not run(conn, "delete_event").ok
    assert len(all_events(conn)) == 1


def test_day_the_user_never_said_is_dropped(conn):
    ev = seed(conn, "Lunch with Aisha", nov(7), (13, 0))
    r = run(conn, "delete_event", raw_text="Delete lunch with Aisha", match_title="lunch", match_date="today")
    assert r.ok and repo.get_event(conn, ev.id) is None


def test_list_events(conn):
    seed(conn, "Dentist appointment", nov(6), (16, 30))
    seed(conn, "Football practice", nov(6), (17, 0), important=True)
    seed(conn, "Gym", nov(8), (18, 0))
    r = run(conn, "list_events", period="tomorrow")
    assert r.ok and not r.mutated and r.confirmation is None
    assert r.data["period"] == "tomorrow"
    assert [e["title"] for e in r.data["events"]] == ["Dentist appointment", "Football practice"]
    assert r.data["events"][0]["when"] == "tomorrow at 4:30 pm"
    assert r.data["events"][1]["important"] is True
    week = run(conn, "list_events", period="this week")
    assert week.data["period"] == "today to Sunday 8 November" and len(week.data["events"]) == 3
