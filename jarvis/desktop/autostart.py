"""Start Jarvis when you log in to Windows: a shortcut in your Startup folder that runs
`pythonw -m jarvis up` (no console window). `python -m jarvis autostart on|off|status`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from jarvis.config import ROOT

STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
SHORTCUT_NAME = "Jarvis.lnk"


def shortcut_path(startup: Path = STARTUP) -> Path:
    return startup / SHORTCUT_NAME


def enable(startup: Path = STARTUP) -> Path:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        raise RuntimeError(f"Can't find {pythonw}; create the venv first.")
    icon = ROOT / "jarvis" / "web" / "icon.ico"
    link = shortcut_path(startup)
    startup.mkdir(parents=True, exist_ok=True)
    # The Windows Script Host shortcut object is the standard way to write a .lnk.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:J_LINK); "
        "$s.TargetPath = $env:J_TARGET; $s.Arguments = '-m jarvis up'; "
        "$s.WorkingDirectory = $env:J_DIR; $s.Description = 'Jarvis assistant'; "
        "if (Test-Path $env:J_ICON) { $s.IconLocation = $env:J_ICON }; $s.Save()"
    )
    env = {**os.environ, "J_LINK": str(link), "J_TARGET": str(pythonw), "J_DIR": str(ROOT), "J_ICON": str(icon)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, env=env,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return link


def disable(startup: Path = STARTUP) -> bool:
    link = shortcut_path(startup)
    if link.exists():
        link.unlink()
        return True
    return False


def is_enabled(startup: Path = STARTUP) -> bool:
    return shortcut_path(startup).exists()
