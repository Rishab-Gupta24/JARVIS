"""Wake word ("hey jarvis") and voice activity detection, both from openWakeWord's
ONNX models on the CPU."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from jarvis.voice.oww import ensure_models, openwakeword


class WakeWord:
    def __init__(self, models_dir: str | Path, name: str = "hey_jarvis"):
        folder = ensure_models(Path(models_dir) / "openwakeword", name)
        openwakeword()
        from openwakeword.model import Model

        self._key = f"{name}_v0.1"
        self._model = Model(
            wakeword_models=[str(folder / f"{self._key}.onnx")], inference_framework="onnx",
            melspec_model_path=str(folder / "melspectrogram.onnx"),
            embedding_model_path=str(folder / "embedding_model.onnx"),
        )

    def score(self, frame: np.ndarray) -> float:
        return float(self._model.predict(frame)[self._key])

    def reset(self) -> None:
        """Clears its audio buffers too, so the same 'hey jarvis' can't fire twice."""
        self._model.reset()


class SileroVad:
    def __init__(self, models_dir: str | Path):
        folder = ensure_models(Path(models_dir) / "openwakeword")
        openwakeword()
        from openwakeword.vad import VAD

        self._vad = VAD(model_path=str(folder / "silero_vad.onnx"))

    def prob(self, frame: np.ndarray) -> float:
        """Probability that this 80 ms frame is speech."""
        return float(self._vad.predict(frame, frame_size=640))

    def reset(self) -> None:
        self._vad.reset_states()
