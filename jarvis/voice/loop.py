"""The voice loop: wait for "hey jarvis", record until the user stops talking,
transcribe, ask the text brain, speak the answer.

    idle --wake word--> record --silence--> transcribe --> answer --> speak
                        (chime only if the user pauses first)
      ^                     |  no speech in 4 s                               |
      +---------------------+<------------------------------------------------+
                                  reply ends in "?" -> record again, no wake word needed
                                  only "hey jarvis" heard -> say "Yes?" and record again
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

import numpy as np

from jarvis.voice.stt import is_nothing, strip_wake


class Wake(Protocol):
    def score(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class Vad(Protocol):
    def prob(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class Stt(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


class Mouth(Protocol):
    def say(self, text: str) -> None: ...
    def chime(self) -> None: ...


NEAR_MISS = 0.2


@dataclass
class LoopConfig:
    wake_threshold: float = 0.5
    vad_threshold: float = 0.5
    end_silence_frames: int = 10   # 0.8 s of quiet ends the request
    wait_frames: int = 50          # 4 s to start talking before giving up
    max_frames: int = 190          # 15 s per request at most
    preroll_frames: int = 4        # 0.3 s before the wake word fired, in case speech overlaps it
    chime: bool = True
    chime_after_frames: int = 6    # chime only after 0.5 s of quiet following the wake word


class VoiceLoop:
    def __init__(self, source, wake: Wake, vad: Vad, stt: Stt, mouth: Mouth,
                 answer: Callable[[str], str], config: LoopConfig | None = None,
                 log: Callable[[str], None] = print):
        self.source, self.wake, self.vad, self.stt, self.mouth = source, wake, vad, stt, mouth
        self.answer = answer
        self.cfg = config or LoopConfig()
        self.log = log

    def run(self) -> None:
        """Runs until the audio source ends (a file), or forever (the microphone)."""
        frames = iter(self.source.frames())
        preroll: deque[np.ndarray] = deque(maxlen=self.cfg.preroll_frames)
        near_miss = 0.0
        for frame in frames:
            score = self.wake.score(frame)
            if score >= self.cfg.wake_threshold:
                self.wake.reset()
                self.log("(wake word)")
                self._conversation(frames, list(preroll))
                preroll.clear()
                self.wake.reset()
                near_miss = 0.0
                continue
            preroll.append(frame)
            # Report almost-detections once they fade, so a quiet mic or a too-high
            # threshold is visible instead of Jarvis just seeming deaf.
            if score >= NEAR_MISS:
                near_miss = max(near_miss, score)
            elif near_miss and score < NEAR_MISS / 4:
                self.log(f"(close to 'hey jarvis': {near_miss:.2f}, needs {self.cfg.wake_threshold})")
                near_miss = 0.0

    def _conversation(self, frames: Iterator[np.ndarray], preroll: list[np.ndarray]) -> None:
        retried = False
        after_wake = True
        while True:
            # No chime up front: the laptop mic's echo cancellation mutes input while the
            # speakers play, which cut "what's on" off "what's on tomorrow" in live tests.
            # It chimes only if the user pauses after the wake word.
            audio = self._record(frames, preroll if after_wake else [], chime_if_quiet=after_wake)
            if audio is None:
                self.log("(no speech, back to waiting)")
                return
            started = time.perf_counter()
            raw = self.stt.transcribe(audio)
            text = strip_wake(raw) if after_wake else raw
            heard_in = time.perf_counter() - started
            if is_nothing(text):
                if after_wake and not retried:  # just "hey jarvis", then a pause
                    self.log(f"(heard only {raw.strip()!r}; asking 'Yes?')")
                    retried, after_wake = True, False
                    self._speak("Yes?")
                    continue
                self.log(f"(heard nothing usable: {raw.strip()!r}; back to waiting)")
                return
            if after_wake and not retried and len(text.split()) == 1:
                # One stray word ("tomorrow.") is a clipped request, not a request.
                retried, after_wake = True, False
                self.log(f"you: {text}   (only one word; asking again)")
                self._speak(f"Sorry, I only caught '{text.strip(' .,!?')}'. Could you say that again?")
                continue
            self.log(f"you: {text}   ({heard_in:.1f}s to transcribe)")
            started = time.perf_counter()
            reply = self.answer(text)
            self.log(f"jarvis: {reply}   ({time.perf_counter() - started:.1f}s to answer)")
            self._speak(reply)
            if not reply.rstrip().endswith("?"):
                return
            after_wake = False  # Jarvis asked something: listen for the answer, no wake word

    def _speak(self, text: str) -> None:
        self.mouth.say(text)
        self.source.flush()  # don't hear Jarvis's own voice as the next request
        self.wake.reset()

    def _record(self, frames: Iterator[np.ndarray], preroll: list[np.ndarray],
                chime_if_quiet: bool = False) -> np.ndarray | None:
        """Collect frames until the user has spoken and then gone quiet."""
        cfg = self.cfg
        self.vad.reset()
        buf = list(preroll)
        spoke, quiet, n = False, 0, 0
        for frame in frames:
            buf.append(frame)
            n += 1
            if self.vad.prob(frame) >= cfg.vad_threshold:
                spoke, quiet = True, 0
            elif spoke:
                quiet += 1
            if chime_if_quiet and cfg.chime and not spoke and n == cfg.chime_after_frames:
                self.mouth.chime()  # they paused after "hey jarvis": signal we're listening
            if spoke and quiet >= cfg.end_silence_frames:
                break
            if not spoke and n >= cfg.wait_frames:
                return None
            if n >= cfg.max_frames:
                break
        if not spoke:
            return None
        return np.concatenate(buf).astype(np.float32) / 32768.0
