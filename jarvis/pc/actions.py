"""Things Jarvis may do on this PC. The safe set needs no confirmation and never
runs a shell: it launches installed apps (by their Start Menu shortcuts), opens
projects, folders and websites, presses media keys and locks the screen.

Arbitrary commands are in confirm.py and only run after the user says yes.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HOME = Path.home()
START_MENUS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
]
# Spoken names -> what to start. Anything else is looked up in the Start Menu.
BUILTIN_APPS = {
    "vs code": ["code"], "vscode": ["code"], "visual studio code": ["code"], "code": ["code"],
    "terminal": ["wt"], "windows terminal": ["wt"], "command prompt": ["cmd", "/c", "start", "cmd"],
    "powershell": ["wt", "powershell"], "file explorer": ["explorer"], "explorer": ["explorer"],
    "files": ["explorer"], "notepad": ["notepad"], "calculator": ["calc"], "paint": ["mspaint"],
    "task manager": ["taskmgr"], "settings": ["start:ms-settings:"], "browser": ["start:https://"],
}
_SKIP_SHORTCUTS = re.compile(r"uninstall|readme|help|documentation|website|license|release notes", re.I)


class ActionError(Exception):
    """Something the user should hear about: not found, not allowed, failed."""


@dataclass(frozen=True)
class Project:
    name: str
    path: Path


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _best(query: str, names: list[str], cutoff: float = 0.6) -> str | None:
    q = _words(query)
    exact = [n for n in names if _words(n) == q]
    if exact:
        return exact[0]
    contains = [n for n in names if q and q in _words(n)]
    if contains:
        return min(contains, key=len)
    close = difflib.get_close_matches(q, [_words(n) for n in names], n=1, cutoff=cutoff)
    return next((n for n in names if _words(n) == close[0]), None) if close else None


# ---------- apps ----------

def start_menu_apps() -> dict[str, Path]:
    apps: dict[str, Path] = {}
    for root in START_MENUS:
        if root.is_dir():
            for lnk in root.rglob("*.lnk"):
                if not _SKIP_SHORTCUTS.search(lnk.stem):
                    apps.setdefault(lnk.stem, lnk)
    return apps


def _launch(argv: list[str]) -> None:
    if argv[0].startswith("start:"):
        os.startfile(argv[0][len("start:"):])  # noqa: S606 (a URL or settings page, not a command)
        return
    exe = shutil.which(argv[0])
    if exe is None:
        raise ActionError(f"{argv[0]} isn't installed.")
    subprocess.Popen([exe, *argv[1:]], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


CLAUDE_NAMES = {"claude", "claude code", "claude cli", "claude code cli", "cloud code"}


def open_app(name: str) -> str:
    key = _words(name)
    if key in CLAUDE_NAMES:
        return open_claude_code(HOME)
    if key in BUILTIN_APPS:
        _launch(BUILTIN_APPS[key])
        return f"Opening {name}."
    apps = start_menu_apps()
    match = _best(name, list(apps))
    if match is None:
        raise ActionError(f"I couldn't find an app called {name}.")
    os.startfile(apps[match])  # noqa: S606 (an installed app's Start Menu shortcut)
    return f"Opening {match}."


# Spoken names -> the program's file name, for closing (opening uses the Start Menu).
APP_EXES = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox", "brave": "brave", "vs code": "code", "vscode": "code", "visual studio code": "code",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook", "teams": "ms-teams",
    "spotify": "spotify", "discord": "discord", "whatsapp": "whatsapp", "notepad": "notepad",
    "paint": "mspaint", "task manager": "taskmgr", "terminal": "windowsterminal", "windows terminal":
    "windowsterminal", "powershell": "windowsterminal", "command prompt": "cmd", "vlc": "vlc", "steam": "steam",
    "zoom": "zoom", "telegram": "telegram", "obs": "obs64", "claude": "claude",
}
# Never closed by name: the Windows shell and Jarvis's own processes.
_KEEP_RUNNING = {"explorer", "dwm", "winlogon", "csrss", "lsass", "services", "svchost", "sihost",
                 "shellexperiencehost", "startmenuexperiencehost", "searchhost", "textinputhost"}
_FOLDER_WINDOW = "CabinetWClass"  # a File Explorer folder window (closing it leaves the shell alone)


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    exe: str      # file name without .exe, lower case
    pid: int
    cls: str = ""


def open_windows() -> list[Window]:
    """Visible top-level windows with a title, and the program each belongs to."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found: list[Window] = []

    def exe_of(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).stem.lower()
            return ""
        finally:
            kernel32.CloseHandle(handle)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER: skip dialogs/tool windows
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(Window(int(hwnd), title.value, exe_of(pid.value), pid.value, cls.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def windows_for(name: str, windows: list[Window]) -> list[Window]:
    """The windows of the app the user named: by program name first, then by window title."""
    key = _words(name)
    if key in ("file explorer", "explorer", "files", "folder", "folders", "file manager"):
        return [w for w in windows if w.cls == _FOLDER_WINDOW]
    closable = [w for w in windows if w.exe not in _KEEP_RUNNING and w.pid != os.getpid()
                and not re.search(r"\bjarvis\b", w.title, re.I)]
    exe = APP_EXES.get(key)
    if exe:
        hits = [w for w in closable if w.exe == exe]
        if hits:
            return hits
    squashed = key.replace(" ", "")
    hits = [w for w in closable if squashed and (w.exe == squashed or w.exe.startswith(squashed))]
    if hits:
        return hits
    hits = [w for w in closable if key and re.search(rf"\b{re.escape(key)}\b", _words(w.title))]
    if hits:
        return hits
    by_exe = {w.exe: w for w in closable if w.exe}
    close = difflib.get_close_matches(squashed, list(by_exe), n=1, cutoff=0.75)
    return [w for w in closable if close and w.exe == close[0]]


def close_app(name: str, windows: list[Window] | None = None, post=None) -> str:
    """Ask the named app's windows to close (WM_CLOSE: like clicking X, so it can still ask to save)."""
    windows = open_windows() if windows is None else windows
    if _words(name) in ("this", "this window", "this app", "current window", "current app", "active window"):
        raise ActionError("Tell me which app to close by name, like 'close Chrome'.")
    hits = windows_for(name, windows)
    if not hits:
        raise ActionError(f"I don't see {name} open.")
    if post is None:
        if sys.platform != "win32":
            raise ActionError("I can only close apps on Windows.")
        import ctypes

        def post(hwnd: int) -> None:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    for w in hits:
        post(w.hwnd)
    label = name if len(hits) == 1 else f"{len(hits)} {name} windows"
    return f"Closing {label}."


# ---------- projects and folders ----------

def discover_projects(extra: dict[str, str] | None = None, roots: list[Path] | None = None) -> list[Project]:
    """Projects the user named in JARVIS_PROJECTS, then git repos in the usual folders."""
    found: dict[str, Project] = {}
    for name, path in (extra or {}).items():
        if Path(path).is_dir():
            found[name.lower()] = Project(name, Path(path))
    for root in roots if roots is not None else [HOME, HOME / "Documents", HOME / "Desktop",
                                                HOME / "source" / "repos", HOME / "Projects"]:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            try:
                if child.is_dir() and (child / ".git").exists():
                    found.setdefault(child.name.lower(), Project(child.name, child))
            except OSError:
                continue
    return list(found.values())


def find_project(name: str, projects: list[Project]) -> Project:
    query = re.sub(r"\b(my|the|project|repo|folder|code|codebase)\b", " ", name, flags=re.I).strip() or name
    match = _best(query, [p.name for p in projects])
    if match is None:
        known = ", ".join(p.name for p in projects) or "none yet"
        raise ActionError(f"I don't know a project called {name}. Projects I know: {known}.")
    return next(p for p in projects if p.name == match)


def open_in_vscode(project: Project) -> str:
    _launch(["code", str(project.path)])
    return f"Opening {project.name} in VS Code."


# Words that say *what kind of request* it is, not what the project is about.
_NAME_STOP = set("""a an the and or for of to in on with using use by from my me i we you it this that some
    please can could would will should want wanna need like just new build make create code write start
    develop design set up setup project app application program simple basic small little quick cool nice
    claude cloud vs visual studio help let lets let's get give do does using""".split())


def project_name_for(text: str) -> str:
    """'build me a website for my Shopify store' -> 'website-shopify-store' (the user's own words, in order)."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _NAME_STOP and len(w) > 1]
    return "-".join(words[:3]) or "new-project"


def create_project(name: str, root: Path) -> Project:
    """A new empty folder under root (git-initialised, so Jarvis finds it by name later)."""
    slug = "-".join(re.findall(r"[a-z0-9]+", name.lower()))[:40].strip("-") or "new-project"
    root.mkdir(parents=True, exist_ok=True)
    path, n = root / slug, 2
    while path.exists():
        path, n = root / f"{slug}-{n}", n + 1
    path.mkdir()
    git = shutil.which("git")
    if git:
        subprocess.run([git, "init", "-q", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    return Project(path.name, path)


def _claude_argv(folder: Path, task: str | None) -> list[str]:
    claude = shutil.which("claude")
    if claude is None:
        raise ActionError("Claude Code isn't installed on this PC yet.")
    wt = shutil.which("wt")
    tail = [claude, task] if task else [claude]
    return ([wt, "-d", str(folder), *tail] if wt
            else ["cmd", "/c", "start", "", "/d", str(folder), *tail])


def open_claude_code(folder: Path = HOME) -> str:
    subprocess.Popen(_claude_argv(folder, None), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return "Opening Claude Code."


def clean_task(task: str) -> str:
    # claude is a .cmd shim, so cmd.exe reads the task: drop characters it would treat as
    # commands or variables, and ';' which Windows Terminal reads as "open another tab".
    task = re.sub(r'[&|<>^%;"`]', " ", task.replace('"', "'"))
    return re.sub(r"\s+", " ", task).strip()[:500]


def _local_coder_argv(folder: Path, task: str | None) -> list[str]:
    """Jarvis's own coder (jarvis.coder) in a terminal window, run from the Jarvis folder."""
    from jarvis.config import ROOT

    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":  # started at login without a console: use the console one
        python = python.with_name("python.exe")
    tail = [str(python), "-m", "jarvis.coder", str(folder), *([task] if task else [])]
    wt = shutil.which("wt")
    return ([wt, "-d", str(ROOT), *tail] if wt else ["cmd", "/c", "start", "", "/d", str(ROOT), *tail])


def _studio_argv(request: str) -> list[str]:
    """Jarvis Studio (jarvis.studio) in a terminal window: plans, builds and tests a whole game."""
    from jarvis.config import ROOT

    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    tail = [str(python), "-m", "jarvis.studio", "new", request]
    wt = shutil.which("wt")
    return ([wt, "-d", str(ROOT), *tail] if wt else ["cmd", "/c", "start", "", "/d", str(ROOT), *tail])


def start_studio(request: str) -> str:
    request = clean_task(request)
    subprocess.Popen(_studio_argv(request), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return (f"I'm starting the studio on: {request}. It researches and plans first (PRD, sketches, tasks), then "
            "builds and tests it in a loop. I'll message your phone as it goes; ask me how it's going any time.")


def start_ai_coder(project: Project, task: str, coder: str = "local") -> str:
    """VS Code on the project, plus a coding agent in a terminal there working on the task:
    Jarvis's local coder by default, or Claude Code. Both ask before changing files."""
    open_in_vscode(project)
    task = clean_task(task)
    if coder == "claude":
        try:
            argv = _claude_argv(project.path, task)
        except ActionError:
            raise ActionError(f"I opened {project.name} in VS Code, but Claude Code isn't installed on this PC "
                              "yet.") from None
        who = "Claude Code"
    else:
        argv, who = _local_coder_argv(project.path, task), "my coder"
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if not task:
        return f"Opening {project.name} in VS Code, with {who} ready in a terminal."
    return f"Opening {project.name} in VS Code and starting {who} on: {task}"


def open_folder(name: str, projects: list[Project]) -> str:
    known = {"downloads": HOME / "Downloads", "documents": HOME / "Documents", "desktop": HOME / "Desktop",
             "pictures": HOME / "Pictures", "music": HOME / "Music", "videos": HOME / "Videos", "home": HOME}
    key = _words(name).replace("my ", "")
    path = known.get(key)
    if path is None:
        path = find_project(name, projects).path
    os.startfile(path)  # noqa: S606 (a folder)
    return f"Opening {path.name or path}."


def open_website(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", url, re.I):
            url = "https://" + url
        else:
            raise ActionError("That doesn't look like a website address.")
    os.startfile(url)  # noqa: S606 (an http(s) URL opens in the default browser)
    return f"Opening {re.sub(r'^https?://', '', url).rstrip('/')}."


# ---------- keys, screen ----------

MEDIA_KEYS = {"play": 0xB3, "pause": 0xB3, "play pause": 0xB3, "next": 0xB0, "previous": 0xB1,
              "volume up": 0xAF, "volume down": 0xAE, "mute": 0xAD}


def press_media(action: str, times: int = 1) -> str:
    key = MEDIA_KEYS.get(_words(action))
    if key is None:
        raise ActionError(f"I can't do '{action}' with the media keys.")
    if sys.platform == "win32":
        import ctypes

        for _ in range(max(1, min(times, 20))):
            ctypes.windll.user32.keybd_event(key, 0, 0, 0)
            ctypes.windll.user32.keybd_event(key, 0, 2, 0)
    return {"volume up": "Volume up.", "volume down": "Volume down.", "mute": "Muted or unmuted.",
            "next": "Next track.", "previous": "Previous track."}.get(_words(action), "Done.")


def lock_screen() -> str:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.LockWorkStation()
    return "Locking the screen."


def screenshot(folder: Path | None = None) -> str:
    from PIL import ImageGrab

    folder = folder or HOME / "Pictures" / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    ImageGrab.grab().save(path)
    return f"Saved a screenshot to Pictures, Jarvis, {path.name}."
