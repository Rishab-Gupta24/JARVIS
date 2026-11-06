"""Make the CUDA libraries from the nvidia-* pip wheels visible to CTranslate2 on Windows.

faster-whisper runs on CTranslate2, which loads cuBLAS and cuDNN as DLLs. The pip
wheels put them in site-packages/nvidia/*/bin, which Windows doesn't search, so
they have to be added before the first model is created.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False


def add_cuda_dlls() -> list[str]:
    global _done
    if _done or sys.platform != "win32":
        return []
    _done = True
    # The CUDA libraries in these wheels have no ready-made code for the RTX 50 series,
    # so the driver compiles it on first use (~1 min). Its cache defaults to 256 MB, too
    # small to keep that work between runs; give it room so it only happens once.
    os.environ.setdefault("CUDA_CACHE_MAXSIZE", str(4 * 1024**3))
    added = []
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            # CTranslate2 also resolves some libraries through PATH.
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            added.append(str(bin_dir))
    return added
