"""Microphone in, speakers out. Everything inside Jarvis is 16 kHz mono int16 in
80 ms frames (1280 samples), which is what openWakeWord expects."""

from __future__ import annotations

import queue
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np

RATE = 16000
FRAME = 1280  # 80 ms


class FrameSource(Protocol):
    def frames(self) -> Iterator[np.ndarray]: ...
    def flush(self) -> None: ...


class Microphone:
    def __init__(self, device: str | int | None = None):
        import sounddevice as sd

        if isinstance(device, str) and device.isdigit():
            device = int(device)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)  # 40 s of backlog at most
        self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                      device=device, callback=self._callback)

    def _callback(self, data, frames, time_info, status) -> None:
        try:
            self._queue.put_nowait(data[:, 0].copy())
        except queue.Full:
            pass  # Jarvis is busy thinking; old audio is flushed afterwards anyway

    def frames(self) -> Iterator[np.ndarray]:
        with self._stream:
            while True:
                yield self._queue.get()

    def flush(self) -> None:
        """Drop audio heard while Jarvis was busy or talking (including its own voice)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class WavFile:
    """Feeds a 16 kHz mono WAV through the loop instead of the microphone."""

    def __init__(self, path: str | Path | None = None, audio: np.ndarray | None = None):
        if audio is None:
            with wave.open(str(path), "rb") as w:
                if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise ValueError("Use a 16 kHz mono 16-bit WAV.")
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        self._audio = audio

    def frames(self) -> Iterator[np.ndarray]:
        for i in range(0, len(self._audio) - FRAME + 1, FRAME):
            yield self._audio[i:i + FRAME]

    def flush(self) -> None:
        pass


def to_pcm16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """float audio at any rate -> 16 kHz int16."""
    if rate != RATE:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(RATE, rate)
        audio = resample_poly(audio, RATE // g, rate // g)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


class Speaker:
    """Plays a reply sentence by sentence, making the next sentence while one plays."""

    def __init__(self, voice, device: str | int | None = None):
        self._voice = voice
        self._device = device
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        import sounddevice as sd

        from jarvis.voice.tts import sentences

        parts = sentences(text)
        if not parts:
            return
        with self._lock:  # a spoken reminder and an answer take turns
            pending = self._pool.submit(self._voice.synthesize, parts[0])
            for nxt in parts[1:] + [None]:
                audio, rate = pending.result()
                if nxt is not None:
                    pending = self._pool.submit(self._voice.synthesize, nxt)
                sd.play(audio, rate, device=self._device)
                sd.wait()

    def chime(self) -> None:
        """A short soft two-note 'I'm listening' sound."""
        import sounddevice as sd

        with self._lock:
            sd.play(listening_chime(), 24000, device=self._device)
            sd.wait()


def listening_chime(rate: int = 24000) -> np.ndarray:
    notes = []
    for freq in (880, 1320):
        t = np.arange(int(rate * 0.07)) / rate
        notes.append(0.15 * np.sin(2 * np.pi * freq * t) * np.hanning(len(t)))
    return np.concatenate(notes).astype(np.float32)
