"""HTTP API: token check, capture, direct event edits."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import NOW
from jarvis.config import Settings
from jarvis.core.api import create_app
from jarvis.core.llm import LLMError
from test_agent import ScriptedLLM, call

TOKEN = "test-token-not-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def make_client(tmp_path):
    def make(llm=None):
        settings = Settings(token=TOKEN, db_path=str(tmp_path / "jarvis.db"))
        return TestClient(create_app(settings, llm=llm or ScriptedLLM(), clock=lambda: NOW))
    return make


def test_refuses_to_start_without_token(tmp_path):
    with pytest.raises(RuntimeError, match="JARVIS_TOKEN"):
        create_app(Settings(token="", db_path=str(tmp_path / "x.db")), llm=ScriptedLLM())


def test_token_required(make_client):
    client = make_client()
    assert client.get("/health").status_code == 200
    assert client.get("/events").status_code == 401
    assert client.get("/events", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/events", headers=AUTH).status_code == 200


def test_capture(make_client):
    client = make_client(ScriptedLLM(call("create_event", title="DBMS quiz", date="monday", start_time="10am")))
    r = client.post("/capture", json={"text": "DBMS quiz monday 10am"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["reply"] == "Added DBMS quiz, Monday 9 November at 10 am."
    [ev] = client.get("/events", headers=AUTH).json()
    assert ev["source"] == "phone" and ev["start"] == "2026-11-09T10:00:00+04:00"


def test_capture_when_model_down(make_client):
    class Down:
        def chat(self, messages, tools):
            raise LLMError("Can't reach Ollama")

    r = make_client(Down()).post("/capture", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]


def test_event_crud(make_client):
    client = make_client()
    r = client.post("/events", json={"title": "Gym", "start": "2026-11-08T18:00"}, headers=AUTH)
    assert r.status_code == 201
    ev = r.json()
    assert ev["end"] == "2026-11-08T19:00:00+04:00" and ev["when"] == "Sunday 8 November at 6 pm"

    r = client.patch(f"/events/{ev['id']}", json={"start": "2026-11-08T19:30", "important": True}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["end"] == "2026-11-08T20:30:00+04:00" and r.json()["important"]

    listed = client.get("/events", params={"from": "2026-11-08", "to": "2026-11-08"}, headers=AUTH).json()
    assert [e["title"] for e in listed] == ["Gym"]
    assert client.get("/events", params={"from": "2026-11-09"}, headers=AUTH).json() == []

    assert client.delete(f"/events/{ev['id']}", headers=AUTH).status_code == 204
    assert client.delete(f"/events/{ev['id']}", headers=AUTH).status_code == 404
    assert client.get("/events", headers=AUTH).json() == []


def test_all_day_and_dedupe(make_client):
    client = make_client()
    body = {"title": "Lab report", "start": "2026-11-20", "all_day": True, "client_uuid": "phone-1"}
    first = client.post("/events", json=body, headers=AUTH)
    again = client.post("/events", json=body, headers=AUTH)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["end"] == "2026-11-21T00:00:00+04:00"


@pytest.mark.parametrize("body", [
    {"title": "Bad", "start": "2026-11-08T18:00", "end": "2026-11-08T17:00"},
    {"title": "Bad", "start": "not a date"},
    {"title": "", "start": "2026-11-08T18:00"},
])
def test_invalid_events_rejected(make_client, body):
    assert make_client().post("/events", json=body, headers=AUTH).status_code == 422
