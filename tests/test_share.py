"""Milestone 5: what the iPhone Share Sheet sends (forwarded messages, files, photos)
and the phone token. Today is Thursday 5 November 2026, 09:00."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, NOW, all_events, at, nov
from jarvis.config import Settings
from jarvis.core import agent
from jarvis.core.api import create_app
from jarvis.core.phone import new_phone_token
from jarvis.core.share import share_file, share_text
from jarvis.documents.ingest import PHOTO_MENU
from jarvis.documents import store
from test_agent import ScriptedLLM, call, say
from test_documents import FakeVision


@pytest.mark.parametrize("pasted, cleaned", [
    ("[05/11/26, 8:14 PM] Rahul CR: OS assignment due Sunday 11:59pm",
     "OS assignment due Sunday 11:59pm"),
    ("05/11/2026, 20:14 - Rahul CR: Lab viva on Thursday 2pm\n05/11/2026, 20:15 - Rahul CR: In Lab 3",
     "Lab viva on Thursday 2pm\nIn Lab 3"),
    ("Forwarded\nClub meeting tomorrow 6pm", "Club meeting tomorrow 6pm"),
    ("Club meeting tomorrow 6pm, at 6:30 we eat", "Club meeting tomorrow 6pm, at 6:30 we eat"),  # untouched
])
def test_clean_shared(pasted, cleaned):
    assert agent.clean_shared(pasted) == cleaned


def test_shared_message_reaches_the_model_without_the_chat_header(conn):
    llm = ScriptedLLM(call("create_event", title="DBMS lab viva", date="Thursday 12th", start_time="2 pm",
                           location="Lab 3"))
    r = agent.handle("[05/11/26, 8:14 PM] Rahul CR: DBMS lab viva on Thursday 12th at 2 pm in Lab 3",
                     conn, llm, now=NOW, shared=True)
    prompt = llm.calls[0][-1]["content"]
    assert "forwarding you a message" in prompt and "8:14 PM" not in prompt and "Rahul CR" not in prompt
    assert r.text == "Added DBMS lab viva, Thursday 12 November at 2 pm."
    [ev] = all_events(conn)
    assert ev.location == "Lab 3" and ev.raw_text == "DBMS lab viva on Thursday 12th at 2 pm in Lab 3"


def test_guards_look_at_the_message_not_the_instructions(conn):
    # the model flags it important, but the message never asked for that
    llm = ScriptedLLM(call("create_event", title="Club meeting", date="tomorrow", start_time="6pm", important=True))
    agent.handle("Club meeting tomorrow 6pm", conn, llm, now=NOW, shared=True)
    assert not all_events(conn)[0].important
    # a message that does ask for it
    llm = ScriptedLLM(call("create_event", title="Fee payment", date="20 nov", important=True))
    agent.handle("Fee payment deadline 20 Nov, very important!", conn, llm, now=NOW, shared=True)
    assert next(e for e in all_events(conn) if e.title == "Fee payment").important


def test_food_words_in_a_shared_message_are_not_a_menu_question(conn):
    llm = ScriptedLLM(say("That message doesn't mention any event."))
    r = agent.handle("lol the mess lunch was terrible today", conn, llm, now=NOW, shared=True)
    assert r.text == "That message doesn't mention any event." and len(llm.calls) == 1


def test_share_text_routes_documents_and_messages(conn, tmp_path):
    menu = "| Day | Breakfast | Lunch | Dinner |\n|---|---|---|---|\n" + "\n".join(
        f"| {d} | Poha | Dal | Roti |" for d in ("Monday", "Tuesday", "Wednesday"))
    r = share_text(conn, ScriptedLLM(), menu + "\nMenu for 9-15 November 2026", NOW, tmp_path)
    assert r.stored["kind"] == "mess_menu" and r.reply.startswith("Got the mess menu for 9 to 15 November")

    llm = ScriptedLLM(call("create_event", title="Hackathon", date="20 nov"))
    r = share_text(conn, llm, "Hackathon registrations close on 20 Nov", NOW, tmp_path)
    assert r.stored is None and r.events_added == 1 and r.reply == "Added Hackathon, Friday 20 November."


def test_share_file_menu_is_stored_without_asking_the_model(conn, tmp_path):
    r = share_file(conn, ScriptedLLM(), (FIXTURES / "mess_menu_week.pdf").read_bytes(), "menu.pdf", NOW, tmp_path)
    assert r.stored["kind"] == "mess_menu" and r.events_added == 0  # ScriptedLLM() would fail if called


class NoticeVision:
    def transcribe(self, image: bytes) -> str:
        return "NOTICE\nGuest lecture on Machine Learning on Monday 9 November at 11 am, Seminar Hall."


def test_screenshot_of_an_announcement_becomes_an_event(conn, tmp_path):
    llm = ScriptedLLM(call("create_event", title="Guest lecture on Machine Learning", date="Monday 9 November",
                           start_time="11 am", location="Seminar Hall"))
    r = share_file(conn, llm, b"png-bytes", "IMG_0042.png", NOW, tmp_path, vision=NoticeVision())
    assert r.stored["kind"] == "other" and r.events_added == 1
    assert r.reply == "Added Guest lecture on Machine Learning, Monday 9 November at 11 am."
    assert all_events(conn)[0].location == "Seminar Hall"


# ---------- over HTTP ----------

TOKEN, PHONE = "main-token-not-secret", "phonetokennotsecret2"


class FakePush:
    def __init__(self):
        self.sent = []

    def send_text(self, title, message):
        self.sent.append((title, message))


@pytest.fixture
def client(tmp_path, monkeypatch):
    def make(llm, push=None):
        monkeypatch.setattr("jarvis.core.api.push_from", lambda settings: push)
        settings = Settings(token=TOKEN, phone_token=PHONE, db_path=str(tmp_path / "j.db"),
                            files_dir=str(tmp_path / "files"), inbox_dir=str(tmp_path / "inbox"))
        return TestClient(create_app(settings, llm=llm, clock=lambda: NOW, vision=FakeVision(),
                                     watch_inbox=False, fetch_holidays=False, send_reminders=False))
    return make


def test_phone_token_works_and_others_dont(client):
    c = client(ScriptedLLM())
    assert c.get("/events", headers={"Authorization": f"Bearer {PHONE}"}).status_code == 200
    assert c.get("/events", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.get("/events", headers={"Authorization": f"Bearer {PHONE}x"}).status_code == 401
    assert c.get("/events", headers={"Authorization": "Bearer "}).status_code == 401
    assert c.get("/events").status_code == 401
    # what the iPhone keyboard does to a typed token still works: capitals, spaces, no "Bearer"
    for typed in (f"Bearer {PHONE.capitalize()}", f"bearer  {PHONE} ", PHONE, f"BEARER {PHONE.upper()}"):
        assert c.get("/events", headers={"Authorization": typed}).status_code == 200, typed
    # the main token is not case-insensitive
    assert c.get("/events", headers={"Authorization": f"Bearer {TOKEN.upper()}"}).status_code == 401


def test_share_endpoint_with_a_whatsapp_message(client):
    llm = ScriptedLLM(call("create_event", title="DBMS lab viva", date="Thursday 12th", start_time="2pm"))
    c = client(llm)
    r = c.post("/share", data={"text": "[05/11/26, 8:14 PM] Rahul: DBMS lab viva Thursday 12th 2pm"},
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.status_code == 200
    assert r.json() == {"reply": "Added DBMS lab viva, Thursday 12 November at 2 pm.", "events_added": 1,
                        "document": None}
    [ev] = c.get("/events", params={"from": "2026-11-12"}, headers={"Authorization": f"Bearer {PHONE}"}).json()
    assert ev["source"] == "phone"


def test_share_endpoint_photo_is_read_in_the_background_and_pushed(client):
    push = FakePush()
    c = client(ScriptedLLM(), push)
    r = c.post("/share", files={"file": ("IMG_1.jpg", b"jpeg-bytes", "image/jpeg")},
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.status_code == 202 and "notification" in r.json()["reply"]
    for _ in range(50):
        if push.sent:
            break
        time.sleep(0.1)
    assert push.sent == [("Jarvis", f"I couldn't read IMG_1.jpg: {PHOTO_MENU}")]


def test_share_endpoint_photo_without_push_is_read_straight_away(client, tmp_path):
    c = client(ScriptedLLM(), push=None)
    r = c.post("/share", files={"file": ("IMG_1.jpg", b"jpeg-bytes", "image/jpeg")},
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.status_code == 422 and r.json()["detail"] == PHOTO_MENU


def test_share_endpoint_errors(client):
    c = client(ScriptedLLM())
    auth = {"Authorization": f"Bearer {PHONE}"}
    assert c.post("/share", data={"text": "  "}, headers=auth).status_code == 422
    assert c.post("/share", files={"file": ("notes.docx", b"x")}, headers=auth).status_code == 422
    assert c.post("/share", data={"text": "hi"}).status_code == 401


def test_capture_shared_flag(client):
    llm = ScriptedLLM(call("create_event", title="Club meeting", date="tomorrow", start_time="6pm"))
    c = client(llm)
    r = c.post("/capture", json={"text": "Club meeting tomorrow 6pm", "shared": True},
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.json()["reply"] == "Added Club meeting, tomorrow at 6 pm."
    assert "forwarding you a message" in llm.calls[0][-1]["content"]


def test_phone_token_is_easy_to_type():
    token = new_phone_token()
    assert len(token) == 20 and token.isalnum() and token == token.lower()
    assert not set(token) & set("l1o0")


# ---------- the simple Shortcut endpoints: raw body in, plain text out ----------

def heic_of(png_path) -> bytes:
    import io

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    out = io.BytesIO()
    Image.open(png_path).convert("RGB").save(out, format="HEIF")
    return out.getvalue()


class JpegOnlyVision(FakeVision):
    """Proves an iPhone HEIC photo is turned into JPEG before the vision model sees it."""

    def transcribe(self, image: bytes) -> str:
        assert image.startswith(b"\xff\xd8\xff"), "vision model got something other than JPEG"
        return super().transcribe(image)


@pytest.mark.parametrize("data, ext", [
    (b"%PDF-1.7 ...", ".pdf"), (b"\xff\xd8\xff\xe0...", ".jpg"), (b"\x89PNG\r\n...", ".png"),
    (b"\x00\x00\x00\x18ftypheic....", ".heic"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
    (b"PK\x03\x04...", ".xlsx"), ("Club meeting tomorrow 6pm".encode(), ".txt"),
])
def test_sniff(data, ext):
    from jarvis.documents.readers import sniff

    assert sniff(data) == ext


def test_ask_plain_text_in_and_out(client):
    c = client(ScriptedLLM(call("create_event", title="Gym", date="sunday", start_time="6pm")))
    r = c.post("/ask", content="add gym on sunday at 6pm".encode(), headers={"Authorization": f"Bearer {PHONE}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == "Added Gym, Sunday 8 November at 6 pm."
    assert c.post("/ask", content=b"  ", headers={"Authorization": f"Bearer {PHONE}"}).text == \
        "I didn't hear anything."
    assert c.post("/ask", content=b"hi").status_code == 401


@pytest.mark.parametrize("how", ["json", "form-text", "form-file", "raw"])
def test_ask_accepts_every_way_a_shortcut_can_send(client, how):
    c = client(ScriptedLLM(call("create_event", title="Gym", date="sunday", start_time="6pm")))
    auth, said = {"Authorization": f"Bearer {PHONE}"}, "add gym on sunday at 6pm"
    send = {"json": dict(json={"text": said}), "form-text": dict(data={"text": said}),
            "form-file": dict(files={"file": ("text.txt", said.encode(), "text/plain")}),
            "raw": dict(content=said.encode())}[how]
    assert c.post("/ask", headers=auth, **send).text == "Added Gym, Sunday 8 November at 6 pm."


def test_send_form_with_text_and_photo_fields_prefers_the_photo(client):
    """The guide's Shortcut 2 puts Shortcut Input in both a text and a file field."""
    c = client(ScriptedLLM())
    r = c.post("/send", headers={"Authorization": f"Bearer {PHONE}"},
               data={"text": "IMG_0042.HEIC"},
               files={"file": ("IMG_0042.jpg", (FIXTURES / "mess_menu_photo.png").read_bytes(), "image/png")})
    assert r.text == PHOTO_MENU  # the photo was used (and refused as a menu), not the text "IMG_0042.HEIC"


def test_send_form_with_only_text_is_a_message(client):
    c = client(ScriptedLLM(call("create_event", title="Club meeting", date="tomorrow", start_time="6pm")))
    r = c.post("/send", headers={"Authorization": f"Bearer {PHONE}"},
               data={"text": "Club meeting tomorrow 6pm"}, files={"file": ("empty.txt", b"", "text/plain")})
    assert r.text == "Added Club meeting, tomorrow at 6 pm."


def test_send_a_whatsapp_message(client):
    c = client(ScriptedLLM(call("create_event", title="DBMS lab viva", date="Thursday 12th", start_time="2pm")))
    r = c.post("/send", content="[05/11/26, 8:14 PM] Rahul: DBMS lab viva Thursday 12th 2pm".encode(),
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.text == "Added DBMS lab viva, Thursday 12 November at 2 pm."


def test_send_an_iphone_heic_photo_of_the_menu(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.core.api.push_from", lambda settings: None)
    settings = Settings(token=TOKEN, phone_token=PHONE, db_path=str(tmp_path / "j.db"),
                        files_dir=str(tmp_path / "files"), inbox_dir=str(tmp_path / "inbox"))
    c = TestClient(create_app(settings, llm=ScriptedLLM(), clock=lambda: NOW, vision=JpegOnlyVision(),
                              watch_inbox=False, fetch_holidays=False, send_reminders=False))
    r = c.post("/send", content=heic_of(FIXTURES / "mess_menu_photo.png"),
               headers={"Authorization": f"Bearer {PHONE}"})
    assert r.text == PHOTO_MENU  # converted to JPEG (JpegOnlyVision checks), read, then refused as a menu


def test_send_a_photo_with_push_answers_now_and_notifies_later(client):
    push = FakePush()
    c = client(ScriptedLLM(), push)
    r = c.post("/send", content=b"\xff\xd8\xff\xe0 jpeg", headers={"Authorization": f"Bearer {PHONE}"})
    assert "notification" in r.text
    for _ in range(50):
        if push.sent:
            break
        time.sleep(0.1)
    assert push.sent == [("Jarvis", PHOTO_MENU)]


def test_send_a_notice_photo_adds_its_event(tmp_path, monkeypatch):
    push = FakePush()
    monkeypatch.setattr("jarvis.core.api.push_from", lambda settings: push)
    settings = Settings(token=TOKEN, phone_token=PHONE, db_path=str(tmp_path / "j.db"),
                        files_dir=str(tmp_path / "files"), inbox_dir=str(tmp_path / "inbox"))
    llm = ScriptedLLM(call("create_event", title="Guest lecture on Machine Learning", date="Monday 9 November",
                           start_time="11 am"))
    c = TestClient(create_app(settings, llm=llm, clock=lambda: NOW, vision=NoticeVision(),
                              watch_inbox=False, fetch_holidays=False, send_reminders=False))
    r = c.post("/send", content=b"\xff\xd8\xff\xe0 jpeg", headers={"Authorization": f"Bearer {PHONE}"})
    assert "notification" in r.text
    for _ in range(50):
        if push.sent:
            break
        time.sleep(0.1)
    assert push.sent == [("Jarvis", "Added Guest lecture on Machine Learning, Monday 9 November at 11 am.")]


def test_send_a_pdf_and_junk(client):
    c = client(ScriptedLLM())
    auth = {"Authorization": f"Bearer {PHONE}"}
    r = c.post("/send", content=(FIXTURES / "mess_menu_week.pdf").read_bytes(), headers=auth)
    assert r.text == "Got the mess menu for 9 to 15 November, 28 meals."
    junk = c.post("/send", content=b"PK\x03\x04 not really a spreadsheet", headers=auth)
    assert junk.status_code == 200 and junk.text.startswith("I couldn't read that xlsx file")
    damaged = c.post("/send", content=b"%PDF-1.7 truncated", headers=auth)
    assert damaged.text.startswith("I couldn't read that pdf file")
    assert c.post("/send", content=b"", headers=auth).text == "Nothing was shared."


# ---------- through the real model ----------

def _events(conn):
    return {e.title.lower(): e for e in all_events(conn)}


@pytest.mark.llm
def test_whatsapp_lab_viva(conn, ollama):
    agent.handle("Hi everyone! DBMS lab viva is on Thursday 12th at 2 pm in Lab 3. Bring your record books.",
                 conn, ollama, now=NOW, shared=True)
    [ev] = all_events(conn)
    assert "viva" in ev.title.lower() and ev.start == at(nov(12), 14)


@pytest.mark.llm
def test_whatsapp_copy_with_header(conn, ollama):
    agent.handle("[05/11/26, 8:14 PM] Rahul CR: OS assignment due this Sunday 11:59pm, no extensions!",
                 conn, ollama, now=NOW, shared=True)
    [ev] = all_events(conn)
    assert "assignment" in ev.title.lower() and ev.start == at(nov(8), 23, 59)


@pytest.mark.llm
def test_whatsapp_two_events_in_one_message(conn, ollama):
    agent.handle("Club meeting tomorrow 6pm at the amphitheatre. Also, hackathon registrations close on 20 Nov.",
                 conn, ollama, now=NOW, shared=True)
    events = all_events(conn)
    assert len(events) == 2, [(e.title, e.start) for e in events]
    meeting = next(e for e in events if "meeting" in e.title.lower())
    hackathon = next(e for e in events if "hackathon" in e.title.lower())
    assert meeting.start == at(nov(6), 18) and hackathon.start.date() == nov(20)


@pytest.mark.llm
def test_whatsapp_dinner_party_is_an_event(conn, ollama):
    agent.handle("Dinner party at Aisha's place on Saturday at 8pm, don't be late!", conn, ollama,
                 now=NOW, shared=True)
    [ev] = all_events(conn)
    assert ev.start == at(nov(7), 20)


@pytest.mark.llm
def test_chit_chat_adds_nothing(conn, ollama):
    r = agent.handle("lol did you see the match yesterday?? crazy ending", conn, ollama, now=NOW, shared=True)
    assert all_events(conn) == [] and r.text
