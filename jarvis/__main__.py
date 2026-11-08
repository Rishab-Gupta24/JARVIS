"""python -m jarvis [chat|up|serve|check|...]"""

from __future__ import annotations

import argparse
import sys

from jarvis.calendar import db
from jarvis.config import Settings
from jarvis.core import agent
from jarvis.core.llm import VISION_NUM_CTX, LLMError, OllamaClient


def chat(settings: Settings) -> None:
    llm = OllamaClient(settings.ollama_url, settings.ollama_model)
    if problem := llm.status():
        print(f"Warning: {problem}")
    conn = db.connect(settings.db_path)
    memory = agent.SessionMemory()
    print(f"Jarvis ({settings.ollama_model}). Type a request, or 'quit'.")
    while True:
        try:
            text = input("you> ").strip().lstrip("﻿")  # PowerShell pipes add a BOM
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in ("quit", "exit", "bye"):
            break
        if not text:
            continue
        try:
            reply = agent.handle(text, conn, llm, source="terminal", session="terminal", memory=memory)
        except LLMError as e:
            print(f"jarvis> (model unavailable) {e}")
            continue
        print(f"jarvis> {reply.text}")
    conn.close()


def ingest_file(settings: Settings, path: str, week: str | None, repeats: bool) -> int:
    from pathlib import Path

    from jarvis.core.dates import DateParseError, now_local
    from jarvis.documents.ingest import ingest
    from jarvis.documents.readers import UnreadableFile

    llm = OllamaClient(settings.ollama_url, settings.ollama_model)
    vision = OllamaClient(settings.ollama_url, settings.vision_model, num_ctx=VISION_NUM_CTX)
    conn = db.connect(settings.db_path)
    try:
        result = ingest(conn, Path(path).read_bytes(), Path(path).name, now_local(), settings.files_dir,
                        source="terminal", vision=vision, llm=llm, week=week, repeats_weekly=repeats or None)
    except (OSError, UnreadableFile, DateParseError, LLMError) as e:
        print(f"Couldn't ingest {path}: {e}")
        return 1
    finally:
        conn.close()
    print(result.reply)
    return 0


def up(settings: Settings, hidden: bool = False, voice: bool = True) -> int:
    """Everything in one process, with the desktop interface. What runs at Windows login."""
    from jarvis.desktop.runtime import Runtime, SingleInstance, log_to_file_if_windowless

    log_to_file_if_windowless()
    lock = SingleInstance()
    if not lock.ok:
        print("Jarvis is already running (look for the orb by the mouse or the tray icon; Ctrl+Alt+J shows it).")
        return 0
    from jarvis.desktop.hud import JarvisDesktop

    runtime = Runtime(settings, voice_enabled=voice)
    desktop = JarvisDesktop(runtime, start_hidden=hidden)
    import signal

    signal.signal(signal.SIGINT, lambda *_: desktop.quit())  # Ctrl+C in a terminal quits cleanly
    runtime.start()
    code = desktop.run()
    runtime.stop()
    return code


def autostart(action: str) -> int:
    from jarvis.desktop import autostart as auto

    if action == "on":
        print(f"Jarvis will start when you log in to Windows ({auto.enable()}).")
    elif action == "off":
        print("Jarvis won't start at login any more." if auto.disable() else "It wasn't set to start at login.")
    else:
        print("On: Jarvis starts at login." if auto.is_enabled() else "Off: Jarvis doesn't start at login.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Local voice assistant")
    parser.add_argument("command", nargs="?", default="chat",
                        choices=["chat", "serve", "check", "ingest", "voice", "devices", "mic-test", "holidays",
                                 "ntfy-setup", "ntfy-server", "notify-test", "remind-test", "phone-setup", "up",
                                 "autostart"])
    parser.add_argument("file", nargs="?", help="ingest: the file to read; voice: a 16 kHz WAV to use instead of the "
                                                "mic; autostart: on, off or status")
    parser.add_argument("--hidden", action="store_true", help="for up: start with just the orb by the mouse")
    parser.add_argument("--no-voice", action="store_true", help="for up: no microphone or speech")
    parser.add_argument("--week", help="for ingest: which week a menu covers, e.g. 'next week'")
    parser.add_argument("--repeats", action="store_true", help="for ingest: the menu repeats every week")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "up":
        return up(settings, hidden=args.hidden, voice=not args.no_voice)
    if args.command == "autostart":
        return autostart(args.file or "status")
    if args.command == "voice":
        from jarvis.voice.app import run

        return run(settings, args.file)
    if args.command == "ntfy-setup":
        from jarvis.notify import server

        values = server.setup()
        print(f"ntfy configured: topic {values['NTFY_TOPIC']}, phone address {values['NTFY_PUBLIC_URL']}")
        print("Token and phone password saved in .env (NTFY_TOKEN, NTFY_PHONE_PASSWORD).")
        if values["NTFY_PUBLIC_URL"].startswith("http://127."):
            print("Tailscale isn't signed in yet, so the phone can't reach it. Sign in, then run this again.")
        return 0
    if args.command == "phone-setup":
        from jarvis.core import phone

        values = phone.setup()
        print(f"Jarvis is reachable from your Tailscale devices at {values['JARVIS_PUBLIC_URL']}")
        print("The token for the iPhone Shortcuts is JARVIS_PHONE_TOKEN in .env.")
        print("Restart `python -m jarvis serve` so it accepts the new token.")
        return 0
    if args.command == "ntfy-server":
        from jarvis.notify import server

        return server.serve()
    if args.command == "notify-test":
        from jarvis.notify.app import send_test

        return send_test(settings)
    if args.command == "remind-test":
        from jarvis.core.dates import now_local
        from jarvis.notify.app import make_test_event

        minutes = int(args.file) if args.file and args.file.isdigit() else 15
        return make_test_event(settings, now_local(), minutes)
    if args.command == "holidays":
        from jarvis.calendar import holidays
        from jarvis.core.dates import now_local

        if not settings.holiday_regions:
            print("HOLIDAY_REGIONS is empty in .env, so holidays are off.")
            return 1
        conn = db.connect(settings.db_path)
        try:
            result = holidays.sync_from_internet(conn, list(settings.holiday_regions), now_local())
        finally:
            conn.close()
        print(result.summary())
        return 1 if result.errors and not (result.added or result.kept or result.updated) else 0
    if args.command == "mic-test":
        from jarvis.voice.app import mic_test

        return mic_test(settings)
    if args.command == "devices":
        from jarvis.voice.app import list_devices

        return list_devices()
    if args.command == "ingest":
        if not args.file:
            parser.error("ingest needs a file")
        return ingest_file(settings, args.file, args.week, args.repeats)
    if args.command == "check":
        problem = OllamaClient(settings.ollama_url, settings.ollama_model).status()
        print(problem or f"Ollama is up and {settings.ollama_model} is ready.")
        return 1 if problem else 0
    if args.command == "serve":
        import uvicorn

        from jarvis.core.api import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0
    chat(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
