"""Studio plugins: rules added to the agents' prompts, checks after each task, and reactions to events.

    ponytail       write less code: a decision ladder before writing anything, and a checker afterwards
    test_first     the tester writes a task's tests before the coder starts (the loop reads this flag)
    review         a reviewer agent checks each finished task (the loop reads this flag)
    phone_updates  ntfy messages: plan ready, feature done, asking for help, blocked, game ready

Ponytail here is Jarvis's own version of the idea behind the open-source Ponytail plugin (the user
chose not to download it, 7 Nov 2026).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Callable

SKIP_DIRS = {".git", ".studio", "__pycache__", "node_modules", ".pytest_cache", "docs"}


class Plugin:
    name = ""

    def rules(self, role: str) -> str:
        return ""

    def check(self, root: Path, stack) -> list[str]:
        """Problems the coder must fix before the task counts as done."""
        return []

    def on_event(self, event: str, project_name: str, **data) -> None:
        pass


# ---------------------------------------------------------------- ponytail

LADDER = """Ponytail rule (write less code). Before writing anything, go down this ladder and stop at the first step that works:
1. Does the project already have it? Reuse or extend that function/module.
2. Does the language, its standard library or the engine (pygame / the browser) already do it? Use that.
3. Can a few clear lines inline do it? Write those.
4. Only then add a new function or module, as small as it can be.
Never add a third-party package. No speculative options, no unused helpers, no copy-pasted blocks: share one
function. Keep validation, error handling and correctness: lazy, never careless."""


def _code_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and p.suffix in suffixes and not (set(rel.parts) & SKIP_DIRS):
            out.append(p)
    return out


def _is_test(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).parts
    return rel[0] == "tests" or path.name.startswith("test_") or path.name.endswith((".test.mjs", ".test.js"))


class Ponytail(Plugin):
    name = "ponytail"
    MAX_LINES = {"lite": 10_000, "full": 500, "ultra": 300}

    def __init__(self, level: str = "full"):
        self.level = level if level in self.MAX_LINES or level == "off" else "full"

    def rules(self, role: str) -> str:
        if self.level == "off" or role not in ("coder", "reviewer", "tester", "helper"):
            return ""
        if role == "tester":
            return "\n\nPonytail: test what the acceptance criteria say, nothing more; no test frameworks beyond the standard ones."
        if role == "reviewer":
            return "\n\nReject code that breaks the Ponytail rule:\n" + LADDER
        return "\n\n" + LADDER

    def check(self, root: Path, stack) -> list[str]:
        if self.level in ("off", "lite"):
            return []
        if stack.name == "web":
            return self._check_js(root)
        return self._check_py(root, set(getattr(stack, "allowed_imports", set())))

    def _check_py(self, root: Path, allowed: set[str]) -> list[str]:
        problems, files = [], _code_files(root, (".py",))
        local = {p.relative_to(root).parts[0].removesuffix(".py") for p in files}
        defined: dict[str, Path] = {}
        texts = {}
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            texts[path] = text
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                problems.append(f"{path.relative_to(root).as_posix()} has a syntax error: {e.msg} (line {e.lineno}).")
                continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for mod in mods:
                    top = mod.split(".")[0]
                    if top not in sys.stdlib_module_names and top not in allowed and top not in local and top not in (
                            "pytest",):
                        problems.append(f"{path.relative_to(root).as_posix()} imports '{top}', a package the project "
                                        "doesn't allow. Use the standard library or what the project already has.")
            if not _is_test(path, root):
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_") \
                            and node.name != "main":
                        defined[node.name] = path
            lines = text.count("\n") + 1
            if lines > self.MAX_LINES[self.level]:
                problems.append(f"{path.relative_to(root).as_posix()} has {lines} lines; split it by responsibility.")
        everything = "\n".join(texts.values())
        for name, path in defined.items():
            if len(re.findall(rf"\b{re.escape(name)}\b", everything)) <= 1:
                problems.append(f"{path.relative_to(root).as_posix()}: function '{name}' is never used. Use it or remove it.")
        problems += self._duplicates(texts, root)
        return problems

    def _check_js(self, root: Path) -> list[str]:
        problems, texts = [], {}
        for path in _code_files(root, (".js", ".mjs")):
            text = path.read_text(encoding="utf-8", errors="replace")
            texts[path] = text
            for spec in re.findall(r"""^\s*import\b[^'"]*['"]([^'"]+)['"]""", text, re.M):
                if not spec.startswith((".", "/", "node:")):
                    problems.append(f"{path.relative_to(root).as_posix()} imports '{spec}', an npm package. This "
                                    "project has no packages: use the browser or plain JavaScript.")
                elif spec.startswith(".") and not spec.endswith((".js", ".mjs")):
                    problems.append(f"{path.relative_to(root).as_posix()} imports '{spec}' without the .js "
                                    "extension; browsers need the full file name.")
        problems += self._duplicates(texts, root)
        return problems

    @staticmethod
    def _duplicates(texts: dict[Path, str], root: Path, window: int = 8) -> list[str]:
        seen: dict[str, tuple[Path, int]] = {}
        found = []
        for path, text in texts.items():
            if _is_test(path, root):
                continue
            lines = [ln.strip() for ln in text.splitlines()]
            for i in range(len(lines) - window + 1):
                chunk = lines[i:i + window]
                if sum(1 for ln in chunk if len(ln) > 3) < window - 1:
                    continue  # mostly blank lines and brackets
                key = "\n".join(chunk)
                first = seen.setdefault(key, (path, i))
                if first[0] != path or i - first[1] >= window:  # another file, or a non-overlapping repeat
                    where = lambda p, n: f"{p.relative_to(root).as_posix()}:{n + 1}"  # noqa: E731
                    found.append(f"The same {window} lines appear at {where(*first)} and {where(path, i)}; "
                                 "share one function.")
                    break
        return found[:5]


# ---------------------------------------------------------------- flags read by the loop

class TestFirst(Plugin):
    name = "test_first"

    def rules(self, role: str) -> str:
        if role == "tester":
            return ("\n\nTest-first: write the tests now, before the code exists. They are expected to fail until the "
                    "coder finishes; that's fine. Test behaviour through the public functions the task names.")
        if role == "coder":
            return "\n\nTest-first: the tests for this task already exist and are fixed. Make them pass by writing the code."
        return ""


class Review(Plugin):
    name = "review"


class Playtest(Plugin):
    name = "playtest"  # the loop plays the finished game and files fix tasks (playtest.py)


class Vision(Plugin):
    name = "vision"  # the vision model reviews the playtest's screenshots against the design (vision.py)


# ---------------------------------------------------------------- phone

class PhoneUpdates(Plugin):
    name = "phone_updates"
    TEXT = {
        "plan_ready": lambda d: (f"Plan ready: {d.get('title', '')}", f"{d.get('tasks', 0)} tasks, {d.get('stack', '')}. "
                                 "Building now."),
        "feature_done": lambda d: ("Feature done", f"{d.get('feature', '')} ({d.get('done', 0)}/{d.get('total', 0)} tasks)"),
        "helper": lambda d: ("Asking Claude Code", f"Stuck on {d.get('task', '')}: {d.get('title', '')}"),
        "blocked": lambda d: ("Task blocked", f"{d.get('task', '')}: {d.get('title', '')}. Carrying on with the rest."),
        "playtest": lambda d: ("Playtest", d.get("detail", "")),
        "done": lambda d: ("Game ready", f"{d.get('title', '')}: {d.get('done', 0)}/{d.get('total', 0)} tasks done. "
                           f"{d.get('play', '')}"),
        "stopped": lambda d: ("Studio paused", d.get("reason", "")),
    }

    def __init__(self, send: Callable[[str, str], None] | None):
        self.send = send

    def on_event(self, event: str, project_name: str, **data) -> None:
        if self.send is None or event not in self.TEXT:
            return
        title, body = self.TEXT[event](data)
        try:
            self.send(f"{project_name}: {title}", body)
        except Exception:  # the phone is a nicety; never stop the build for it
            pass


ALL = ("ponytail", "test_first", "review", "playtest", "vision", "phone_updates")


def load(names: list[str], ponytail_level: str = "full", send: Callable[[str, str], None] | None = None) -> list[Plugin]:
    made = {"ponytail": lambda: Ponytail(ponytail_level), "test_first": TestFirst, "review": Review,
            "playtest": Playtest, "vision": Vision, "phone_updates": lambda: PhoneUpdates(send)}
    return [made[n]() for n in names if n in made]


def phone_sender(settings) -> Callable[[str, str], None] | None:
    from jarvis.notify.app import push_from

    push = push_from(settings)
    return push.send_text if push else None
