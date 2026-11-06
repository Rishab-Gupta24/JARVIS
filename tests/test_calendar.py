"""Calendar storage: validation, soft delete, dedupe, matching, reminders."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, all_events, at, nov, seed
from jarvis.calendar import repo


def test_create_round_trips_local_times(conn):
    ev = seed(conn, "DBMS quiz", nov(9), (10, 0))
    got = repo.get_event(conn, ev.id)
    assert got.start == at(nov(9), 10) and got.end == at(nov(9), 11)
    assert got.start.utcoffset() == timedelta(hours=4)
    # stored as UTC
    row = conn.execute("SELECT start_at FROM events WHERE id = ?", (ev.id,)).fetchone()
    assert row["start_at"] == "2026-11-09T06:00:00+00:00"


def test_list_events_overlap(conn):
    seed(conn, "Early", nov(9), (8, 0))
    seed(conn, "All day", nov(9))
    seed(conn, "Other day", nov(10), (8, 0))
    titles = [e.title for e in repo.list_on_day(conn, nov(9))]
    assert titles == ["All day", "Early"]


@pytest.mark.parametrize("title, start, end, message", [
    ("", at(nov(9), 10), at(nov(9), 11), "title"),
    ("Quiz", at(nov(9), 10), at(nov(9), 9), "end must be after"),
    ("Quiz", at(nov(9), 10), at(nov(9), 10), "end must be after"),
    ("Quiz", at(nov(9, year=2040), 10), at(nov(9, year=2040), 11), "looks wrong"),
])
def test_validation(conn, title, start, end, message):
    with pytest.raises(ValueError, match=message):
        repo.create_event(conn, repo.NewEvent(title=title, start=start, end=end), NOW)
    assert all_events(conn) == []


def test_all_day_must_be_midnight(conn):
    with pytest.raises(ValueError, match="midnight"):
        repo.create_event(conn, repo.NewEvent("X", at(nov(9), 10), at(nov(10), 10), all_day=True), NOW)


def test_client_uuid_dedupes(conn):
    new = repo.NewEvent("Gym", at(nov(8), 18), at(nov(8), 19), client_uuid="abc-1")
    a = repo.create_event(conn, new, NOW)
    b = repo.create_event(conn, new, NOW)
    assert a.id == b.id and len(all_events(conn)) == 1


def test_deleted_event_is_not_resurrected_by_resend(conn):
    new = repo.NewEvent("Gym", at(nov(8), 18), at(nov(8), 19), client_uuid="abc-2")
    ev = repo.create_event(conn, new, NOW)
    repo.delete_event(conn, ev.id, NOW)
    again = repo.create_event(conn, new, NOW)
    assert again.deleted and all_events(conn) == []


def test_update_and_soft_delete(conn):
    ev = seed(conn, "Dentist", nov(6), (16, 30))
    moved = repo.update_event(conn, ev.id, NOW, start=at(nov(6), 18), end=at(nov(6), 19))
    assert moved.start == at(nov(6), 18)
    repo.delete_event(conn, ev.id, NOW)
    assert repo.get_event(conn, ev.id) is None
    assert repo.get_event(conn, ev.id, include_deleted=True).deleted
    with pytest.raises(LookupError):
        repo.update_event(conn, ev.id, NOW, title="x")


def test_update_rejects_unknown_fields(conn):
    ev = seed(conn, "Dentist", nov(6), (16, 30))
    with pytest.raises(ValueError, match="source"):
        repo.update_event(conn, ev.id, NOW, source="hack")


def test_important_event_gets_three_reminders(conn):
    ev = seed(conn, "DBMS quiz", nov(9), (10, 0), important=True)
    assert repo.pending_reminders(conn, ev.id) == [
        at(nov(8), 10), at(nov(9), 9), at(nov(9), 9, 50),
    ]


def test_reminders_in_the_past_are_skipped(conn):
    ev = seed(conn, "Soon", nov(5), (9, 30), important=True)  # 30 minutes from now
    assert repo.pending_reminders(conn, ev.id) == [at(nov(5), 9, 20)]


def test_all_day_important_reminders(conn):
    ev = seed(conn, "Lab report due", nov(20), important=True)
    assert repo.pending_reminders(conn, ev.id) == [at(nov(19), 20), at(nov(20), 8)]


def test_reminders_follow_changes(conn):
    ev = seed(conn, "Quiz", nov(9), (10, 0))
    assert repo.pending_reminders(conn, ev.id) == []
    repo.update_event(conn, ev.id, NOW, important=True)
    assert len(repo.pending_reminders(conn, ev.id)) == 3
    repo.update_event(conn, ev.id, NOW, start=at(nov(10), 10), end=at(nov(10), 11))
    assert repo.pending_reminders(conn, ev.id)[0] == at(nov(9), 10)
    repo.update_event(conn, ev.id, NOW, important=False)
    assert repo.pending_reminders(conn, ev.id) == []
    repo.update_event(conn, ev.id, NOW, important=True)
    repo.delete_event(conn, ev.id, NOW)
    assert repo.pending_reminders(conn, ev.id) == []


def test_find_events(conn):
    dentist = seed(conn, "Dentist appointment", nov(6), (16, 30))
    seed(conn, "Football practice", nov(6), (17, 0))
    sat = seed(conn, "Football practice", nov(7), (17, 0))
    assert repo.find_events(conn, "my dentist appointment", NOW)[0][1].id == dentist.id
    assert repo.find_events(conn, "dentst", NOW)[0][1].id == dentist.id  # typo
    by_day = repo.find_events(conn, "football", NOW, day=nov(7))
    assert [e.id for _, e in by_day] == [sat.id]
    both = repo.find_events(conn, "football", NOW)
    assert len(both) == 2 and both[0][0] == both[1][0]
    assert repo.find_events(conn, "swimming", NOW) == []
    assert repo.find_events(conn, "dentists", NOW)[0][1].id == dentist.id   # plural
    assert repo.find_events(conn, "foot", NOW)                              # start of a word


def test_find_events_does_not_match_a_shorter_different_word(conn):
    navami = seed(conn, "Rama Navami", nov(20))
    ramadan = seed(conn, "Ramadan Start", nov(21))
    assert [e.id for _, e in repo.find_events(conn, "ramadan", NOW)] == [ramadan.id]
    assert [e.id for _, e in repo.find_events(conn, "navami", NOW)] == [navami.id]
