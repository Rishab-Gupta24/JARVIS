"""When the local agents are stuck on a task, ask Claude Code to fix it (the user asked for this,
7 Nov 2026). It runs headless in the game's folder, sees only that folder, may edit files and run the
project's tests, and its usage counts against the user's Claude plan. Calls are limited per task and per
project by the loop."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python -m pytest:*)", "Bash(node --test:*)",
         "Bash(python main.py:*)"]

PROMPT = """You are helping Jarvis Studio, an automated loop of small local AI agents that builds an original game in
this folder. It got stuck on one task. Please make it pass.

{task}

Why it's stuck (latest output):
{failure}

Project conventions:
{conventions}

Rules:
- Work only inside this folder.
- Make ALL tests pass ({test_cmd}) and keep the automatic play-through working ({smoke_cmd}).
- Don't change test files unless a test contradicts the task's acceptance criteria; say so if you do.
- Write as little code as works: reuse what's there, standard library first, no new packages.
- The game's names, characters and art are original; don't copy any existing game.
Finish with a two-line summary of what you changed."""


@dataclass
class HelperResult:
    ok: bool
    summary: str


def claude_executable() -> list[str] | None:
    """The real claude.exe if npm installed it (no cmd.exe in between), else the shim on PATH."""
    shim = shutil.which("claude")
    if shim:
        exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.is_file():
            return [str(exe)]
        return [shim]
    appdata = os.environ.get("APPDATA")
    exe = Path(appdata or "") / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return [str(exe)] if appdata and exe.is_file() else None


class ClaudeCodeHelper:
    name = "Claude Code"

    def __init__(self, timeout: int = 1500, runner=subprocess.run):
        self.timeout = timeout
        self.runner = runner

    def available(self) -> bool:
        return claude_executable() is not None

    def ask(self, root: Path, task_text: str, failure: str, stack) -> HelperResult:
        exe = claude_executable()
        if exe is None:
            return HelperResult(False, "Claude Code isn't installed.")
        test_cmd = "python -m pytest -q tests" if stack.name == "pygame" else "node --test"
        smoke_cmd = ("python main.py --autoplay --frames 600 --trace .studio/trace.json" if stack.name == "pygame"
                     else "smoke.html in a browser")
        prompt = PROMPT.format(task=task_text, failure=failure[-6000:], conventions=stack.conventions,
                               test_cmd=test_cmd, smoke_cmd=smoke_cmd)
        argv = [*exe, "-p", "--output-format", "json", "--permission-mode", "acceptEdits", "--allowedTools", *TOOLS]
        try:
            done = self.runner(argv, input=prompt, cwd=root, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return HelperResult(False, f"Claude Code didn't finish within {self.timeout // 60} minutes.")
        except OSError as e:
            return HelperResult(False, f"Couldn't start Claude Code: {e}")
        text = (done.stdout or "").strip()
        try:
            data = json.loads(text.splitlines()[-1]) if text else {}
        except json.JSONDecodeError:
            data = {"result": text}
        if done.returncode != 0 or data.get("is_error"):
            return HelperResult(False, str(data.get("result") or done.stderr or f"exit code {done.returncode}")[-800:])
        return HelperResult(True, str(data.get("result", "")).strip()[-800:])
