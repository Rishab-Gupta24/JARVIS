"""Commands that run only after the user says yes.

The model can only *propose* a command (the run_command tool stores it). It runs
when the user's very next words in the same conversation are a clear yes, which
is checked here in plain Python, not by the model. A proposal expires after two
minutes; any other reply drops it. A few catastrophic commands are refused even
with a yes, because a misheard "yes" there can't be undone.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

EXPIRES = timedelta(minutes=2)
TIMEOUT_S = 60

YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|run it|confirm(ed)?|please do|affirmative)"
                 r"\b[\w\s,.!']{0,20}$", re.I)
NO = re.compile(r"^\W*(no|nope|cancel|stop|don'?t|never ?mind|abort|forget it)\b", re.I)

# Refused outright: wiping disks, deleting whole drives or system folders, boot/registry surgery.
NEVER = re.compile(
    r"\b(format-volume|format\s+[a-z]:|diskpart|clear-disk|initialize-disk|remove-partition|bcdedit|"
    r"cipher\s+/w|vssadmin\s+delete|"
    r"reg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)|takeown\s+/f\s+[a-z]:\\\s*$)"
    r"|(rm|del|erase|rmdir|rd|remove-item)\b[^|;&]*\s(-rf?\s+)?[\"']?([a-z]:\\?|\\|/|~|\$env:(systemroot|windir)|"
    r"c:\\windows|c:\\users\\?|c:\\program files)[\"']?\s*(-|/|$)",
    re.I,
)


@dataclass
class Pending:
    command: str
    purpose: str
    proposed_at: datetime


class PendingCommands:
    """One proposed command per conversation (session)."""

    def __init__(self):
        self._items: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def propose(self, session: str, command: str, purpose: str, now: datetime) -> None:
        with self._lock:
            self._items[session] = Pending(command, purpose, now)

    def take(self, session: str, now: datetime) -> Pending | None:
        """Remove and return the session's proposal, if it hasn't expired."""
        with self._lock:
            p = self._items.pop(session, None)
        return p if p and now - p.proposed_at <= EXPIRES else None

    def peek(self, session: str, now: datetime) -> Pending | None:
        with self._lock:
            p = self._items.get(session)
        return p if p and now - p.proposed_at <= EXPIRES else None


PENDING = PendingCommands()


def refuse_reason(command: str) -> str | None:
    if not command.strip():
        return "There's no command to run."
    if NEVER.search(command):
        return "That command could wipe data or break Windows, so I won't run it by voice. Do that one yourself."
    return None


def run(command: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run in PowerShell. Returns (exit code, combined output, trimmed)."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=TIMEOUT_S, cwd=str(cwd or Path.home()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
        )
    except subprocess.TimeoutExpired:
        return -1, f"It was still running after {TIMEOUT_S} seconds, so I stopped waiting."
    out = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
    return done.returncode, out.strip()[-4000:]


def spoken_result(code: int, output: str) -> str:
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if code == 0:
        return f"Done. {first[:200]}" if first else "Done."
    if code == -1:
        return output
    return f"That failed (exit code {code}). {first[:200]}".strip()
