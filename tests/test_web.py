"""Milestone 6: the web app's endpoints and the iPhone Calendar feed.
Today is Thursday 5 November 2026, 09:00 in Dubai."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import NOW, at, ingest_fixture, nov
from jarvis.calendar import db, ics, repo
from jarvis.config import Settings
from jarvis.core.api import create_app
from test_agent import ScriptedLLM, call

TOKEN, PHONE, FEED = "main-token-not-secret", "phonetokennotsecret2", "feed-token-not-secret-0123"
AUTH = {"Authorization": f"Bearer {PHONE}"}


class FakeStt:
    def __init__(self, heard="Hey Jarvis, what's on tomorrow?", fail=False):
        self.heard, self.fail, self.got = heard, fail, []

    def transcribe_file(self, data: bytes) -> str:
        self.got.append(data)
        if self.fail:
            raise RuntimeError("can't decode")
        return self.heard


@pytest.fixture
def make(tmp_path):
    def build(llm=None, stt=None, voice=None, **settings_kw):
        kw = dict(token=TOKEN, phone_token=PHONE, feed_token=FEED, public_url="https://pc.tail1234.ts.net:8443",
                  db_path=str(tmp_path / "j.db"), files_dir=str(tmp_path / "files"), inbox_dir=str(tmp_path / "in"))
        kw.update(settings_kw)
        settings = Settings(**kw)
        app = create_app(settings, llm=llm or ScriptedLLM(), clock=lambda: NOW, watch_inbox=False,
                         fetch_holidays=False, send_reminders=False, stt=stt or FakeStt(), voice=voice)
        return TestClient(app), settings
    return build


def add(settings, title, start, end=None, **kw):
    conn = db.connect(settings.db_path)
    try:
        return repo.create_event(conn, repo.NewEvent(title, start, end or start + timedelta(hours=1), **kw), NOW)
    finally:
        conn.close()


# ---------- the page ----------

def test_page_and_icons_need_no_token(make):
    c, _ = make()
    page = c.get("/app")
    assert page.status_code == 200 and "<title>Jarvis</title>" in page.text and "/ask-audio" in page.text
    assert c.get("/", follow_redirects=False).headers["location"] == "/app"
    assert c.get("/manifest.webmanifest").json()["start_url"] == "/app"
    for size in (180, 192, 512):
        r = c.get(f"/icon-{size}.png")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert c.get("/icon-64.png").status_code == 404
    # but data does
    assert c.get("/meals").status_code == 401 and c.get("/feed-url").status_code == 401


class FakeVoice:
    def __init__(self):
        self.said = []

    def synthesize(self, sentence):
        import numpy as np

        self.said.append(sentence)
        return np.zeros(2400, dtype=np.float32), 24000


def test_speak_returns_jarvis_voice_as_wav(make):
    import io
    import wave

    voice = FakeVoice()
    tc, _ = make(voice=voice)
    r = tc.post("/speak", json={"text": "Added Gym, Sunday 8 November at 6 pm. Anything else?"}, headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        assert w.getnframes() > 4800  # two sentences plus a pause
    assert voice.said == ["Added Gym, Sunday 8 November at 6 p.m.", "Anything else?"]
    assert tc.post("/speak", json={"text": " "}, headers=AUTH).status_code == 422
    assert tc.post("/speak", json={"text": "hi"}).status_code == 401


def test_page_has_no_secrets():
    page = (Path(__file__).parent.parent / "jarvis" / "web" / "app.html").read_text(encoding="utf-8")
    assert "tk_" not in page and "ts.net" not in page


# ---------- meals ----------

def test_meals_for_a_day(make, tmp_path):
    c, settings = make()
    conn = db.connect(settings.db_path)
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path / "files")
    conn.close()
    r = c.get("/meals", params={"date": "2026-11-11"}, headers=AUTH).json()
    assert r["meals"]["breakfast"] == "Poha, boiled egg, banana, tea/coffee" and len(r["meals"]) == 4
    assert c.get("/meals", headers=AUTH).json() == {"date": "2026-11-05", "meals": None}  # today: no menu


# ---------- calendar feed ----------

def test_feed_url(make):
    c, _ = make()
    r = c.get("/feed-url", headers=AUTH).json()
    assert r["https"] == f"https://pc.tail1234.ts.net:8443/calendar/{FEED}.ics"
    assert r["webcal"] == f"webcal://pc.tail1234.ts.net:8443/calendar/{FEED}.ics"
    c2, _ = make(feed_token="")
    assert c2.get("/feed-url", headers=AUTH).status_code == 404


def test_calendar_feed(make):
    c, settings = make()
    add(settings, "DBMS quiz", at(nov(9), 10), important=True, location="Room 101, Block A")
    add(settings, "Lab report due", repo.local_midnight(nov(20)), repo.local_midnight(nov(21)), all_day=True,
        notes="Submit on Moodle; bring the printout")
    old = add(settings, "Ancient history", at(nov(5), 8) - timedelta(days=100))
    r = c.get(f"/calendar/{FEED}.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:★ DBMS quiz" in body and "PRIORITY:1" in body
    assert "DTSTART:20261109T060000Z" in body and "DTEND:20261109T070000Z" in body  # 10 am Dubai = 06:00 UTC
    assert "LOCATION:Room 101\\, Block A" in body
    assert "DTSTART;VALUE=DATE:20261120" in body and "DTEND;VALUE=DATE:20261121" in body
    assert "DESCRIPTION:Submit on Moodle\\; bring the printout" in body
    assert f"jarvis-{old.id}@" not in body  # more than 60 days ago
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_calendar_feed_rejects_wrong_or_missing_token(make):
    c, _ = make()
    assert c.get("/calendar/wrong.ics").status_code == 404
    assert c.get(f"/calendar/{PHONE}.ics").status_code == 404  # the phone token isn't a feed token
    c2, _ = make(feed_token="")
    assert c2.get("/calendar/.ics").status_code == 404


def test_feed_token_never_reaches_the_access_log(make):
    import logging

    from jarvis.core.api import _HideFeedToken

    make()  # installs the filter
    log = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, _HideFeedToken) for f in log.filters)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("100.1.2.3:0", "GET", f"/calendar/{FEED}.ics", "1.1", 200), None)
    for f in log.filters:
        f.filter(record)
    assert FEED not in record.getMessage() and "/calendar/<feed-token>.ics" in record.getMessage()


def test_ics_folds_long_lines_and_escapes():
    e = repo.Event(1, "A very long title " * 8, NOW, NOW + timedelta(hours=1), False, None, "line one\nline two",
                   False, None, "text", None, None, NOW, False)
    text = ics.calendar_ics([e], NOW)
    lines = text.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:" + "A very long title " * 8 in unfolded
    assert "DESCRIPTION:line one\\nline two" in unfolded


# ---------- the mic ----------

def test_ask_audio(make):
    stt = FakeStt()
    nothing = {"role": "assistant", "content": "Nothing tomorrow."}
    llm = ScriptedLLM(call("list_events", period="tomorrow"), nothing, call("list_events", period="tomorrow"), nothing)
    c, _ = make(llm=llm, stt=stt)
    r = c.post("/ask-audio", files={"file": ("speech.m4a", b"\x00" * 500, "audio/mp4")}, headers=AUTH).json()
    assert r == {"heard": "what's on tomorrow?", "reply": "Nothing tomorrow."}  # "Hey Jarvis" stripped
    assert len(stt.got[0]) == 500
    raw = c.post("/ask-audio", content=b"\x01" * 300, headers=AUTH).json()  # raw body works too
    assert raw["heard"] and stt.got[-1] == b"\x01" * 300


@pytest.mark.parametrize("stt, body, reply", [
    (FakeStt(), b"x", "I didn't hear anything."),                          # too short to be audio
    (FakeStt(heard="Thank you."), b"\x00" * 500, "I didn't catch that."),  # Whisper noise
    (FakeStt(fail=True), b"\x00" * 500, "Sorry, I couldn't make out that recording."),
])
def test_ask_audio_problems(make, stt, body, reply):
    c, _ = make(stt=stt)
    assert c.post("/ask-audio", content=body, headers=AUTH).json() == {"heard": "", "reply": reply}
    assert c.post("/ask-audio", content=body).status_code == 401


@pytest.mark.llm
def test_iphone_style_recording_through_real_whisper(make, ollama):
    """Kokoro says it, PyAV encodes AAC in MP4 (what iPhone Safari's MediaRecorder makes),
    and real Whisper + qwen3 answer it."""
    import io

    import av
    import numpy as np

    settings = Settings.from_env()
    models = Path(settings.models_dir)
    if not (models / "kokoro" / "kokoro-v1.0.onnx").exists() or not (models / "whisper").exists():
        pytest.skip("voice models not downloaded")
    from jarvis.voice.stt import Transcriber
    from jarvis.voice.tts import KokoroVoice

    audio, rate = KokoroVoice(models, "am_michael").synthesize("What's on tomorrow?")
    buf = io.BytesIO()
    with av.open(buf, "w", format="mp4") as out:
        stream = out.add_stream("aac", rate=rate)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray((audio * 32767).astype(np.int16)[None, :], format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    m4a = buf.getvalue()
    assert m4a[4:8] == b"ftyp"

    stt = Transcriber(settings.whisper_model, settings.whisper_device, settings.whisper_compute, models)
    c, s = make(llm=ollama, stt=stt)
    add(s, "Dentist appointment", at(nov(6), 16, 30))
    r = c.post("/ask-audio", files={"file": ("speech.m4a", m4a, "audio/mp4")}, headers=AUTH).json()
    assert "tomorrow" in r["heard"].lower(), r
    assert "dentist" in r["reply"].lower(), r
