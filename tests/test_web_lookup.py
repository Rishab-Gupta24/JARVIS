"""look_up: internet answers for things Jarvis doesn't know, with a fake network."""

from __future__ import annotations

import httpx
import pytest

from conftest import NOW
from jarvis.calendar import db
from jarvis.core import tools, web
from jarvis.core.toolkit import ToolContext

DDG = """
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FBlack_hole&rut=x">Black hole - Wikipedia</a>
<a class="result__snippet" href="#">A black hole is a region of spacetime where gravity is so strong that nothing escapes.</a>
<a rel="nofollow" class="result__a" href="https://www.youtube.com/watch?v=1">Black holes explained</a>
<a class="result__snippet" href="#">Video</a>
<a rel="nofollow" class="result__a" href="https://science.example.org/black-holes">What is a black hole?</a>
<a class="result__snippet" href="#">NASA explains.</a>
"""
PAGE = ("<html><nav>Menu Home About</nav><p>Black holes form when massive stars collapse at the end of their "
        "lives, squeezing matter into a tiny space.</p><p>Unrelated paragraph about cookies and privacy settings "
        "on this website for visitors.</p></html>")


def fake(wiki_status=200, dictionary=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        url = str(request.url)
        if "duckduckgo" in url:
            return httpx.Response(200, text=DDG)
        if "dictionaryapi.dev" in url:
            return httpx.Response(200, json=dictionary) if dictionary else httpx.Response(404, json={})
        if "rest_v1/page/summary/Black_hole" in url:
            return httpx.Response(wiki_status, json={"extract": "A black hole is a region of spacetime..."})
        if "science.example.org" in url:
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_wikipedia_summary_first():
    http, seen = fake()
    d = web.look_up_web("what is a black hole", http)
    assert d["source"] == "Wikipedia" and d["excerpt"].startswith("A black hole is a region")
    assert not any("youtube" in u for u in seen)


def test_wikipedia_refused_uses_its_snippet():
    http, _ = fake(wiki_status=403)
    d = web.look_up_web("what is a black hole", http)
    assert d["source"] == "Wikipedia" and "nothing escapes" in d["excerpt"]


def test_definitions_come_from_the_dictionary():
    entry = [{"meanings": [{"partOfSpeech": "adjective", "definitions": [
        {"definition": "Lasting a very short time.", "example": "ephemeral pleasures"}]}]}]
    http, seen = fake(dictionary=entry)
    d = web.look_up_web("ephemeral meaning", http)
    assert d == {"query": "ephemeral meaning", "found": True, "source": "the dictionary",
                 "excerpt": "ephemeral (adjective): Lasting a very short time. Example: ephemeral pleasures"}
    assert not any("duckduckgo" in u for u in seen)  # one quick request


def test_page_excerpt_keeps_the_relevant_part():
    http, _ = fake()
    text = web.page_excerpt("https://science.example.org/black-holes", "how do black holes form", http)
    assert text.startswith("Black holes form") and "Menu Home" not in text


@pytest.mark.parametrize("query, word", [
    ("define serendipity", "serendipity"), ("what does ubiquitous mean", "ubiquitous"), ("ephemeral", "ephemeral"),
    ("ephemeral meaning", "ephemeral"), ("who is the president of France", None), ("what is a black hole", None),
])
def test_which_queries_are_definitions(query, word):
    m = web._DEFINE.match(query)
    assert (m[1] if m else None) == word


def test_look_up_is_only_for_the_users_own_questions(monkeypatch):
    conn = db.connect(":memory:")
    monkeypatch.setattr(web, "look_up_web", lambda q: {"query": q, "found": True, "source": "x", "excerpt": "y"})
    own = ToolContext(conn=conn, now=NOW, raw_text="what is a black hole", allow_pc=True)
    r = tools.execute("look_up", {"query": "black hole"}, own)
    assert r.ok and r.confirmation is None and r.data["excerpt"] == "y"  # the model phrases it
    shared = ToolContext(conn=conn, now=NOW, raw_text="forwarded text", allow_pc=False)
    assert not tools.execute("look_up", {"query": "black hole"}, shared).ok
    assert "look_up" not in {s["function"]["name"] for s in tools.specs_for(False)}


def test_no_internet_is_said_plainly(monkeypatch):
    conn = db.connect(":memory:")

    def boom(q):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(web, "look_up_web", boom)
    r = tools.execute("look_up", {"query": "black hole"}, ToolContext(conn=conn, now=NOW, allow_pc=True))
    assert r.confirmation == "I couldn't reach the internet just now."


def test_contact_goes_in_the_client_name(monkeypatch):
    monkeypatch.delenv("JARVIS_WEB_CONTACT", raising=False)
    assert web.user_agent() == "JarvisPersonalAssistant/1.0 (local voice assistant)"
    monkeypatch.setenv("JARVIS_WEB_CONTACT", "https://github.com/someone")
    assert web.user_agent() == "JarvisPersonalAssistant/1.0 (local voice assistant; https://github.com/someone)"


@pytest.mark.llm
@pytest.mark.parametrize("said", ["What does ephemeral mean?", "Who wrote the theory of relativity?"])
def test_model_looks_things_up_and_names_the_source(ollama, monkeypatch, said):
    from jarvis.core import agent

    conn = db.connect(":memory:")
    asked = []

    def fake_look_up(query):
        asked.append(query)
        return {"query": query, "found": True, "source": "Wikipedia",
                "excerpt": "Ephemeral means lasting a very short time." if "ephemeral" in said.lower() else
                "The theory of relativity was developed by Albert Einstein between 1905 and 1915."}

    monkeypatch.setattr(web, "look_up_web", fake_look_up)
    r = agent.handle(said, conn, ollama, now=NOW, source="voice", session=said)
    assert asked, r.text
    assert ("short" in r.text.lower()) if "ephemeral" in said.lower() else ("einstein" in r.text.lower()), r.text
