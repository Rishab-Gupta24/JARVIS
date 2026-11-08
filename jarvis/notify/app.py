"""Turn settings into a running reminder loop, and the CLI commands around it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from jarvis.calendar import db, repo
from jarvis.config import Settings
from jarvis.notify.ntfy import NtfyError, NtfySender
from jarvis.notify.reminders import Dispatcher, PushSender, SpeakSender


def push_from(settings: Settings) -> PushSender | None:
    if not (settings.ntfy_url and settings.ntfy_topic and settings.ntfy_token):
        return None
    return PushSender(NtfySender(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token))


def start_reminders(settings: Settings, clock: Callable[[], datetime], say: Callable[[str], None] | None = None,
                    log: Callable[[str], None] = print) -> Dispatcher | None:
    push = push_from(settings)
    extras = [SpeakSender(say)] if say else []
    if push is None and not extras:
        log("  reminders: off (phone not set up: run python -m jarvis ntfy-setup)")
        return None
    dispatcher = Dispatcher(lambda: db.connect(settings.db_path), clock, push, extras, log)
    dispatcher.start()
    where = " and ".join(w for w, on in (("phone", push is not None), ("spoken here", bool(say))) if on)
    log(f"  reminders: on ({where})")
    return dispatcher


def start_alarms(settings: Settings, clock: Callable[[], datetime], ring=None,
                 log: Callable[[str], None] = print):
    """Alarms and timers: ring on the PC (if there's a speaker) and push to the phone (urgent)."""
    from jarvis.calendar.alarms import AlarmClock

    push = None
    if settings.ntfy_url and settings.ntfy_topic and settings.ntfy_token:
        sender = NtfySender(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token)

        def push(title: str, words: str) -> None:
            sender.publish(title, words, "urgent", tags=["alarm_clock"])
    clock_ = AlarmClock(lambda: db.connect(settings.db_path), clock, ring, push, log)
    clock_.start()
    where = " and ".join(w for w, on in (("phone", push is not None), ("rung here", ring is not None)) if on)
    log(f"  alarms: on ({where or 'nowhere to ring: no speaker or phone'})")
    return clock_


def send_test(settings: Settings) -> int:
    if push_from(settings) is None:
        print("The phone isn't set up yet: run python -m jarvis ntfy-setup, then start python -m jarvis ntfy-server.")
        return 1
    try:
        NtfySender(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token).publish(
            "Jarvis test", "If you can read this on your phone, reminders work.", "high", tags=["white_check_mark"])
    except NtfyError as e:
        print(f"Couldn't send: {e}")
        return 1
    print("Sent. It should appear in the ntfy app on your iPhone within a few seconds.")
    return 0


def make_test_event(settings: Settings, now: datetime, minutes: int = 15) -> int:
    """An important event `minutes` from now; its 10-minute reminder fires `minutes - 10` from now."""
    start = (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    conn = db.connect(settings.db_path)
    try:
        event = repo.create_event(conn, repo.NewEvent(
            title="Jarvis test reminder", start=start, end=start + timedelta(minutes=15),
            important=True, source="test", notes="Made by python -m jarvis remind-test; delete any time."), now)
        fires = repo.pending_reminders(conn, event.id)
    finally:
        conn.close()
    print(f"Added '{event.title}' at {start:%H:%M} (important).")
    for t in fires:
        print(f"  reminder due at {t:%H:%M}")
    print("Keep `python -m jarvis serve` (or voice) and `python -m jarvis ntfy-server` running until then.")
    return 0
