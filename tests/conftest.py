from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from jarvis.calendar import db, repo
from jarvis.config import TZ, Settings
from jarvis.core.llm import OllamaClient

# Every test runs at the same moment: Thursday 5 November 2026, 09:00 in Dubai.
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=TZ)
TODAY = NOW.date()


def nov(day: int, month: int = 11, year: int = 2026) -> date:
    return date(year, month, day)


def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(d, time(hour, minute), TZ)


def seed(conn, title: str, day: date, start: tuple[int, int] | None = None,
         end: tuple[int, int] | None = None, important: bool = False) -> repo.Event:
    if start is None:
        s = repo.local_midnight(day)
        e = s + timedelta(days=1)
    else:
        s = at(day, *start)
        e = at(day, *end) if end else s + timedelta(hours=1)
    return repo.create_event(
        conn, repo.NewEvent(title=title, start=s, end=e, all_day=start is None, important=important), NOW
    )


def all_events(conn) -> list[repo.Event]:
    return repo.list_events(conn, NOW - timedelta(days=400), NOW + timedelta(days=1000))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(scope="session")
def ollama() -> OllamaClient:
    s = Settings.from_env()
    client = OllamaClient(s.ollama_url, s.ollama_model)
    if problem := client.status():
        pytest.skip(problem)
    return client


@pytest.fixture(scope="session")
def vision_model() -> OllamaClient:
    s = Settings.from_env()
    client = OllamaClient(s.ollama_url, s.vision_model)
    if problem := client.status():
        pytest.skip(problem)
    return client


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def ingest_fixture(conn, name: str, files_dir, now=NOW, **kwargs):
    from jarvis.documents.ingest import ingest

    return ingest(conn, (FIXTURES / name).read_bytes(), name, now, files_dir, **kwargs)


# Autocorrect compares words with installed apps and projects; tests use a fixed list instead.
TEST_NAMES = ["Jarvis", "Claude Code", "VS Code", "Visual Studio Code", "Spotify", "YouTube", "WhatsApp", "Chrome",
              "Google Chrome", "Discord", "Notepad", "Calculator", "Steam", "Blender", "Cursor", "Ollama"]


@pytest.fixture(autouse=True)
def fixed_vocabulary(monkeypatch):
    from jarvis.core import autocorrect

    monkeypatch.setattr(autocorrect, "vocabulary", lambda max_age=600: list(TEST_NAMES))
    monkeypatch.setattr(autocorrect, "PENDING", autocorrect.Pending())
