"""Speech to text with faster-whisper, plus clean-up of what comes back."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

# Whisper's stock outputs for silence, breath or noise: treat them as nothing said.
_HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "bye", "okay", "ok", "so", "uh", "um", "hmm", "mm",
    "please subscribe", "subtitles by the amara org community",
}
# "Hey Jarvis," at the start of a transcript, as Whisper tends to spell it.
_WAKE = re.compile(r"^\W*(?:(?:hey|hi|hay|a|okay|ok)\W+)?(?:jarvis|jarvis's|jervis|javis|travis)\b\W*", re.I)


# Names Whisper should expect (it biases decoding towards them). The Transcriber adds the user's own apps and
# projects (autocorrect.hotwords); this is the fallback.
HOTWORDS = "Jarvis, Claude Code, VS Code, Shopify"


def fix_names(text: str) -> str:
    """'build it with cloud code' -> 'build it with Claude Code' (the fixed mishearings in autocorrect.FIXED)."""
    from jarvis.core.autocorrect import apply_fixed

    return apply_fixed(text)


def _hotwords() -> str:
    try:
        from jarvis.core.autocorrect import hotwords

        return hotwords()
    except Exception:
        return HOTWORDS


def decode_audio(data: bytes) -> np.ndarray:
    """Any recording PyAV can open -> 16 kHz mono float32 in [-1, 1].

    Our own decoder because faster-whisper 1.2.1's passes `metadata_errors=` to av.open(),
    which PyAV 19 removed: every uploaded recording failed (caught by test_web.py)."""
    import io

    import av

    chunks = []
    with av.open(io.BytesIO(data)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def strip_wake(text: str) -> str:
    return _WAKE.sub("", text.strip(), count=1).strip()


def is_nothing(text: str) -> bool:
    return re.sub(r"[^a-z ]", "", text.lower()).strip() in _HALLUCINATIONS


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, models_dir: str | Path):
        if device == "cuda":
            from jarvis.voice.cuda import add_cuda_dlls

            add_cuda_dlls()
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model, device=device, compute_type=compute_type,
                                   download_root=str(Path(models_dir) / "whisper"))
        self.hotwords = _hotwords()  # your apps and projects, so "Spotify" isn't heard as "spot if I"

    def warm(self) -> None:
        """The first GPU run compiles kernels; do it now rather than on the first question."""
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe_file(self, data: bytes) -> str:
        """A recorded file (the web app's mic: AAC/MP4 on iPhone Safari, WebM/Opus on Chrome)."""
        segments, _ = self._model.transcribe(
            decode_audio(data), language="en", beam_size=5, condition_on_previous_text=False,
            hotwords=self.hotwords, vad_filter=True,
        )
        return fix_names(" ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8).strip())

    def transcribe(self, audio: np.ndarray) -> str:
        """16 kHz mono float32 in [-1, 1] -> text."""
        segments, _ = self._model.transcribe(
            audio, language="en", beam_size=5, condition_on_previous_text=False,
            hotwords=self.hotwords, vad_filter=False,
        )
        return fix_names(" ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8).strip())
