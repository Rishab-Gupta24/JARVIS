"""Milestone 4 without a phone: when reminders go out, what they say, and what
happens when the PC was off or the push fails. Today is Thursday 5 November 2026."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from conftest import NOW, at, nov
from jarvis.calendar import db, repo
from jarvis.notify import server
from jarvis.notify.ntfy import NtfyError, NtfySender
from jarvis.notify.reminders import MAX_ATTEMPTS, Dispatcher, describe, history


class Phone:
    def __init__(self, fail_times: int = 0):
        self.got, self.fail_times = [], fail_times

    def send(self, notice):
        if self.fail_times:
            self.fail_times -= 1
            raise NtfyError("server not reachable")
        self.got.append(notice)


class Mouth:
    def __init__(self, broken=False):
        self.said, self.broken = [], broken

    def send(self, notice):
        if self.broken:
            raise RuntimeError("no speakers")
        self.said.append(notice.spoken)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "jarvis.db")


def add(path, title, start, end=None, important=True, all_day=False, location=None):
    conn = db.connect(path)
    try:
        return repo.create_event(conn, repo.NewEvent(
            title, start, end or start.replace(hour=start.hour + 1) if not all_day else end,
            all_day=all_day, important=important, location=location), NOW)
    finally:
        conn.close()


def dispatcher(path, clock, push, extras=None):
    log = []
    d = Dispatcher(lambda: db.connect(path), lambda: clock[0], push, extras, log.append)
    return d, log


def statuses(path, event_id):
    conn = db.connect(path)
    try:
        return [s for _, s in history(conn, event_id)]
    finally:
        conn.close()


def test_three_reminders_on_time(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    clock, phone = [NOW], Phone()
    d, _ = dispatcher(path, clock, phone)
    assert d.run_once() == []  # nothing due yet
    for when in (at(nov(8), 10), at(nov(9), 9), at(nov(9), 9, 50)):
        clock[0] = when
        d.run_once()
    assert [(n.title, n.message, n.priority) for n in phone.got] == [
        ("DBMS quiz", "Tomorrow at 10 am", "default"),
        ("DBMS quiz", "In 1 hour, at 10 am", "default"),
        ("DBMS quiz", "In 10 minutes, at 10 am", "high"),
    ]
    assert statuses(path, quiz.id) == ["sent", "sent", "sent"]
    clock[0] = at(nov(9), 9, 55)
    assert d.run_once() == []  # nothing is sent twice


def test_pc_was_off_sends_only_the_latest(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    clock, phone = [at(nov(9), 9, 55)], Phone()
    d, _ = dispatcher(path, clock, phone)
    d.run_once()
    assert [n.message for n in phone.got] == ["In 5 minutes, at 10 am"]
    assert statuses(path, quiz.id) == ["missed", "missed", "sent"]


def test_too_late_sends_nothing(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    clock, phone = [at(nov(9), 10, 30)], Phone()
    d, log = dispatcher(path, clock, phone)
    d.run_once()
    assert phone.got == [] and statuses(path, quiz.id) == ["missed"] * 3
    assert any("missed DBMS quiz" in line for line in log)


def test_all_day_event(path):
    report = add(path, "Lab report due", at(nov(20), 0), at(nov(21), 0), all_day=True)
    clock, phone = [at(nov(19), 20)], Phone()
    d, _ = dispatcher(path, clock, phone)
    d.run_once()
    clock[0] = at(nov(20), 8)
    d.run_once()
    assert [(n.message, n.priority) for n in phone.got] == [("Tomorrow", "default"), ("Today", "high")]
    assert statuses(path, report.id) == ["sent", "sent"]


def test_failed_push_is_retried_then_given_up(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    clock, phone = [at(nov(9), 9, 50)], Phone(fail_times=2)
    d, log = dispatcher(path, clock, phone)
    d.run_once()
    d.run_once()
    assert phone.got == [] and statuses(path, quiz.id)[-1] == "pending"
    d.run_once()
    assert len(phone.got) == 1 and statuses(path, quiz.id)[-1] == "sent"

    gym = add(path, "Gym", at(nov(10), 18))
    clock[0] = at(nov(10), 17, 50)
    d2, log2 = dispatcher(path, clock, Phone(fail_times=99))
    for _ in range(MAX_ATTEMPTS + 2):
        d2.run_once()
    assert statuses(path, gym.id)[-1] == "failed"
    conn = db.connect(path)
    row = conn.execute("SELECT attempts, last_error FROM reminders WHERE event_id = ? ORDER BY fire_at DESC",
                       (gym.id,)).fetchone()
    assert row["attempts"] == MAX_ATTEMPTS and "not reachable" in row["last_error"]
    assert any("giving up" in line for line in log2)


def test_serve_and_voice_never_both_send(path):
    add(path, "DBMS quiz", at(nov(9), 10))
    clock, a, b = [at(nov(9), 9, 50)], Phone(), Phone()
    d1, _ = dispatcher(path, clock, a)
    d2, _ = dispatcher(path, clock, b)
    d1.run_once()
    d2.run_once()
    assert len(a.got) + len(b.got) == 1


def test_spoken_on_the_pc_too_and_never_blocks_the_push(path):
    add(path, "DBMS quiz", at(nov(9), 10), location="Room 101")
    clock, phone, mouth = [at(nov(9), 9, 50)], Phone(), Mouth()
    d, _ = dispatcher(path, clock, phone, [mouth, Mouth(broken=True)])
    d.run_once()
    assert phone.got[0].message == "In 10 minutes, at 10 am, Room 101"
    assert mouth.said == ["Reminder: DBMS quiz, in 10 minutes, at 10 am."]


def test_speaking_only_when_the_phone_is_not_set_up(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    clock, mouth = [at(nov(9), 9, 50)], Mouth()
    d, _ = dispatcher(path, clock, None, [mouth])
    d.run_once()
    assert len(mouth.said) == 1 and statuses(path, quiz.id)[-1] == "sent"
    with pytest.raises(ValueError):
        Dispatcher(lambda: db.connect(path), lambda: NOW, None, [])


def test_crash_while_sending_is_recovered(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    conn = db.connect(path)
    conn.execute("UPDATE reminders SET status = 'sending' WHERE event_id = ?", (quiz.id,))
    conn.commit()
    clock, phone = [at(nov(9), 9, 50)], Phone()
    d, _ = dispatcher(path, clock, phone)
    d.recover()
    d.run_once()
    assert len(phone.got) == 1


def test_unimportant_or_deleted_events_send_nothing(path):
    quiz = add(path, "DBMS quiz", at(nov(9), 10))
    conn = db.connect(path)
    repo.update_event(conn, quiz.id, NOW, important=False)
    gym = add(path, "Gym", at(nov(9), 18))
    repo.delete_event(conn, gym.id, NOW)
    clock, phone = [at(nov(9), 17, 55)], Phone()
    dispatcher(path, clock, phone)[0].run_once()
    assert phone.got == []


@pytest.mark.parametrize("minutes, message", [
    (1, "In 1 minute, at 9:01 am"), (45, "In 45 minutes, at 9:45 am"), (90, "In 2 hours, at 10:30 am"),
])
def test_describe_lead_times(minutes, message):
    from datetime import timedelta

    start = NOW + timedelta(minutes=minutes)
    event = repo.Event(1, "X", start, start + timedelta(hours=1), False, None, None, True, None, "text",
                       None, None, NOW, False)
    assert describe(event, NOW).message == message


def test_ntfy_sender_posts_json_with_the_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content)["topic"] == "wrong":
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(200, json={"id": "abc123"})

    sender = NtfySender("http://127.0.0.1:2586/", "jarvis-topic", "tk_x", transport=httpx.MockTransport(handler))
    assert sender.publish("DBMS quiz", "In 10 minutes", "high", ["alarm_clock"]) == "abc123"
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://127.0.0.1:2586"
    assert seen[0].headers["Authorization"] == "Bearer tk_x"
    assert body == {"topic": "jarvis-topic", "title": "DBMS quiz", "message": "In 10 minutes",
                    "priority": 4, "tags": ["alarm_clock"]}
    with pytest.raises(NtfyError, match="403"):
        NtfySender("http://x", "wrong", "tk_x", transport=httpx.MockTransport(handler)).publish("a", "b")
    with pytest.raises(ValueError):
        NtfySender("http://x", "", "tk_x")


def test_server_config_and_env(tmp_path):
    text = server.config_text("https://pc.tail1234.ts.net", "jarvis-abc", "$2a$10$jj", "$2a$10$pp", "tk_t")
    for line in ('base-url: "https://pc.tail1234.ts.net"', '"jarvis:jarvis-abc:write-only"',
                 '"phone:jarvis-abc:read-only"', '"jarvis:tk_t:Jarvis reminders"',
                 'auth-default-access: "deny-all"', 'upstream-base-url: "https://ntfy.sh"',
                 'listen-http: "127.0.0.1:2586"'):
        assert line in text
    token = server.new_token()
    assert token.startswith("tk_") and len(token) == 32 and token[3:].isalnum() and token[3:].islower()

    env = tmp_path / ".env"
    env.write_text("# my comment\nJARVIS_TOKEN=keep-me\nNTFY_TOPIC=old\n")
    server.write_env(env, {"NTFY_TOPIC": "new", "NTFY_TOKEN": "tk_y"})
    assert env.read_text().splitlines()[:3] == ["# my comment", "JARVIS_TOKEN=keep-me", "NTFY_TOPIC=new"]
    assert server.read_env(env) == {"JARVIS_TOKEN": "keep-me", "NTFY_TOPIC": "new", "NTFY_TOKEN": "tk_y"}


def test_password_goes_to_ntfy_as_bytes_without_carriage_returns(monkeypatch):
    """Regression: text mode on Windows turned \\n into \\r\\n, ntfy hashed 'password\\r',
    and the phone got 401."""
    import subprocess

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"$2a$10$" + b"a" * 53 + b"\n", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._hash("s3cret").startswith("$2a$10$")
    assert seen["input"] == b"s3cret\ns3cret\n" and not seen.get("text")


def test_upgrade_keeps_existing_reminders(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("".join(db.MIGRATIONS[:3]) + "; PRAGMA user_version = 3;")
    old.execute("INSERT INTO events (id, title, start_at, end_at, important, created_at, updated_at) "
                "VALUES (1, 'Quiz', '2026-11-09T06:00:00+00:00', '2026-11-09T07:00:00+00:00', 1, 'x', 'x')")
    old.execute("INSERT INTO reminders (event_id, fire_at, status) VALUES (1, '2026-11-09T05:50:00+00:00', 'sent')")
    old.commit()
    old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT status, attempts, sent_at FROM reminders").fetchone()
    assert tuple(row) == ("sent", 0, None)
    conn.execute("UPDATE reminders SET status = 'missed'")  # the new statuses are allowed
