"""Autocorrect: misheard or misspelt names fixed silently when sure, asked about when not, and remembered."""

from __future__ import annotations

import pytest

from conftest import NOW, TEST_NAMES
from jarvis.calendar import db
from jarvis.core import agent, autocorrect
from test_agent import ScriptedLLM, say


@pytest.mark.parametrize("heard, fixed", [
    ("open could code", "open Claude Code"),                    # the one from the first day
    ("build me a website with cloud code", "build me a website with Claude Code"),
    ("close spot if I", "close Spotify"),
    ("open spotfy for me", "open Spotify for me"),
    ("open crome and spotfy", "open Chrome and Spotify"),
    ("open my jarvs project", "open my Jarvis project"),
    ("open blunder", "open Blender"),
    ("close the curser window", "close the Cursor window"),
    ("open you tube", "open YouTube"),
    ("open vs cold", "open VS Code"),
])
def test_sure_fixes_are_silent(heard, fixed):
    assert autocorrect.correct(heard, TEST_NAMES) == (fixed, None)


@pytest.mark.parametrize("said", [
    "start coding", "open the downloads folder", "set an alarm for 7", "what is on my calendar",
    "what's for lunch and dinner tomorrow", "play some music", "open settings", "start a timer for ten minutes",
    "remind me to call mom on friday", "turn the volume up", "open youtube",
])
def test_ordinary_words_are_left_alone(said):
    assert autocorrect.correct(said, TEST_NAMES) == (said, None)


@pytest.mark.parametrize("heard, name, fixed", [
    ("start stream", "Steam", "start Steam"),                    # could be OBS streaming: ask
    ("open visual studio", "Visual Studio Code", "open Visual Studio Code"),
    ("open lama", "Ollama", "open Ollama"),
])
def test_unsure_fixes_are_asked(heard, name, fixed):
    text, fix = autocorrect.correct(heard, TEST_NAMES)
    assert text == heard and fix.name == name and fix.text == fixed and fix.question == f"Did you mean {name}?"


def test_learned_answers_win():
    assert autocorrect.correct("start stream", TEST_NAMES, {"stream": None}) == ("start stream", None)
    assert autocorrect.correct("open lama", TEST_NAMES, {"lama": "Ollama"}) == ("open Ollama", None)


def test_sound_key():
    assert autocorrect.sound("Claude") == autocorrect.sound("could")
    assert autocorrect.sound("Spotify") == autocorrect.sound("spot if I")
    assert autocorrect.sound("Chrome") == autocorrect.sound("crome")


def test_hotwords_start_with_the_key_names_and_stay_short():
    words = autocorrect.hotwords(TEST_NAMES + ["echo-duel", "Task Scheduler", "Blender"], limit=120)
    assert words.startswith("Jarvis, Claude Code, VS Code, Spotify, YouTube, WhatsApp")
    assert "echo-duel" in words and "Task Scheduler" not in words and len(words) <= 120


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def two(text):
    # A PC request answered without a tool is sent back once (agent._MUST_LOOK), so script two replies.
    return ScriptedLLM(say(text), say(text))


def test_agent_fixes_silently_before_the_model(conn):
    llm = two("Closing Spotify.")
    agent.handle("close spot if I", conn, llm, now=NOW, source="voice", session="v")
    assert llm.calls[0][-1]["content"].endswith("close Spotify")


def test_yes_runs_the_fix_and_remembers_it(conn, monkeypatch):
    memory = agent.SessionMemory()
    llm = two("Opening Ollama.")
    r = agent.handle("open lama", conn, llm, now=NOW, source="voice", session="v", memory=memory)
    assert r.text == "Did you mean Ollama?" and llm.calls == []   # asked before the model was used
    agent.handle("yes", conn, llm, now=NOW, source="voice", session="v", memory=memory)
    assert llm.calls[0][-1]["content"].endswith("open Ollama")
    assert autocorrect.learned(conn) == {"lama": "Ollama"}
    llm2 = two("Opening Ollama.")
    r = agent.handle("open lama", conn, llm2, now=NOW, source="voice", session="v")
    assert r.text == "Opening Ollama." and llm2.calls[0][-1]["content"].endswith("open Ollama")  # silent now


def test_no_runs_it_as_heard_and_never_asks_again(conn):
    llm = ScriptedLLM(*[say("Starting the stream.")] * 4)
    assert agent.handle("start stream", conn, llm, now=NOW, source="voice", session="v").text == \
        "Did you mean Steam?"
    agent.handle("no", conn, llm, now=NOW, source="voice", session="v")
    assert llm.calls[0][-1]["content"].endswith("start stream")
    assert agent.handle("start stream", conn, llm, now=NOW, source="voice", session="v").text == \
        "Starting the stream."


def test_no_i_meant_learns_the_right_name(conn):
    llm = two("Opening Discord.")
    agent.handle("open lama", conn, llm, now=NOW, source="voice", session="v")
    agent.handle("No, I meant Discord", conn, llm, now=NOW, source="voice", session="v")
    assert llm.calls[0][-1]["content"].endswith("open Discord")
    assert autocorrect.learned(conn)["lama"] == "Discord"


def test_something_else_is_a_new_request(conn):
    llm = ScriptedLLM(say("Nothing on tomorrow."))
    agent.handle("open lama", conn, llm, now=NOW, source="voice", session="v")
    agent.handle("what's on tomorrow?", conn, llm, now=NOW, source="voice", session="v")
    assert llm.calls[0][-1]["content"].endswith("what's on tomorrow?")
    assert autocorrect.learned(conn) == {}


def test_shared_messages_are_never_corrected(conn):
    llm = ScriptedLLM(say("No events in that message."))
    agent.handle("open lama", conn, llm, now=NOW, shared=True)
    assert "open lama" in llm.calls[0][-1]["content"]
