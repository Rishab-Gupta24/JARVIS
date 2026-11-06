"""The voice loop's state machine with stand-in parts (no models, no sound card),
and the text clean-up around speech."""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.voice import stt, tts
from jarvis.voice.audio import FRAME, WavFile, listening_chime, to_pcm16k
from jarvis.voice.loop import LoopConfig, VoiceLoop

# One character per 80 ms frame: W = "hey jarvis" (the wake word fires), S = speech, _ = quiet,
# w = something close to "hey jarvis" that doesn't reach the threshold.
CODES = {"_": 0, "S": 1, "W": 2, "w": 3}


def script(pattern: str) -> np.ndarray:
    return np.concatenate([np.full(FRAME, CODES[c], dtype=np.int16) for c in pattern])


class FakeWake:
    def __init__(self):
        self.resets = 0

    def score(self, frame):
        return {2: 1.0, 3: 0.35}.get(int(frame[0]), 0.0)

    def reset(self):
        self.resets += 1


class FakeVad:
    def prob(self, frame):
        return 1.0 if frame[0] else 0.0

    def reset(self):
        pass


class FakeStt:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.heard = []  # frame codes of each utterance it was given

    def transcribe(self, audio):
        codes = (audio[::FRAME] * 32768).round().astype(int)
        self.heard.append("".join("_SW"[c] for c in codes))
        return self.texts.pop(0)


class FakeMouth:
    def __init__(self):
        self.said, self.chimes = [], 0

    def say(self, text):
        self.said.append(text)

    def chime(self):
        self.chimes += 1


class Source(WavFile):
    def __init__(self, pattern):
        super().__init__(audio=script(pattern))
        self.flushes = 0

    def flush(self):
        self.flushes += 1


def run(pattern, *transcripts, answers=None):
    src, wake, mouth, fake_stt = Source(pattern), FakeWake(), FakeMouth(), FakeStt(*transcripts)
    asked = []
    replies = list(answers or [])

    def answer(text):
        asked.append(text)
        return replies.pop(0) if replies else f"answer to {text}"

    log = []
    VoiceLoop(src, wake, FakeVad(), fake_stt, mouth, answer, LoopConfig(), log.append).run()
    return asked, mouth, fake_stt, src, log


def test_wake_word_then_question_gets_spoken_answer():
    asked, mouth, heard, src, log = run("___W" + "S" * 15 + "_" * 12 + "____",
                                        "Hey Jarvis, what's on tomorrow?")
    assert asked == ["what's on tomorrow?"]
    assert mouth.chimes == 0  # kept talking: no chime to mute the mic mid-sentence
    assert mouth.said == ["answer to what's on tomorrow?"]
    assert heard.heard[0].startswith("___S")  # the 0.3 s before the wake word is kept
    assert src.flushes == 1  # Jarvis's own voice is dropped before listening again
    assert any(line.startswith("you: what's on tomorrow?") for line in log)


def test_speech_without_wake_word_is_ignored():
    asked, mouth, *_ = run("S" * 30 + "_" * 20)
    assert asked == [] and mouth.said == [] and mouth.chimes == 0


def test_wake_word_then_nothing_times_out():
    asked, mouth, heard, *_ = run("W" + "_" * 60)
    assert asked == [] and heard.heard == [] and mouth.said == []


def test_chime_only_when_the_user_pauses_after_the_wake_word():
    asked, mouth, *_ = run("W" + "_" * 8 + "S" * 10 + "_" * 11, "What's on tomorrow?")
    assert mouth.chimes == 1 and asked == ["What's on tomorrow?"]


def test_one_word_fragment_is_asked_again():
    asked, mouth, *_ = run("W" + "S" * 4 + "_" * 11 + "S" * 10 + "_" * 11,
                           "tomorrow.", "What's on tomorrow?")
    assert mouth.said[0] == "Sorry, I only caught 'tomorrow'. Could you say that again?"
    assert asked == ["What's on tomorrow?"]


def test_just_hey_jarvis_then_a_pause_asks_yes():
    asked, mouth, *_ = run("W" + "S" + "_" * 11 + "S" * 10 + "_" * 11,
                           "Hey Jarvis.", "What's on tomorrow?")
    assert mouth.said == ["Yes?", "answer to What's on tomorrow?"]
    assert asked == ["What's on tomorrow?"]


def test_question_back_means_listen_again_without_wake_word():
    asked, mouth, *_ = run("W" + "S" * 10 + "_" * 11 + "S" * 8 + "_" * 11,
                           "Hey Jarvis, cancel football", "Saturday",
                           answers=["Which one, Friday or Saturday?", "Deleted Football practice."])
    assert asked == ["cancel football", "Saturday"]
    assert mouth.said[-1] == "Deleted Football practice."


def test_whisper_noise_is_not_a_question():
    asked, mouth, *_ = run("W" + "S" * 5 + "_" * 11 + "S" * 3 + "_" * 11, "Thank you.", "you")
    assert asked == [] and mouth.said == ["Yes?"]


def test_long_speech_is_cut_at_15_seconds():
    asked, mouth, heard, *_ = run("W" + "S" * 250 + "_" * 15, "Hey Jarvis, a very long ramble")
    assert len(heard.heard[0]) == LoopConfig().max_frames
    assert asked == ["a very long ramble"]


def test_near_miss_is_reported_once():
    asked, mouth, _, _, log = run("__www___www__")
    assert asked == [] and mouth.chimes == 0
    assert log == ["(close to 'hey jarvis': 0.35, needs 0.5)"] * 2


def test_back_to_waiting_after_each_conversation():
    asked, *_ = run("W" + "S" * 5 + "_" * 11 + "S" * 20 + "W" + "S" * 5 + "_" * 11,
                    "Hey Jarvis, first question", "Hey Jarvis, second question")
    assert asked == ["first question", "second question"]


@pytest.mark.parametrize("heard, meant", [
    ("Hey Jarvis, what's on tomorrow?", "what's on tomorrow?"),
    ("hey, Jarvis. Add gym at 6", "Add gym at 6"),
    ("Jarvis what's for lunch", "what's for lunch"),
    ("Hey Jervis, cancel gym", "cancel gym"),
    ("What's on tomorrow?", "What's on tomorrow?"),
    ("Tell Jarvis's mum", "Tell Jarvis's mum"),  # only at the start
])
def test_strip_wake(heard, meant):
    assert stt.strip_wake(heard) == meant


@pytest.mark.parametrize("text, nothing", [
    ("Thank you.", True), (" you", True), ("Thanks for watching!", True), ("", True), ("...", True),
    ("Thank you, add gym at 6", False), ("lunch", False),
])
def test_is_nothing(text, nothing):
    assert stt.is_nothing(text) is nothing


@pytest.mark.parametrize("written, spoken", [
    ("Added DBMS quiz, Monday 9 November at 10 am, marked important.",
     "Added DBMS quiz, Monday 9 November at 10 a.m., marked important."),
    ("Dentist at 4:30 pm and gym at 6:00 pm.", "Dentist at 4 30 p.m. and gym at 6 p.m."),
    ("Breakfast: Poha, banana, tea/coffee.", "Breakfast: Poha, banana, tea or coffee."),
    ("I am **sure** it's at 14:00.", "I am sure it's at 14."),
    ("Rice & dal", "Rice and dal"),
])
def test_for_speech(written, spoken):
    assert tts.for_speech(written) == spoken


def test_sentences_are_spoken_one_at_a_time():
    assert tts.sentences("Added gym, Sunday 8 November at 6 pm. Added call mom, today at 8 pm.") == [
        "Added gym, Sunday 8 November at 6 p.m.", "Added call mom, today at 8 p.m.",
    ]
    assert tts.sentences("Is it 2.5 hours? Yes.") == ["Is it 2.5 hours?", "Yes."]


@pytest.mark.parametrize("fmt, codec", [("mp4", "aac"), ("webm", "libopus"), ("wav", "pcm_s16le")])
def test_decode_audio_from_browser_recordings(fmt, codec):
    """iPhone Safari records AAC/MP4, Chrome Opus/WebM; both must come out as 16 kHz mono."""
    import io

    import av

    rate = 48000
    tone = (np.sin(np.linspace(0, 2 * np.pi * 440 * 1.5, int(rate * 1.5))) * 0.5 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with av.open(buf, "w", format=fmt) as out:
        stream = out.add_stream(codec, rate=rate)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(tone[None, :], format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in list(stream.encode(frame)) + list(stream.encode(None)):
            out.mux(packet)
    audio = stt.decode_audio(buf.getvalue())
    assert audio.dtype == np.float32 and abs(len(audio) / 16000 - 1.5) < 0.1
    assert 0.3 < np.abs(audio).max() <= 1.0


def test_audio_helpers():
    one_second_48k = np.sin(np.linspace(0, 2 * np.pi * 440, 48000)).astype(np.float32) * 0.5
    pcm = to_pcm16k(one_second_48k, 48000)
    assert pcm.dtype == np.int16 and len(pcm) == 16000 and 15000 < pcm.max() < 17000
    chime = listening_chime()
    assert 0.1 < len(chime) / 24000 < 0.2 and np.abs(chime).max() <= 0.15
