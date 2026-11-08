"""Everything Jarvis runs, in one process (`python -m jarvis up`, started at Windows login):

- the API on 127.0.0.1:8765 (phone Shortcuts, web app, reminders, inbox, holidays), in a thread;
- the "hey jarvis" voice loop, in a thread;
- ntfy, as a child process (phone notifications);
- one copy each of the language model client, Whisper and the Kokoro voice, shared by all of them.

The desktop interface (hud.py) runs on the main thread and talks to this through the event bus.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from jarvis.config import ROOT, Settings
from jarvis.desktop.bus import BUS, Bus

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flashing up when run without one
LOG_DIR = ROOT / "data" / "logs"


class SingleInstance:
    """A lock file held while Jarvis runs, so a second `jarvis up` (e.g. a double login) exits."""

    def __init__(self, path: Path = ROOT / "data" / "jarvis.lock"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a+")  # noqa: SIM115 (kept open for the life of the process)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            self.ok = True
        except OSError:
            self.ok = False


class Runtime:
    def __init__(self, settings: Settings | None = None, bus: Bus = BUS, log: Callable[[str], None] | None = None,
                 voice_enabled: bool = True):
        self.settings = settings or Settings.from_env()
        self.bus = bus
        self.voice_enabled = voice_enabled
        self.status = {"api": "starting", "voice": "off" if not voice_enabled else "starting",
                       "ntfy": "off", "model": "starting"}
        self._log = log or (lambda line: print(line, flush=True))
        self.llm = None
        self.stt = None
        self.speaker = None
        self.loop = None
        self._server = None
        self._ntfy: subprocess.Popen | None = None
        self._ask_lock = threading.Lock()
        self._memory = None

    def log(self, line: str) -> None:
        self._log(line)
        self.bus.publish("log", text=line)

    def _set(self, part: str, state: str) -> None:
        self.status[part] = state
        self.bus.publish("status", status=dict(self.status))

    # ---------- start ----------

    def start(self) -> None:
        threading.Thread(target=self._start_all, name="jarvis-start", daemon=True).start()

    def _start_all(self) -> None:
        from jarvis.core.llm import LLMError, OllamaClient

        s = self.settings
        self._start_ntfy()
        self.llm = OllamaClient(s.ollama_url, s.ollama_model, keep_alive=-1)
        try:
            self.llm.warm()
            self._set("model", "ready")
        except LLMError as e:
            self._set("model", "offline")
            self.log(f"language model: {e}")
        if self.voice_enabled:
            try:
                self._load_voice_models()
            except Exception as e:  # no mic, no GPU... the rest still works
                self._set("voice", "failed")
                self.log(f"voice: couldn't start: {e!r}")
        self._start_api()
        if self.voice_enabled and self.stt is not None:
            threading.Thread(target=self._run_voice, name="voice", daemon=True).start()

    def _start_ntfy(self) -> None:
        from jarvis.notify import server as ntfy

        if not (self.settings.ntfy_url and ntfy.EXE.exists() and ntfy.CONFIG.exists()):
            return
        if _healthy(f"{ntfy.LOCAL_URL}/v1/health"):
            self._set("ntfy", "ready")  # already running (e.g. its own terminal)
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = open(LOG_DIR / "ntfy.log", "a")  # noqa: SIM115 (owned by the child process)
        self._ntfy = subprocess.Popen([str(ntfy.EXE), "serve", "--config", str(ntfy.CONFIG)],
                                      stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        self._set("ntfy", "ready" if _wait_healthy(f"{ntfy.LOCAL_URL}/v1/health") else "failed")

    def _load_voice_models(self) -> None:
        from jarvis.voice.audio import Speaker
        from jarvis.voice.stt import Transcriber
        from jarvis.voice.tts import make_voice

        s = self.settings
        self.stt = Transcriber(s.whisper_model, s.whisper_device, s.whisper_compute, s.models_dir)
        self.stt.warm()
        self.voice = make_voice(s.tts_engine, s.models_dir, s.tts_voice, s.tts_speed)
        self.speaker = Speaker(self.voice)

    def _start_api(self) -> None:
        import uvicorn

        from jarvis.core.api import create_app

        s = self.settings
        if not s.token:
            self._set("api", "off")
            self.log("API: set JARVIS_TOKEN in .env to turn it on")
            return
        app = create_app(s, llm=self.llm, stt=self.stt, voice=getattr(self, "voice", None),
                         reminder_say=self.say if self.speaker else None,
                         alarm_ring=self._ring if self.speaker else None,
                         on_exchange=lambda src, heard, reply: self._exchange(src, heard, reply))
        config = uvicorn.Config(app, host=s.host, port=s.port, log_level="warning")
        self._server = uvicorn.Server(config)
        threading.Thread(target=self._server.run, name="api", daemon=True).start()
        self._set("api", "ready" if _wait_healthy(f"http://{s.host}:{s.port}/health") else "failed")

    def _run_voice(self) -> None:
        from jarvis.calendar import db
        from jarvis.voice.app import build

        conn = db.connect(self.settings.db_path)
        try:
            self.loop = build(self.settings, conn, llm=self.llm, stt=self.stt, mouth=self.speaker,
                              log=self.log, on_event=lambda kind, data: self.bus.publish(kind, **data))
            self._set("voice", "listening")
            self.loop.run()
        except Exception as e:
            self._set("voice", "failed")
            self.log(f"voice: stopped: {e!r}")
        finally:
            conn.close()

    # ---------- used by the interface ----------

    def _exchange(self, source: str, heard: str, reply: str) -> None:
        self.bus.publish("heard", text=heard, source=source)
        self.bus.publish("reply", text=reply, source=source)

    def say(self, text: str) -> None:
        if self.speaker is None:
            return
        self.bus.publish("state", state="speaking")
        try:
            self.speaker.say(text)
        finally:
            self.bus.publish("state", state="idle")

    def _ring(self, said: str, stop) -> None:
        from jarvis.calendar.alarms import ring_with

        self.bus.publish("reply", text=said, source="alarm")
        self.bus.publish("state", state="speaking")
        try:
            ring_with(self.speaker)(said, stop)
        finally:
            self.bus.publish("state", state="idle")
            if self.loop is not None:
                self.loop.source.flush()  # don't hear the alarm as a request

    def ask(self, text: str, speak: bool = True) -> None:
        """Typed into the interface: answer in a worker thread, show and say the reply."""
        threading.Thread(target=self._ask, args=(text, speak), daemon=True).start()

    def _ask(self, text: str, speak: bool) -> None:
        from jarvis.calendar import db
        from jarvis.core import agent
        from jarvis.core.llm import LLMError

        with self._ask_lock:
            self._memory = self._memory or agent.SessionMemory()
            self.bus.publish("heard", text=text, source="hud")
            self.bus.publish("state", state="thinking")
            conn = db.connect(self.settings.db_path)
            try:
                reply = agent.handle(text, conn, self.llm, source="hud", session="hud", memory=self._memory).text
            except (LLMError, AttributeError):
                reply = "Sorry, I can't reach my language model right now."
            finally:
                conn.close()
            self.bus.publish("reply", text=reply, source="hud")
            if speak:
                self.say(reply)
            else:
                self.bus.publish("state", state="idle")

    def listen(self) -> None:
        if self.loop is not None:
            self.loop.trigger()
        else:
            self.bus.publish("reply", text="Voice isn't running, so I can't listen. You can type instead.")

    # ---------- stop ----------

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._ntfy is not None and self._ntfy.poll() is None:
            self._ntfy.terminate()


def _healthy(url: str) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_healthy(url: str, seconds: float = 20) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _healthy(url):
            return True
        time.sleep(0.4)
    return False


def log_to_file_if_windowless() -> None:
    """Started by pythonw at login there's no console: send prints to data/logs/jarvis.log."""
    if sys.stdout is None or sys.stderr is None or not os.isatty(1):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None or sys.stderr is None:
            f = open(LOG_DIR / "jarvis.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115
            sys.stdout = sys.stderr = f
