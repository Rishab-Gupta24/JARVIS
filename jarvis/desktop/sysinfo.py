"""CPU, memory and GPU readings for the interface, from Windows itself and nvidia-smi
(no psutil: its compiled module could be blocked by Smart App Control, like scikit-learn was)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    def value(self) -> int:
        return (self.high << 32) | self.low


class CpuMeter:
    """Percent busy since the previous reading."""

    def __init__(self):
        self._last = self._times()

    @staticmethod
    def _times() -> tuple[int, int] | None:
        if sys.platform != "win32":
            return None
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return idle.value(), kernel.value() + user.value()  # kernel time includes idle time

    def read(self) -> float | None:
        now = self._times()
        if now is None or self._last is None:
            return None
        (idle0, total0), (idle1, total1) = self._last, now
        self._last = now
        total = total1 - total0
        return None if total <= 0 else max(0.0, min(100.0, 100.0 * (1 - (idle1 - idle0) / total)))


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total", ctypes.c_ulonglong),
                ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                ("avail_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]


def memory() -> tuple[float, float] | None:
    """(used GB, total GB)."""
    if sys.platform != "win32":
        return None
    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    gb = 1024 ** 3
    return (status.total - status.avail) / gb, status.total / gb


def gpu() -> tuple[float, float, float] | None:
    """(GPU busy %, VRAM used GB, VRAM total GB) from nvidia-smi, or None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW,
        ).stdout.strip().splitlines()[0]
        busy, used, total = (float(x) for x in out.split(","))
        return busy, used / 1024, total / 1024
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
