"""Import openWakeWord without its training extras.

openwakeword/__init__.py imports `custom_verifier_model`, which needs scikit-learn.
On this PC, Windows Smart App Control blocks one of scikit-learn's compiled files,
so the import fails. Jarvis only runs pre-trained models and never trains
verifiers, so a placeholder module stands in for that one import. Windows
security settings are left as they are.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


def _no_training(*args, **kwargs):
    raise RuntimeError("Training custom verifiers isn't available in Jarvis (scikit-learn is blocked).")


def openwakeword():
    name = "openwakeword.custom_verifier_model"
    if name not in sys.modules:
        stub = types.ModuleType(name)
        stub.train_custom_verifier = _no_training
        sys.modules[name] = stub
    import openwakeword as oww

    return oww


def ensure_models(folder: str | Path, wake_model: str = "hey_jarvis") -> Path:
    """Download the wake word, feature and VAD models once; returns the folder."""
    folder = Path(folder)
    needed = [f"{wake_model}_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx"]
    if not all((folder / f).exists() for f in needed):
        folder.mkdir(parents=True, exist_ok=True)
        openwakeword()
        from openwakeword.utils import download_models

        download_models(model_names=[wake_model], target_directory=str(folder))
    return folder
