"""Text to speech: Kokoro (natural) or Piper (instant), one sentence at a time so
the first sentence starts playing while the next is still being made."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np


class Voice(Protocol):
    def synthesize(self, sentence: str) -> tuple[np.ndarray, int]:
        """float32 audio in [-1, 1] and its sample rate."""
        ...


def for_speech(text: str) -> str:
    """Written form -> what a TTS engine reads naturally."""
    s = re.sub(r"[*_`#]", "", text)
    s = re.sub(r"\b(\d{1,2}):00\b", r"\1", s)                 # 10:00 am -> 10 am
    s = re.sub(r"\b(\d{1,2}):(\d{2})\b", r"\1 \2", s)          # 4:30 -> 4 30
    s = re.sub(r"(?<=\d)\s?([ap])\.?m\b\.?", lambda m: f" {m[1].lower()}.m.", s, flags=re.I)  # 10 am -> 10 a.m.
    s = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " or ", s)       # tea/coffee -> tea or coffee
    s = s.replace("&", " and ")
    return " ".join(s.split())


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9])", for_speech(text))
    return [p.strip() for p in parts if p.strip()]


class KokoroVoice:
    def __init__(self, models_dir: str | Path, voice: str = "bm_george", speed: float = 1.0):
        from kokoro_onnx import Kokoro

        folder = Path(models_dir) / "kokoro"
        self._kokoro = Kokoro(str(folder / "kokoro-v1.0.onnx"), str(folder / "voices-v1.0.bin"))
        self._voice = voice
        self._speed = speed
        # British voices (b*) read with British pronunciation, the rest American.
        self._lang = "en-gb" if voice[:1] == "b" else "en-us"

    def synthesize(self, sentence: str) -> tuple[np.ndarray, int]:
        audio, sr = self._kokoro.create(sentence, voice=self._voice, speed=self._speed, lang=self._lang)
        return audio.astype(np.float32), sr


class PiperVoice:
    def __init__(self, models_dir: str | Path, voice: str = "en_GB-alan-medium"):
        from piper import PiperVoice as _Piper

        self._piper = _Piper.load(str(Path(models_dir) / "piper" / f"{voice}.onnx"))

    def synthesize(self, sentence: str) -> tuple[np.ndarray, int]:
        chunks = list(self._piper.synthesize(sentence))
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate


def make_voice(engine: str, models_dir: str | Path, voice: str, speed: float = 1.0) -> Voice:
    if engine == "piper":
        return PiperVoice(models_dir, voice if voice.startswith(("en_", "en-")) else "en_GB-alan-medium")
    return KokoroVoice(models_dir, voice, speed)


def speech_chunks(voice: Voice, text: str) -> Iterator[tuple[np.ndarray, int]]:
    for sentence in sentences(text):
        yield voice.synthesize(sentence)


def to_wav(voice: Voice, text: str, pause: float = 0.18) -> bytes:
    """The whole reply as one 16-bit mono WAV, sentences joined by a short pause."""
    import io
    import wave

    parts, rate = [], 24000
    for audio, rate in speech_chunks(voice, text):
        parts += [audio, np.zeros(int(rate * pause), dtype=np.float32)]
    pcm = (np.clip(np.concatenate(parts) if parts else np.zeros(1, np.float32), -1, 1) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()
