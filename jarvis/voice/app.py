"""Wire the real pieces together: `python -m jarvis voice`."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Callable

from jarvis.config import Settings
from jarvis.core import agent
from jarvis.core.dates import now_local
from jarvis.core.llm import LLMError, OllamaClient
from jarvis.voice.loop import LoopConfig, VoiceLoop

UNREACHABLE = "Sorry, I can't reach my language model right now."


def make_answer(conn: sqlite3.Connection, llm, clock: Callable[[], datetime] = now_local) -> Callable[[str], str]:
    memory = agent.SessionMemory()

    def answer(text: str) -> str:
        try:
            return agent.handle(text, conn, llm, now=clock(), source="voice", session="voice", memory=memory).text
        except LLMError:
            return UNREACHABLE

    return answer


def build(settings: Settings, conn: sqlite3.Connection, *, source=None, mouth=None, llm=None, stt=None,
          clock: Callable[[], datetime] = now_local, chime: bool = True,
          log: Callable[[str], None] = print, on_event=None) -> VoiceLoop:
    from jarvis.voice.audio import Microphone, Speaker
    from jarvis.voice.listen import SileroVad, WakeWord
    from jarvis.voice.stt import Transcriber
    from jarvis.voice.tts import make_voice

    def step(what: str, fn):
        started = time.perf_counter()
        result = fn()
        log(f"  {what} ({time.perf_counter() - started:.1f}s)")
        return result

    if llm is None:
        llm = OllamaClient(settings.ollama_url, settings.ollama_model, keep_alive=-1)  # stay loaded
        try:
            step(f"language model {settings.ollama_model} loaded", llm.warm)  # before Whisper: VRAM order
        except LLMError as e:
            log(f"  warning: {e}")
    if stt is None:
        stt = step(f"speech recognition {settings.whisper_model} on {settings.whisper_device}", lambda: Transcriber(
            settings.whisper_model, settings.whisper_device, settings.whisper_compute, settings.models_dir))
        step("speech recognition warmed up", stt.warm)
    wake = step("wake word 'hey jarvis'", lambda: WakeWord(settings.models_dir))
    vad = SileroVad(settings.models_dir)
    if mouth is None:
        voice = step(f"voice {settings.tts_engine} {settings.tts_voice}",
                     lambda: make_voice(settings.tts_engine, settings.models_dir, settings.tts_voice,
                                        settings.tts_speed))
        mouth = Speaker(voice)
    if source is None:
        source = Microphone(settings.mic_device)
    config = LoopConfig(wake_threshold=settings.wake_threshold, chime=chime)
    return VoiceLoop(source, wake, vad, stt, mouth, make_answer(conn, llm, clock), config, log, on_event)


def run(settings: Settings, wav: str | None = None) -> int:
    from jarvis.calendar import db
    from jarvis.voice.audio import WavFile

    conn = db.connect(settings.db_path)
    print("Starting Jarvis voice:")
    if settings.holiday_regions:
        from datetime import timedelta

        from jarvis.calendar import holidays

        holidays.refresh_in_background(lambda: db.connect(settings.db_path), list(settings.holiday_regions),
                                       now_local, timedelta(days=settings.holiday_refresh_days))
    loop = build(settings, conn, source=WavFile(wav) if wav else None)

    def say_reminder(text: str) -> None:
        loop.mouth.say(text)
        loop.source.flush()  # don't hear the reminder as a request

    from jarvis.calendar.alarms import ring_with
    from jarvis.notify.app import start_alarms, start_reminders

    def ring(said: str, stop) -> None:
        ring_with(loop.mouth)(said, stop)
        loop.source.flush()

    start_reminders(settings, now_local, say=say_reminder)
    start_alarms(settings, now_local, ring=ring)
    print("Listening for 'Hey Jarvis'. Ctrl+C to stop.")
    try:
        loop.run()
    except KeyboardInterrupt:
        print()
    finally:
        conn.close()
    return 0


def mic_test(settings: Settings, seconds: int = 6) -> int:
    """Record the user saying the wake phrase and report what each stage made of it."""
    import numpy as np
    import sounddevice as sd

    from jarvis.voice.audio import FRAME, RATE
    from jarvis.voice.listen import WakeWord
    from jarvis.voice.stt import Transcriber

    device = int(settings.mic_device) if settings.mic_device and settings.mic_device.isdigit() else settings.mic_device
    name = sd.query_devices(device if device is not None else sd.default.device[0])["name"]
    print(f"Microphone: {name}")
    wake = WakeWord(settings.models_dir)
    stt = Transcriber(settings.whisper_model, settings.whisper_device, settings.whisper_compute, settings.models_dir)
    input("Press Enter when you're ready, then say \"Hey Jarvis, what's on tomorrow?\" ")
    print(f">>> Recording {seconds} s, speak now", flush=True)
    rec = sd.rec(seconds * RATE, samplerate=RATE, channels=1, dtype="int16", device=device)
    sd.wait()
    pcm = rec[:, 0]
    level = np.abs(pcm.astype(np.float32)) / 32768
    per_second = [float(np.sqrt(np.mean(level[i:i + RATE] ** 2))) for i in range(0, len(level), RATE)]
    print("Level each second:", "  ".join(f"{v:.3f}" for v in per_second), f"(peak {level.max():.3f})")
    scores = [wake.score(pcm[i:i + FRAME]) for i in range(0, len(pcm) - FRAME + 1, FRAME)]
    best = max(scores)
    print(f"Best 'hey jarvis' score: {best:.2f} (needs {settings.wake_threshold})")
    heard = stt.transcribe(pcm.astype(np.float32) / 32768)
    print(f"Whisper heard: {heard!r}")
    if level.max() < 0.02:
        print("=> The mic is barely picking anything up. Check Windows mic privacy settings, the input "
              "device (python -m jarvis devices, then MIC_DEVICE in .env) and the mic volume in Sound settings.")
    elif best < 0.1 and "jarvis" not in heard.lower():
        print("=> The mic works, but 'Hey Jarvis' wasn't in the recording. Run the test again and say it "
              "during the 6 seconds after 'Speak now'.")
    elif best < settings.wake_threshold:
        print("=> Sound is coming through, but 'hey jarvis' wasn't recognised clearly. Try again a bit louder "
              f"and slower, or set WAKE_THRESHOLD={max(0.2, round(best * 0.8, 2))} in .env.")
    else:
        print("=> All good: Jarvis should hear you.")
    return 0


def list_devices() -> int:
    import sounddevice as sd

    default_in, default_out = sd.default.device
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != 0:
            continue  # the same devices repeat under other Windows audio APIs
        kinds = [k for k, n in (("mic", d["max_input_channels"]), ("speaker", d["max_output_channels"])) if n]
        mark = " (default)" if i in (default_in, default_out) else ""
        print(f"{i:3d}  {d['name']}  [{', '.join(kinds)}]{mark}")
    return 0
