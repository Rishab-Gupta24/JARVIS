"""Milestone 2 done-criterion, through the real local model: after the sample
mess menu is ingested, "what's for breakfast this Wednesday?" returns exactly
what the file says, and a week with no menu gets "I don't have that week's menu."
Today is Thursday 5 November 2026; the sample PDF covers 9 to 15 November.

Run with:  .venv\\Scripts\\python -m pytest -m llm -v
"""

from __future__ import annotations

import pytest

from conftest import NOW, ingest_fixture
from jarvis.core import agent
from jarvis.documents import store
from jarvis.documents.tools import NO_MENU

pytestmark = pytest.mark.llm


@pytest.fixture
def menu_conn(conn, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    return conn


def ask(conn, llm, text: str) -> str:
    return agent.handle(text, conn, llm, now=NOW).text


@pytest.mark.parametrize("question, answer", [
    ("what's for breakfast this Wednesday?",
     "On Wednesday 11 November, for breakfast you have poha, boiled egg and banana."),
    ("What's for lunch on Monday?",
     "On Monday 9 November, for lunch you have rajma, with jeera rice and roti."),
    ("what are the evening snacks on friday next week",
     "On Friday 13 November, for snacks you have pav bhaji."),
    ("What's for dinner next Sunday?",
     "On Sunday 15 November, for dinner you have khichdi and kadhi."),
])
def test_menu_answers_are_exact(menu_conn, ollama, question, answer):
    assert ask(menu_conn, ollama, question) == answer


@pytest.mark.parametrize("question", [
    "what's for breakfast on 20 November?",
    "What's for dinner today?",
])
def test_week_without_a_menu(menu_conn, ollama, question):
    assert ask(menu_conn, ollama, question) == NO_MENU


def test_no_invented_menu_mid_conversation(menu_conn, ollama, tmp_path):
    """Regression: with a menu waiting for its week and earlier answers in memory, the
    model once answered 'dinner on 20 November' from imagination without a tool call."""
    ingest_fixture(menu_conn, "mess_menu_undated.csv", tmp_path)
    memory = agent.SessionMemory()
    for question in ["what's for breakfast this Wednesday?", "when is paneer next week?"]:
        agent.handle(question, menu_conn, ollama, now=NOW, session="s", memory=memory)
    r = agent.handle("what's for dinner on 20 november?", menu_conn, ollama, now=NOW, session="s", memory=memory)
    assert r.text in (NO_MENU, agent.NO_GUESSING)


def test_whole_day(menu_conn, ollama):
    reply = ask(menu_conn, ollama, "What's in the mess on Tuesday?")
    assert reply.startswith("On Tuesday 10 November, for breakfast you have idli, sambar and coconut chutney.")


def test_when_is_a_dish(menu_conn, ollama):
    reply = ask(menu_conn, ollama, "When is biryani next week?")
    assert "Chicken biryani, raita, gulab jamun" in reply and "Veg biryani, raita, salad" in reply


def test_question_about_another_file(conn, ollama, tmp_path):
    ingest_fixture(conn, "hostel_notice.txt", tmp_path)
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    reply = ask(conn, ollama, "Which days is laundry collected?").lower()
    assert "tuesday" in reply and "friday" in reply


def test_telling_jarvis_the_week(conn, ollama, tmp_path):
    memory = agent.SessionMemory()
    r = ingest_fixture(conn, "mess_menu_undated.csv", tmp_path)
    memory.add("s", NOW, "(sent the file mess_menu_undated.csv)", r.reply)
    agent.handle("It's for next week", conn, ollama, now=NOW, session="s", memory=memory)
    doc = store.get(conn, r.document.id)
    assert doc.status == "ready" and doc.valid_from.isoformat() == "2026-11-09"
    assert ask(conn, ollama, "what's for breakfast on Tuesday?") == \
        "On Tuesday 10 November, for breakfast it's the Tuesday breakfast special."


def test_menu_written_as_sentences(conn, ollama, tmp_path):
    r = ingest_fixture(conn, "mess_menu_text.txt", tmp_path, llm=ollama)
    assert r.document.kind == "mess_menu" and r.document.repeats_weekly
    assert r.meals >= 10
    rows = {(row.weekday, row.meal): row.items.lower() for row in store.rows_of(conn, r.document.id)}
    assert "masala dosa" in rows[(2, "breakfast")]
    assert "rajma" in rows[(1, "lunch")]


def test_menu_photo_is_recognised_and_refused(conn, vision_model, tmp_path):
    """Needs the vision model too (OLLAMA_VISION_MODEL). The real model must still see that
    it's a menu, so Jarvis can say "send the PDF" rather than store it as something else."""
    from jarvis.documents.ingest import PHOTO_MENU
    from jarvis.documents.readers import UnreadableFile

    with pytest.raises(UnreadableFile) as e:
        ingest_fixture(conn, "mess_menu_photo.png", tmp_path, vision=vision_model)
    assert str(e.value) == PHOTO_MENU and store.list_all(conn) == []
