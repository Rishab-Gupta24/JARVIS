"""Festivals and public holidays from the internet (sample feeds in tests/fixtures,
shaped like Google's). Today is Thursday 5 November 2026: 2026 and 2027 are kept."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FIXTURES, NOW, all_events, at, nov, seed
from jarvis.calendar import db, holidays, repo
from jarvis.core import agent, tools

INDIA = (FIXTURES / "holidays_india.ics").read_text()
UAE = (FIXTURES / "holidays_uae.ics").read_text()
FEEDS = {"India": INDIA, "UAE": UAE}


def by_title(conn) -> dict[str, list[repo.Event]]:
    out: dict[str, list[repo.Event]] = {}
    for e in all_events(conn):
        out.setdefault(e.title, []).append(e)
    return out


def ask(conn, name, **args):
    return tools.execute(name, args, tools.ToolContext(conn=conn, now=NOW))


def test_parse_ics():
    india = holidays.parse_ics(INDIA)
    assert len(india) == 11
    diwali = [h for h in india if h.title == "Diwali/Deepavali"]  # folded line put back together
    assert [h.day.year for h in diwali] == [2025, 2026, 2027, 2028]
    assert diwali[1].public and not diwali[1].tentative and diwali[1].days == 1
    naraka = next(h for h in india if h.title == "Naraka Chaturdasi")
    assert not naraka.public
    eid = next(h for h in holidays.parse_ics(UAE) if h.title == "Eid al-Fitr")
    assert eid.public and eid.tentative
    marked = holidays.parse_ics(UAE.replace("SUMMARY:Eid", "SUMMARY:Eid al-Adha (tentative)\r\nX:", 1))
    assert any(h.title == "Eid al-Adha" and h.tentative for h in marked)  # real feeds write it in the name


def test_several_matches_read_as_a_list(conn):
    seed(conn, "DBMS exam", nov(24))
    seed(conn, "OS exam", nov(26))
    seed(conn, "Maths exam", nov(28))
    assert ask(conn, "find_event", query="exam").confirmation == (
        "DBMS exam is on Tuesday 24 November, OS exam is on Thursday 26 November, "
        "and Maths exam is on Saturday 28 November."
    )
    seed(conn, "Physics exam", nov(30))
    assert ask(conn, "find_event", query="exam").confirmation.count(" is on ") == 3  # at most three


def test_import_this_year_and_next(conn):
    r = holidays.sync(conn, FEEDS, NOW)
    assert (r.added, r.updated, r.removed, r.years) == (12, 0, 0, (2026, 2027))
    events = by_title(conn)
    assert sorted(e.start.year for e in events["Diwali"]) == [2026, 2027]  # not 2025 or 2028
    choti = events["Choti Diwali (Naraka Chaturdasi)"][0]
    assert choti.start == at(nov(8), 0) and choti.all_day and not choti.important
    assert choti.source == "holiday" and choti.notes == "Observance (India)."
    [new_year] = events["New Year's Day"]  # in both lists, stored once
    assert new_year.notes == "Public holiday (India, UAE)."
    assert events["Eid al-Fitr"][0].notes == "Public holiday (UAE). Date is tentative."
    assert repo.pending_reminders(conn, events["Diwali"][0].id) == []  # not important by default


def test_second_sync_changes_nothing(conn):
    holidays.sync(conn, FEEDS, NOW)
    r = holidays.sync(conn, FEEDS, NOW)
    assert (r.added, r.updated, r.removed, r.kept) == (0, 0, 0, 12)


def test_moved_date_is_corrected(conn):
    holidays.sync(conn, FEEDS, NOW)
    moved = {"India": INDIA, "UAE": UAE.replace("DTSTART;VALUE=DATE:20260320", "DTSTART;VALUE=DATE:20260321")}
    r = holidays.sync(conn, moved, NOW)
    assert (r.added, r.removed) == (1, 1)
    assert [e.start.day for e in by_title(conn)["Eid al-Fitr"] if e.start.year == 2026] == [21]


def test_your_changes_survive_a_refresh(conn):
    holidays.sync(conn, FEEDS, NOW)
    events = by_title(conn)
    xmas = next(e for e in events["Christmas"] if e.start.year == 2026)
    diwali = next(e for e in events["Diwali"] if e.start.year == 2026)
    repo.delete_event(conn, xmas.id, NOW)
    repo.update_event(conn, diwali.id, NOW, important=True)
    party = seed(conn, "Diwali party", nov(8), (19, 0))
    r = holidays.sync(conn, FEEDS, NOW)
    assert (r.added, r.removed) == (0, 0)
    assert repo.get_event(conn, xmas.id) is None  # stays deleted
    assert repo.get_event(conn, diwali.id).important  # stays important, reminders kept
    assert repo.pending_reminders(conn, diwali.id) == [at(nov(7), 20), at(nov(8), 8)]
    assert repo.get_event(conn, party.id) is not None  # your own events are never touched


def test_sync_from_internet_and_staleness(conn):
    def fetcher(regions):
        assert regions == ["india", "uae"]
        return FEEDS, []

    assert holidays.is_stale(conn, NOW, timedelta(days=7))
    r = holidays.sync_from_internet(conn, ["india", "uae"], NOW, fetcher)
    assert r.added == 12 and not r.errors
    assert db.get_meta(conn, holidays.LAST_SYNC) == NOW.isoformat()
    assert not holidays.is_stale(conn, NOW + timedelta(days=6), timedelta(days=7))
    assert holidays.is_stale(conn, NOW + timedelta(days=7), timedelta(days=7))
    assert holidays.is_stale(conn, NOW.replace(year=2027, month=1, day=1), timedelta(days=365))


def test_download_problems(conn):
    r = holidays.sync_from_internet(conn, ["india"], NOW, lambda regions: ({}, ["India: timed out"]))
    assert r.errors == ["India: timed out"] and all_events(conn) == []
    assert db.get_meta(conn, holidays.LAST_SYNC) is None  # try again next time
    partial = holidays.sync_from_internet(conn, ["india", "uae"], NOW,
                                          lambda regions: ({"India": INDIA}, ["UAE: 503"]))
    assert partial.added == 9 and partial.errors == ["UAE: 503"]
    assert db.get_meta(conn, holidays.LAST_SYNC) is None


def test_feed_urls():
    assert holidays.feed_url("India") == holidays.GOOGLE_ICS.format(code="indian")
    assert holidays.feed_url("uae").endswith("en.ae%23holiday%40group.v.calendar.google.com/public/basic.ics")
    assert holidays.feed_url("german").count("en.german%23holiday") == 1  # any Google code works


@pytest.mark.parametrize("query, answer", [
    ("Diwali", "Diwali is on Sunday 8 November."),
    ("choti diwali", "Choti Diwali (Naraka Chaturdasi) is on Sunday 8 November."),
    ("Christmas", "Christmas is on Friday 25 December."),
    ("Eid", "Eid al-Fitr is on Wednesday 10 March 2027."),       # this year's has passed
    ("new year", "New Year's Day was on Thursday 1 January."),   # only this year's is listed
    ("swimming", "I couldn't find swimming in your calendar."),
])
def test_when_is(conn, query, answer):
    holidays.sync(conn, FEEDS, NOW)
    assert ask(conn, "find_event", query=query).confirmation == answer


def test_when_is_one_of_your_events(conn):
    seed(conn, "Dentist appointment", nov(6), (16, 30))
    assert ask(conn, "find_event", query="dentist").confirmation == "Dentist appointment is tomorrow at 4:30 pm."


def test_holidays_show_up_in_the_schedule(conn):
    holidays.sync(conn, FEEDS, NOW)
    titles = [e["title"] for e in ask(conn, "list_events", period="this weekend").data["events"]]
    assert sorted(titles) == ["Choti Diwali (Naraka Chaturdasi)", "Diwali"]  # both on Sunday 8 Nov


# ---------- through the real model ----------

@pytest.mark.llm
@pytest.mark.parametrize("question, answer", [
    ("When is Diwali?", "Diwali is on Sunday 8 November."),
    ("when is choti diwali", "Choti Diwali (Naraka Chaturdasi) is on Sunday 8 November."),
    ("When is Christmas this year?", "Christmas is on Friday 25 December."),
])
def test_when_is_by_model(conn, ollama, question, answer):
    holidays.sync(conn, FEEDS, NOW)
    assert agent.handle(question, conn, ollama, now=NOW).text == answer


@pytest.mark.llm
def test_whats_on_sunday_mentions_diwali(conn, ollama):
    holidays.sync(conn, FEEDS, NOW)
    reply = agent.handle("What's on Sunday?", conn, ollama, now=NOW).text.lower()
    assert "diwali" in reply
