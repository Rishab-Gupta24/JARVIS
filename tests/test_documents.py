"""Milestone 2 without the model: reading files, menu grids, which week a menu
covers, storage, and the menu tools. Today is Thursday 5 November 2026; the
sample PDF covers Monday 9 to Sunday 15 November."""

from __future__ import annotations

import os
import shutil
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, NOW, ingest_fixture, nov
from fixtures.make_fixtures import WEEK_MENU
from jarvis.calendar import db
from jarvis.config import Settings
from jarvis.core import agent, tools
from jarvis.core.api import create_app
from jarvis.documents import menu, readers, store
from jarvis.documents.ingest import guess_kind, ingest, scan_inbox
from jarvis.documents.menu import MenuRow, WeekInfo
from jarvis.documents.tools import NO_MENU
from test_agent import ScriptedLLM, call

WED_BREAKFAST = "On Wednesday 11 November, for breakfast you have poha, boiled egg and banana."


def ask(conn, name, **args):
    return tools.execute(name, args, tools.ToolContext(conn=conn, now=NOW))


class FakeVision:
    """Stands in for the vision model: 'reads' the menu photo as a Markdown table."""

    def transcribe(self, image: bytes) -> str:
        lines = ["Hostel Mess Menu", "Week: 9th Nov to 15th Nov 2026", "",
                 "| Day | Breakfast | Lunch | Evening Snacks | Dinner |", "|---|---|---|---|---|"]
        lines += [f"| {day} | {' | '.join(meals)} |" for day, meals in WEEK_MENU.items()]
        return "\n".join(lines)


class FakeJson:
    def __init__(self, answer: dict):
        self.answer = answer
        self.asked = 0

    def json(self, messages, schema):
        self.asked += 1
        return self.answer


# ---------- reading files ----------

def test_read_pdf_table():
    x = readers.read_bytes((FIXTURES / "mess_menu_week.pdf").read_bytes(), "menu.pdf")
    assert "Week: 9th Nov to 15th Nov 2026" in x.text
    assert x.tables[0][0][0] == "Day" and len(x.tables[0]) == 8


def test_read_xlsx_copies_merged_cells_across_not_down():
    x = readers.read_bytes((FIXTURES / "mess_menu_rotating.xlsx").read_bytes(), "menu.xlsx")
    grid = x.tables[0]
    tea_row = next(r for r in grid if "Tea, coffee" in r)
    assert tea_row[1:] == ["Tea, coffee"] * 7  # B5:H5 merged across the days
    assert tea_row[0] == ""                    # A4:A5 "Breakfast" not copied down


def test_read_text_with_markdown_table():
    x = readers.read_text("Menu\n| Day | Lunch |\n|---|---|\n| Monday | Dal |\n")
    assert x.tables == [[["Day", "Lunch"], ["Monday", "Dal"]]]


@pytest.mark.parametrize("name, fragment", [("menu.docx", "can't read .docx"), ("IMG_1.HEIC", "HEIC photo")])
def test_unsupported_files(name, fragment):
    with pytest.raises(readers.UnreadableFile, match=fragment):
        readers.read_bytes(b"x", name)


def test_photo_needs_vision_model():
    with pytest.raises(readers.UnreadableFile, match="vision"):
        readers.read_bytes(b"png", "menu.png", vision=None)


# ---------- cells ----------

@pytest.mark.parametrize("cell, meal", [
    ("Breakfast", "breakfast"), ("BREAKFAST (7:30-9:30 AM)", "breakfast"), ("Evening Snacks", "snacks"),
    ("Hi-Tea", "snacks"), ("Lunch 12.30 - 2.30", "lunch"), ("Dinner Menu", "dinner"), ("Supper", "dinner"),
    ("Masala tea", None), ("Lunch special: biryani", None), ("", None),
    ("B R E A K F A S T", "breakfast"), ("S N A C K S", "snacks"), ("P S 1 1", None),
])
def test_meal_of(cell, meal):
    assert menu.meal_of(cell) == meal


@pytest.mark.parametrize("cell, expected", [
    ("Monday", (0, None)), ("WED", (2, None)), ("Mon 16/11", (0, nov(16))),
    ("Wednesday (11.11.2026)", (2, nov(11))), ("Tuesday, 10 Nov", (1, nov(10))),
    ("Mon 17/11", (0, None)),  # 17 Nov is a Tuesday: keep the weekday, drop the bad date
    ("MONDAY\nDate-2/11/2026", (0, nov(2))), ("SUNDAY\nDate 8/11/2026", (6, nov(8))),
    ("Sunday special", None), ("Monday or Tuesday", None), ("Dal, rice", None),
])
def test_weekday_of(cell, expected):
    assert menu.weekday_of(cell, NOW.date()) == expected


def test_hostel_style_pdf_every_cell_exact(conn, tmp_path):
    """Real menus put spaced-out headings above the table, 'MONDAY / Date-16/11/2026'
    labels and one dish per line, with shaded rows that break table detection."""
    from fixtures.make_fixtures import HOSTEL

    r = ingest_fixture(conn, "mess_menu_hostel.pdf", tmp_path)
    assert r.reply == "Got the mess menu for 16 to 22 November, 28 meals."
    got = {(row.date, row.meal): row.items for row in store.rows_of(conn, r.document.id)}
    for i, (day, meals) in enumerate(HOSTEL.items()):
        for meal, dishes in zip(("breakfast", "lunch", "dinner", "snacks"), meals):
            expected = ", ".join(d for d in dishes if d)
            assert got.get((nov(16 + i), meal)) == expected, (day, meal)
    assert len(got) == 28  # the PS11 column is not a meal and isn't stored as one


@pytest.mark.parametrize("cell, cleaned", [
    ("Poha,  boiled egg, tea", "Poha, boiled egg, tea"),
    ("Masala\ndosa, chutney", "Masala dosa, chutney"),        # wrapped line in a comma list
    ("Chicken biryani\nRaita\nGulab jamun", "Chicken biryani, Raita, Gulab jamun"),
    ("-", ""), ("  ", ""), ("Dal,", "Dal"),
])
def test_clean_items(cell, cleaned):
    assert menu.clean_items(cell) == cleaned


# ---------- which week ----------

@pytest.mark.parametrize("text, expected", [
    ("Week: 9th Nov to 15th Nov 2026", WeekInfo(nov(9), nov(15), False)),
    ("Mess menu 9-15 November 2026", WeekInfo(nov(9), nov(15), False)),
    ("Menu 09.11.2026 - 15.11.2026", WeekInfo(nov(9), nov(15), False)),
    ("Nov 9 to Nov 15", WeekInfo(nov(9), nov(15), False)),
    ("Menu for the week of 11 Nov", WeekInfo(nov(9), nov(15), False)),
    ("Holiday menu 28 Dec to 3 Jan 2027", WeekInfo(date(2026, 12, 28), date(2027, 1, 3), False)),
    ("w.e.f. 9/11/2026", WeekInfo(nov(9), None, True)),
    ("Regular menu, repeats every week", WeekInfo(None, None, True)),
    ("Weekly menu", WeekInfo()),
    ("Dinner 7.30-9.30, breakfast 7-9", WeekInfo()),
    ("Semester 1 Nov to 30 Apr", WeekInfo()),  # too long to be one week's menu
])
def test_detect_week(text, expected):
    assert menu.detect_week(text, NOW.date()) == expected


# ---------- ingesting ----------

def test_ingest_dated_pdf(conn, tmp_path):
    r = ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    doc = r.document
    assert r.reply == "Got the mess menu for 9 to 15 November, 28 meals."
    assert (doc.kind, doc.status, doc.valid_from, doc.valid_to, doc.repeats_weekly) == \
        ("mess_menu", "ready", nov(9), nov(15), False)
    rows = store.rows_of(conn, doc.id)
    assert MenuRow(2, nov(11), "breakfast", "Poha, boiled egg, banana, tea/coffee") in rows
    assert all(r.date for r in rows)
    assert (tmp_path / os.path.basename(doc.stored_path)).read_bytes() == (FIXTURES / "mess_menu_week.pdf").read_bytes()


def test_breakfast_this_wednesday(conn, tmp_path):
    """The milestone's done-criterion, with the model's tool call scripted."""
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert ask(conn, "get_menu", date="this wednesday", meal="breakfast").confirmation == WED_BREAKFAST
    llm = ScriptedLLM(call("get_menu", date="this wednesday", meal="breakfast"))
    reply = agent.handle("what's for breakfast this Wednesday?", conn, llm, now=NOW)
    assert reply.text == WED_BREAKFAST and len(llm.calls) == 1  # straight from the rows, no rephrasing


def test_menu_only_for_food_questions(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)

    def menu(said):
        return tools.execute("get_menu", {"date": "tomorrow"},
                             tools.ToolContext(conn=conn, now=NOW, raw_text=said))

    refused = menu("what's on tomorrow.")  # as Whisper wrote it
    assert not refused.ok and "list_events" in refused.data["error"]
    for said in ("what's for dinner tomorrow", "what's in the mess tomorrow", "what are we eating tomorrow"):
        assert menu(said).ok, said
    # "Yes" answering Jarvis's own offer to read the menu (seen live by voice)
    offered = tools.ToolContext(conn=conn, now=NOW, raw_text="Yes",
                                replying_to="Would you like me to look up the mess menu for today?")
    assert tools.execute("get_menu", {"date": "today"}, offered).ok


def test_yes_to_a_menu_offer_through_the_agent(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_rotating.xlsx", tmp_path)
    memory = agent.SessionMemory()
    memory.add("s", NOW, "when is diwali?", "I don't know that. Shall I read today's mess menu?")
    llm = ScriptedLLM(call("get_menu", date="today"))
    r = agent.handle("Yes", conn, llm, now=NOW, session="s", memory=memory)
    assert r.text.startswith("Today for breakfast you have poha"), r.text


@pytest.mark.parametrize("day", ["20 november", "today", "next month"])
def test_no_menu_for_that_week(conn, tmp_path, day):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert ask(conn, "get_menu", date=day, meal="breakfast").confirmation == NO_MENU


def test_no_menu_at_all(conn):
    assert ask(conn, "get_menu", date="tomorrow").confirmation == NO_MENU


def test_whole_day_and_missing_meal(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert ask(conn, "get_menu", date="wednesday").confirmation == (
        "On Wednesday 11 November, for breakfast you have poha, boiled egg and banana. "
        "For lunch there's kadhi pakora and aloo gobi, with rice and roti. "
        "For snacks there's veg sandwich. "
        "And for dinner there's chicken biryani and raita, and for dessert to go with it, gulab jamun."
    )
    assert ask(conn, "get_menu", date="next sunday", meal="supper").confirmation == \
        "On Sunday 15 November, for dinner you have khichdi and kadhi."
    ingest_fixture(conn, "mess_menu_dated.csv", tmp_path)  # 16-22 Nov, no snacks column
    assert ask(conn, "get_menu", date="18 nov", meal="snacks").confirmation == \
        "The menu doesn't list snacks on Wednesday 18 November."


def test_newest_menu_wins_and_old_weeks_are_not_reused(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert ask(conn, "get_menu", date="18 nov", meal="breakfast").confirmation == NO_MENU
    ingest_fixture(conn, "mess_menu_week_revised.csv", tmp_path)
    assert ask(conn, "get_menu", date="this wednesday", meal="breakfast").confirmation == \
        "On Wednesday 11 November, for breakfast you have aloo poha, and for something sweet, jalebi."
    ingest_fixture(conn, "mess_menu_dated.csv", tmp_path)
    assert ask(conn, "get_menu", date="18 nov", meal="breakfast").confirmation == \
        "On Wednesday 18 November, for breakfast you have uttapam and chutney."


def test_rotating_menu_repeats(conn, tmp_path):
    r = ingest_fixture(conn, "mess_menu_rotating.xlsx", tmp_path)
    assert r.reply == "Got the mess menu, 28 meals. I'll use it every week from Monday 2 November."
    assert ask(conn, "get_menu", date="wednesday", meal="breakfast").confirmation == \
        "On Wednesday 11 November, for breakfast you have idli, sambar and chutney."
    assert ask(conn, "get_menu", date="23 december", meal="dinner").confirmation == \
        "On Wednesday 23 December, for dinner you have aloo gobi, with roti."
    assert ask(conn, "get_menu", date="sunday", meal="dinner").confirmation == \
        "On Sunday 8 November, for dinner you have chicken biryani and raita, and for dessert to go with it, gulab jamun."


def test_find_in_menu(conn, tmp_path):
    assert ask(conn, "find_in_menu", dish="biryani", period="next week").confirmation == NO_MENU
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert ask(conn, "find_in_menu", dish="biryani", period="next week").confirmation == (
        "Biryani is at dinner on Wednesday 11 November (Chicken biryani, raita, gulab jamun), "
        "and at lunch on Saturday 14 November (Veg biryani, raita, salad)."
    )
    assert ask(conn, "find_in_menu", dish="pizza", period="next week").confirmation == \
        "I don't see pizza on the menu from Monday 9 November to Sunday 15 November."
    assert ask(conn, "find_in_menu", dish="paneer").ok  # default: the next 7 days


def test_undated_menu_asks_for_its_week(conn, tmp_path):
    r = ingest_fixture(conn, "mess_menu_undated.csv", tmp_path)
    assert r.document.status == "needs_week"
    assert r.reply == "Got a mess menu with 21 meals. Which week is it for, or does it repeat every week?"
    assert ask(conn, "get_menu", date="tuesday", meal="breakfast").confirmation == NO_MENU
    # the agent's prompt tells the model a menu is waiting
    llm = ScriptedLLM(call("set_menu_week", week="next week"))
    reply = agent.handle("it's for next week", conn, llm, now=NOW)
    assert "set_menu_week" in llm.calls[0][0]["content"]
    assert reply.text == "Got the mess menu for 9 to 15 November, 21 meals."
    assert ask(conn, "get_menu", date="tuesday", meal="breakfast").confirmation == \
        "On Tuesday 10 November, for breakfast it's the Tuesday breakfast special."
    assert not ask(conn, "set_menu_week", week="next week").ok  # nothing waiting any more


def test_undated_menu_repeating_or_week_given_up_front(conn, tmp_path):
    r = ingest_fixture(conn, "mess_menu_undated.csv", tmp_path, week="this week")
    assert r.reply == "Got the mess menu for 2 to 8 November, 21 meals."
    conn2 = db.connect(":memory:")
    ingest_fixture(conn2, "mess_menu_undated.csv", tmp_path)
    r2 = ask(conn2, "set_menu_week", repeats_weekly=True)
    assert r2.confirmation == "Got the mess menu, 21 meals. I'll use it every week from Monday 2 November."
    assert "Friday breakfast special" in ask(conn2, "get_menu", date="4 dec").confirmation
    assert not ask(conn2, "set_menu_week").ok


def test_reread_replaces_what_was_stored(conn, tmp_path):
    from jarvis.documents.ingest import reread

    first = ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    conn.execute("UPDATE menu_items SET items = 'wrong' WHERE document_id = ?", (first.document.id,))
    again = reread(conn, first.document.id, NOW, tmp_path)
    assert again.reply == "Got the mess menu for 9 to 15 November, 28 meals."
    assert [d.id for d in store.list_all(conn)] == [again.document.id]
    assert ask(conn, "get_menu", date="this wednesday", meal="breakfast").confirmation == WED_BREAKFAST


def test_duplicate_file_stored_once(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    again = ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    assert again.duplicate and again.reply == "I already have that file (mess_menu_week.pdf)."
    assert len(store.list_all(conn)) == 1


@pytest.mark.parametrize("name, kind", [
    ("timetable.csv", "timetable"), ("exam_schedule.txt", "exam_schedule"), ("hostel_notice.txt", "other"),
])
def test_other_kinds(conn, tmp_path, name, kind):
    r = ingest_fixture(conn, name, tmp_path)
    assert r.document.kind == kind and r.document.status == "ready" and r.meals == 0


def test_model_classifies_only_when_unsure(conn, tmp_path):
    llm = FakeJson({"kind": "timetable"})
    ingest_fixture(conn, "exam_schedule.txt", tmp_path, llm=llm)
    assert llm.asked == 0  # the heuristics were sure
    r = ingest_fixture(conn, "hostel_notice.txt", tmp_path, llm=llm)
    assert llm.asked == 1 and r.document.kind == "timetable"
    assert guess_kind("Breakfast lunch dinner Monday Tuesday Wednesday") == "mess_menu"


def test_search_documents(conn, tmp_path):
    for name in ("hostel_notice.txt", "exam_schedule.txt", "mess_menu_week.pdf"):
        ingest_fixture(conn, name, tmp_path)
    r = ask(conn, "search_documents", query="when is laundry collected?")
    assert r.ok and r.confirmation is None  # the model answers from the excerpt
    first = r.data["results"][0]
    assert first["file"] == "hostel_notice.txt" and "Laundry" in first["excerpt"]
    assert ask(conn, "search_documents", query="operating systems exam").data["results"][0]["file"] == \
        "exam_schedule.txt"
    assert ask(conn, "search_documents", query="swimming pool").confirmation == \
        "I couldn't find anything about that in your files."


def test_menu_photo_is_refused_menus_need_a_pdf(conn, tmp_path):
    """User's choice (6 Nov 2026): photos aren't read exactly enough for menus."""
    from jarvis.documents.ingest import PHOTO_MENU

    with pytest.raises(readers.UnreadableFile) as e:
        ingest_fixture(conn, "mess_menu_photo.png", tmp_path, vision=FakeVision())
    assert str(e.value) == PHOTO_MENU
    assert store.list_all(conn) == []


def test_free_text_menu_keeps_only_dishes_in_the_text(conn, tmp_path):
    llm = FakeJson({"days": [
        {"day": "Monday", "meals": [{"meal": "breakfast", "items": "poori bhaji and tea"},
                                    {"meal": "lunch", "items": "pizza, burger"}]},  # invented: dropped
        {"day": "Tuesday", "meals": [{"meal": "breakfast", "items": "upma with coffee"}]},
        {"day": "Wednesday", "meals": [{"meal": "breakfast", "items": "masala dosa"}]},
    ]})
    r = ingest_fixture(conn, "mess_menu_text.txt", tmp_path, llm=llm)
    assert r.document.kind == "mess_menu" and r.meals == 3 and r.document.repeats_weekly
    assert ask(conn, "get_menu", date="monday", meal="lunch").confirmation == \
        "The menu doesn't list lunch on Monday 9 November."


def test_free_text_menu_without_model_is_kept_as_document(conn, tmp_path):
    r = ingest_fixture(conn, "mess_menu_text.txt", tmp_path)
    assert r.document.kind == "other" and "couldn't read its days and meals" in r.reply


def test_inbox(conn, tmp_path):
    inbox, files = tmp_path / "inbox", tmp_path / "files"
    inbox.mkdir()
    for name in ("mess_menu_week.pdf", "hostel_notice.txt"):
        shutil.copy(FIXTURES / name, inbox / name)
    (inbox / "notes.docx").write_bytes(b"not supported")
    (inbox / "big.pdf.crdownload").write_bytes(b"still downloading")
    old = time.time() - 60
    for p in inbox.iterdir():
        os.utime(p, (old, old))
    (inbox / "fresh.txt").write_text("being copied right now")
    lines = []
    results = scan_inbox(conn, inbox, files, NOW, log=lines.append)
    assert sorted(r.document.filename for r in results) == ["hostel_notice.txt", "mess_menu_week.pdf"]
    assert sorted(p.name for p in inbox.iterdir() if p.is_file()) == ["big.pdf.crdownload", "fresh.txt"]
    assert (inbox / "failed" / "notes.docx").exists()
    assert any("couldn't read notes.docx" in line for line in lines)


def test_upgrades_a_milestone_1_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.MIGRATIONS[0] + "; PRAGMA user_version = 1;")
    old.execute("INSERT INTO events (title, start_at, end_at, created_at, updated_at) "
                "VALUES ('Kept', '2026-11-09T06:00:00+00:00', '2026-11-09T07:00:00+00:00', 'x', 'x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("SELECT title FROM events").fetchone()[0] == "Kept"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


# ---------- over HTTP ----------

TOKEN = "test-token-not-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_upload_and_ask_over_http(tmp_path):
    settings = Settings(token=TOKEN, db_path=str(tmp_path / "j.db"), files_dir=str(tmp_path / "files"),
                        inbox_dir=str(tmp_path / "inbox"))
    llm = ScriptedLLM(call("get_menu", date="this wednesday", meal="breakfast"))
    client = TestClient(create_app(settings, llm=llm, clock=lambda: NOW, vision=FakeVision(), watch_inbox=False))
    pdf = (FIXTURES / "mess_menu_week.pdf").read_bytes()

    r = client.post("/documents", files={"file": ("menu.pdf", pdf, "application/pdf")}, headers=AUTH)
    assert r.status_code == 201
    assert r.json()["reply"] == "Got the mess menu for 9 to 15 November, 28 meals."
    again = client.post("/documents", files={"file": ("menu.pdf", pdf, "application/pdf")}, headers=AUTH)
    assert again.status_code == 200 and again.json()["duplicate"]

    shared = client.post("/documents", data={"text": "Laundry is on Tuesdays.", "filename": "note.txt"},
                         headers=AUTH)
    assert shared.status_code == 201 and shared.json()["document"]["kind"] == "other"

    photo = client.post("/documents", files={"file": ("IMG_2.png", b"fake", "image/png")},
                        data={"week": "next week"}, headers=AUTH)
    assert photo.status_code == 422 and "PDF" in photo.json()["detail"]  # menus from photos are refused

    assert client.post("/documents", files={"file": ("x.docx", b"x")}, headers=AUTH).status_code == 422
    assert client.post("/documents", data={"text": "x", "week": "blursday"}, headers=AUTH).status_code == 422
    assert client.post("/documents", headers=AUTH).status_code == 422
    assert client.post("/documents", data={"text": "x"}).status_code == 401
    assert len(client.get("/documents", headers=AUTH).json()) == 2

    answer = client.post("/capture", json={"text": "what's for breakfast this wednesday?"}, headers=AUTH)
    assert answer.json()["reply"] == WED_BREAKFAST


@pytest.mark.parametrize("said, meal, hour, expected_start", [
    ("what's for the next meal?", None, 9, "Today for breakfast"),
    ("what's for the next meal?", None, 16, "Today for snacks"),
    ("what's for the next meal?", None, 23, "Tomorrow for breakfast"),
    ("what's next to eat", "next", 12, "Today for lunch"),
    ("what's for the next breakfast?", "breakfast", 11, "Tomorrow for breakfast"),
    ("what's for the next breakfast?", "breakfast", 8, "Today for breakfast"),
])
def test_next_meal_is_worked_out_from_the_clock(conn, tmp_path, said, meal, hour, expected_start):
    # Seen live: "What's for the next meal?" got "Could you please specify which meal?"
    ingest_fixture(conn, "mess_menu_rotating.xlsx", tmp_path)
    args = {"meal": meal} if meal else {}
    r = tools.execute("get_menu", args, tools.ToolContext(conn=conn, now=NOW.replace(hour=hour), raw_text=said))
    assert r.confirmation.startswith(expected_start), r.confirmation
