"""Milestone 3 done-criterion, end to end with every real model: "Hey Jarvis,
what's on tomorrow?" gets a spoken answer.

A different Kokoro voice plays the user, so the audio is real speech. It runs
through the real wake word, VAD, Whisper (GPU), qwen3 and Kokoro. Only the
speakers are left out: the answer is synthesized but not played.
Needs the voice models in data/models (downloaded on first `jarvis voice`).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import NOW, ingest_fixture, nov, seed
from jarvis.config import Settings
from jarvis.voice.app import make_answer
from jarvis.voice.audio import WavFile, to_pcm16k
from jarvis.voice.loop import LoopConfig, VoiceLoop

pytestmark = pytest.mark.llm
SETTINGS = Settings.from_env()
MODELS = Path(SETTINGS.models_dir)


@pytest.fixture(scope="module")
def parts():
    needed = [MODELS / "kokoro" / "kokoro-v1.0.onnx", MODELS / "openwakeword" / "hey_jarvis_v0.1.onnx",
              MODELS / "whisper"]
    if not all(p.exists() for p in needed):
        pytest.skip("voice models not downloaded yet (run: python -m jarvis voice)")
    from jarvis.voice.listen import SileroVad, WakeWord
    from jarvis.voice.stt import Transcriber
    from jarvis.voice.tts import KokoroVoice

    whisper = Transcriber(SETTINGS.whisper_model, SETTINGS.whisper_device, SETTINGS.whisper_compute, MODELS)
    whisper.warm()
    return {
        "stt": whisper,
        "wake": WakeWord(MODELS),
        "vad": SileroVad(MODELS),
        "jarvis_voice": KokoroVoice(MODELS, "bm_george"),
        "user_voice": KokoroVoice(MODELS, "am_michael"),
    }


class RecordingMouth:
    """Makes Jarvis's spoken audio for real, but keeps it instead of playing it."""

    def __init__(self, voice):
        self.voice, self.said, self.seconds = voice, [], 0.0

    def say(self, text):
        from jarvis.voice.tts import sentences

        self.said.append(text)
        for sentence in sentences(text):
            audio, rate = self.voice.synthesize(sentence)
            self.seconds += len(audio) / rate

    def chime(self):
        pass


def user_says(parts, text: str) -> WavFile:
    audio, rate = parts["user_voice"].synthesize(text)
    speech = to_pcm16k(audio, rate)
    quiet = np.zeros(16000, dtype=np.int16)
    return WavFile(audio=np.concatenate([quiet, speech, quiet, quiet]))


def converse(parts, conn, ollama, text):
    mouth, log = RecordingMouth(parts["jarvis_voice"]), []
    parts["wake"].reset()
    VoiceLoop(user_says(parts, text), parts["wake"], parts["vad"], parts["stt"], mouth,
              make_answer(conn, ollama, clock=lambda: NOW), LoopConfig(chime=False), log.append).run()
    return mouth, log


def test_hey_jarvis_whats_on_tomorrow(conn, ollama, parts):
    seed(conn, "Dentist appointment", nov(6), (16, 30))
    seed(conn, "Football practice", nov(6), (17, 0))
    seed(conn, "Gym", nov(8), (18, 0))
    mouth, log = converse(parts, conn, ollama, "Hey Jarvis, what's on tomorrow?")
    assert "(wake word)" in log
    heard = next(line for line in log if line.startswith("you:"))
    assert "tomorrow" in heard.lower()
    assert len(mouth.said) == 1, log
    reply = mouth.said[0].lower()
    assert "dentist" in reply and "football" in reply and "gym" not in reply, log
    assert mouth.seconds > 2  # it was actually spoken


def test_menu_by_voice(conn, ollama, parts, tmp_path):
    ingest_fixture(conn, "mess_menu_week.pdf", tmp_path)
    mouth, log = converse(parts, conn, ollama, "Hey Jarvis, what's for breakfast this Wednesday?")
    assert mouth.said == ["Breakfast on Wednesday 11 November: Poha, boiled egg, banana, tea/coffee."], log


def test_adding_an_event_by_voice(conn, ollama, parts):
    mouth, log = converse(parts, conn, ollama, "Hey Jarvis, add DBMS quiz on Monday at 10 am, it's important.")
    assert mouth.said == ["Added DBMS quiz, Monday 9 November at 10 am, marked important."], log
