"""How a game is laid out, tested and smoke-run, per technology.

Each stack writes a small working skeleton first (scene manager, fixed-step loop, input as action
names, an autoplay smoke run that records which scenes it visited), so the agents extend something
that already runs instead of starting from an empty folder. Tests and smoke runs never open a window:
pygame uses SDL's dummy drivers; web games run their logic under node and the smoke page in headless
Edge/Chrome against a local server that only listens on 127.0.0.1.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from html import escape as _esc
from pathlib import Path
from typing import Callable

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TEST_TIMEOUT = 180
SMOKE_TIMEOUT = 90


@dataclass
class TestResult:
    ok: bool
    passed: int = 0
    failed: int = 0
    output: str = ""
    failing: list[str] = field(default_factory=list)   # "<test file name>::<test name>" for each failure

    def summary(self) -> str:
        return f"{self.passed} passed, {self.failed} failed" + ("" if self.ok else " (NOT OK)")


@dataclass
class SmokeResult:
    ok: bool
    scenes: list[str] = field(default_factory=list)
    frames: int = 0
    error: str = ""

    def summary(self) -> str:
        path = " -> ".join(self.scenes[:12]) or "no scenes"
        if not self.ok:
            return f"crashed: {self.error[-600:]}"
        return f"page check: sections {path}" if self.frames == 0 else f"ran {self.frames} frames: {path}"


def _run(argv: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        return -1, f"Stopped after {timeout} s (an endless loop or a game waiting for a window?).\n{out[-3000:]}"
    return done.returncode, (done.stdout or "") + ("\n" + done.stderr if done.stderr else "")


def tap_failures(out: str) -> list[str]:
    """Failing tests from node's TAP output, with the file each one is in (from its `location:` line)."""
    lines, fails = out.splitlines(), []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*not ok \d+ - (.+)$", line)
        if not m:
            continue
        loc = next((re.search(r"location: '(.+?):\d+:\d+'", ln) for ln in lines[i + 1:i + 15] if "location:" in ln), None)
        name = re.split(r"[\\/]+", loc.group(1))[-1] if loc else ""
        fails.append(f"{name}::{m.group(1).strip()}")
    return fails


def pytest_failures(out: str) -> list[str]:
    return [f"{re.split(r'[\\/]+', t.split('::')[0])[-1]}::{'::'.join(t.split('::')[1:])}"
            for t in re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)]


def _dedupe(path: list[str]) -> list[str]:
    out = []
    for s in path:
        if not out or out[-1] != s:
            out.append(s)
    return out


GAME_PLANNING = """- 8 to 20 tasks, ids "T1", "T2", ... in build order. The skeleton (scene manager, main loop, input, autoplay) already
  exists: tasks build on it. Pure logic first, then scenes, then drawing, then polish.
- scenes are lowercase names; the first scene is where the game starts; every transition uses listed scenes."""


class Stack:
    name = ""
    what = "game"                      # what the studio is making, in the prompts
    planning = GAME_PLANNING           # stack-specific planning rules for the planner
    test_dir = "tests"
    allowed_imports: set[str] = set()   # third-party modules the Ponytail check accepts
    conventions = ""

    def scaffold(self, root: Path, title: str) -> list[str]:
        written = []
        for rel, text in self.skeleton(title).items():
            path = root / rel
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(rel)
        return written

    def skeleton(self, title: str) -> dict[str, str]:
        raise NotImplementedError

    TITLE_LINES: dict[str, tuple[str, Callable[[str], str]]] = {}

    def set_title(self, root: Path, title: str) -> None:
        """Put the planned title into the skeleton's config (each file's own quoting)."""
        for rel, (pattern, line) in self.TITLE_LINES.items():
            path = root / rel
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                path.write_text(re.sub(pattern, lambda m: line(title), text, count=1, flags=re.M), encoding="utf-8")

    def test_file(self, task_id: str) -> str:
        raise NotImplementedError

    def syntax_error(self, path: Path) -> str | None:
        """Does this test file even parse? (The tester sometimes writes broken regexes, seen 8 Nov 2026.)"""
        if not path.is_file():
            return None
        if path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
            except SyntaxError as e:
                return f"line {e.lineno}: {e.msg}"
            return None
        node = shutil.which("node")
        if not node:
            return None
        code, out = _run([node, "--check", str(path)], path.parent, 30)
        return None if code == 0 else out.strip()[-600:]

    # what Rules.md and Architecture.md say about this stack
    tech: list[str] = []
    approved: list[str] = []
    prohibited: list[str] = []
    must: list[str] = []
    avoid: list[str] = []
    layout: dict[str, str] = {}
    config_file = ""

    def palette_code(self, palette: list[dict]) -> str:
        raise NotImplementedError

    def set_palette(self, root: Path, palette: list[dict]) -> None:
        """PALETTE in the game's config module, from design.md, so every scene uses the same colours."""
        path = root / self.config_file
        if not path.is_file() or not palette:
            return
        text = path.read_text(encoding="utf-8")
        block = self.palette_code(palette)
        text = re.sub(r"\n(?:#|//|/\*) palette \(design\.md\)[\s\S]*?(?:#|//|/\*) end palette(?: \*/)?\n", "\n", text)
        path.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")

    def run_tests(self, root: Path) -> TestResult:
        raise NotImplementedError

    def smoke(self, root: Path, frames: int = 900) -> SmokeResult:
        raise NotImplementedError

    def play_command(self, root: Path) -> str:
        raise NotImplementedError

    def available(self) -> str | None:
        """None if this stack can run here, else what's missing."""
        return None


# ---------------------------------------------------------------- pygame

PY_ENGINE = '''"""Engine skeleton made by Jarvis Studio: input as action names, scenes, the fixed-step Game, autoplay.
Nothing here imports pygame, so tests run without a window. Extend it; keep the Game/Scene/Input API."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

ACTIONS = ("left", "right", "up", "down", "jump", "attack", "kick", "block", "special", "confirm", "back", "pause")
DT = 1 / 60


@dataclass
class Input:
    """What the player (or the AI, or autoplay) wants this frame: actions held down, and newly pressed."""
    held: set = field(default_factory=set)
    pressed: set = field(default_factory=set)

    def __post_init__(self):
        self.held, self.pressed = set(self.held), set(self.pressed)


class Scene:
    """One screen. update() returns the name of the scene to switch to, or None to stay."""
    name = "scene"

    def __init__(self, game: "Game"):
        self.game = game

    def enter(self) -> None:
        pass

    def update(self, inp: Input, dt: float) -> str | None:
        return None

    def draw(self, surface) -> None:
        pass


class Game:
    def __init__(self, scenes: dict, first: str, data: dict | None = None):
        self.scene_classes = dict(scenes)
        self.data = data if data is not None else {}
        self.frame = 0
        self.trace: list[dict] = []
        self.scene_name: str | None = None
        self.scene: Scene | None = None
        self.switch(first)

    def switch(self, name: str) -> None:
        if name not in self.scene_classes:
            raise KeyError(f"no scene called {name!r}; known scenes: {sorted(self.scene_classes)}")
        self.scene_name = name
        self.scene = self.scene_classes[name](self)
        self.scene.enter()
        self.trace.append({"frame": self.frame, "scene": name})

    def step(self, inp: Input, dt: float = DT) -> str:
        nxt = self.scene.update(inp, dt)
        self.frame += 1
        if nxt and nxt != self.scene_name:
            self.switch(nxt)
        return self.scene_name

    def scene_path(self) -> list[str]:
        return [t["scene"] for t in self.trace]


class Autoplay:
    """Scripted input for the smoke run: confirm every so often, plus random moves and attacks."""

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.prev: set = set()

    def next(self, frame: int) -> Input:
        held = set()
        if frame % 45 < 2:
            held.add("confirm")
        if self.rng.random() < 0.6:
            held.add(self.rng.choice(("left", "right")))
        if self.rng.random() < 0.3:
            held.add(self.rng.choice(("attack", "kick", "block", "jump", "special", "up", "down")))
        inp = Input(held, held - self.prev)
        self.prev = held
        return inp
'''

PY_CONFIG = '''"""Every tunable number lives here."""

TITLE = {title!r}
WIDTH, HEIGHT = 960, 540
FPS = 60
'''

PY_SCENES = '''"""The game's screens. SCENES maps scene names to classes; FIRST is where the game starts.
Scenes may draw with pygame; game logic belongs in its own modules (no pygame there)."""

from __future__ import annotations

from game import config
from game.engine import Scene


class MenuScene(Scene):
    name = "menu"

    def update(self, inp, dt):
        if "confirm" in inp.pressed:
            return "play"
        return None

    def draw(self, surface):
        import pygame

        surface.fill((20, 24, 40))
        font = pygame.font.Font(None, 72)
        text = font.render(config.TITLE, True, (240, 240, 240))
        surface.blit(text, text.get_rect(center=(config.WIDTH // 2, config.HEIGHT // 2 - 40)))
        hint = pygame.font.Font(None, 32).render("Press Enter", True, (180, 180, 200))
        surface.blit(hint, hint.get_rect(center=(config.WIDTH // 2, config.HEIGHT // 2 + 40)))


class PlayScene(Scene):
    name = "play"

    def update(self, inp, dt):
        if "back" in inp.pressed:
            return "menu"
        return None

    def draw(self, surface):
        surface.fill((30, 30, 30))


SCENES = {"menu": MenuScene, "play": PlayScene}
FIRST = "menu"
'''

PY_MAIN = '''"""Play: python main.py
Smoke run (no window, scripted input): python main.py --autoplay --frames 900 --trace .studio/trace.json"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--autoplay", action="store_true")
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--trace")
    args = ap.parse_args(argv)
    if args.autoplay:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame

    from game import config
    from game.engine import DT, Autoplay, Game, Input
    from game.scenes import FIRST, SCENES

    keys = {pygame.K_LEFT: "left", pygame.K_a: "left", pygame.K_RIGHT: "right", pygame.K_d: "right",
            pygame.K_UP: "up", pygame.K_w: "up", pygame.K_DOWN: "down", pygame.K_s: "down",
            pygame.K_SPACE: "jump", pygame.K_j: "attack", pygame.K_k: "kick", pygame.K_l: "block",
            pygame.K_i: "special", pygame.K_RETURN: "confirm", pygame.K_ESCAPE: "back", pygame.K_p: "pause"}
    pygame.init()
    screen = pygame.display.set_mode((config.WIDTH, config.HEIGHT))
    pygame.display.set_caption(config.TITLE)
    clock = pygame.time.Clock()
    game = Game(SCENES, FIRST)
    auto = Autoplay() if args.autoplay else None
    prev: set = set()
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        if auto:
            inp = auto.next(game.frame)
        else:
            pressed_keys = pygame.key.get_pressed()
            held = {action for key, action in keys.items() if pressed_keys[key]}
            inp = Input(held, held - prev)
            prev = held
        game.step(inp, DT)
        game.scene.draw(screen)
        pygame.display.flip()
        if game.data.get("quit") or (auto and game.frame >= args.frames):
            running = False
        if not auto:
            clock.tick(config.FPS)
    if args.trace:
        Path(args.trace).parent.mkdir(parents=True, exist_ok=True)
        Path(args.trace).write_text(json.dumps({"frames": game.frame, "scenes": game.scene_path(),
                                                "trace": game.trace[-300:]}), encoding="utf-8")
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

PY_TEST = '''"""The skeleton's own tests (made by Jarvis Studio)."""

import pytest

from game.engine import Autoplay, Game, Input
from game.scenes import FIRST, SCENES


def test_menu_starts_and_confirm_goes_to_play():
    game = Game(SCENES, FIRST)
    assert game.scene_name == "menu"
    game.step(Input(pressed={"confirm"}, held={"confirm"}))
    assert game.scene_name == "play"


def test_unknown_scene_is_an_error():
    with pytest.raises(KeyError):
        Game(SCENES, "nowhere")


def test_autoplay_is_repeatable():
    a, b = Autoplay(seed=1), Autoplay(seed=1)
    assert [a.next(i).held for i in range(50)] == [b.next(i).held for i in range(50)]
'''


class PygameStack(Stack):
    name = "pygame"
    allowed_imports = {"pygame"}
    TITLE_LINES = {"game/config.py": (r"^TITLE = .*$", lambda t: f"TITLE = {t!r}")}
    config_file = "game/config.py"
    tech = ["Python 3.12", "pygame 2.6 (window, drawing, input, sound)", "pytest (tests, run headless with SDL's "
            "dummy drivers)", "git (one commit per finished task)"]
    approved = ["the Python standard library (math, random, json, dataclasses, enum, pathlib...)", "pygame",
                "pytest (tests only)"]
    prohibited = ["any other third-party package (numpy, pandas, requests, pymunk, arcade, pyglet, Pillow...)",
                  "network access of any kind", "downloaded images, sounds or fonts (draw and synthesise in code)"]
    must = ["Game logic lives in its own modules under game/ and never imports pygame; only scenes and drawing do.",
            "Every scene is a Scene subclass registered in game/scenes; update() returns the next scene name or None.",
            "Input arrives as an Input with held/pressed action names; logic never reads the keyboard or the clock.",
            "Every tunable number and the colour PALETTE live in game/config.py.",
            "Each task's tests in tests/test_<task id>.py; all tests and the automatic play-through must pass.",
            "Keep main.py's --autoplay --frames --trace smoke run working."]
    avoid = ["Editing tests to make them pass (tests change only when they contradict the task).",
             "time.sleep, real-time clocks or blocking loops in game logic.", "Global mutable state; use game.data.",
             "Placeholders, unused helpers, copy-pasted blocks (Ponytail rule).", "Reusing names, characters, story or "
             "art of an existing game."]
    layout = {"main.py": "window, keyboard -> Input, fixed 60 fps loop, smoke run", "game/engine.py":
              "Input, Scene, Game (scene manager), Autoplay", "game/config.py": "title, sizes, tuning numbers, PALETTE",
              "game/scenes/ or game/scenes.py": "one Scene per screen; SCENES and FIRST",
              "game/<logic>.py": "entities and rules (no pygame)", "tests/": "pytest files, one per task",
              "docs/": "PRD, Architecture, Rules, memory, design, tasks, project.html", ".studio/": "the studio's state, "
              "log and playtest reports"}

    def palette_code(self, palette: list[dict]) -> str:
        rgb = lambda h: tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))  # noqa: E731
        items = "\n".join(f"    {c['name'].strip().lower().replace(' ', '_')!r}: {rgb(c['hex'])},  # {c['use'][:60]}"
                          for c in palette)
        return f"# palette (design.md)\nPALETTE = {{\n{items}\n}}\n# end palette\n"
    conventions = """Technology: Python 3.12 + pygame 2.6. Already in the project (made by the studio, working):
- main.py: window, keyboard -> Input, fixed 60 fps loop, and the smoke run (--autoplay --frames --trace). Keep
  those flags working; change main.py only for wiring.
- game/engine.py: Input(held, pressed), Scene (enter/update/draw; update returns the next scene name or None),
  Game(SCENES, FIRST) with .step(inp), .scene_name, .data (shared dict), Autoplay. Don't rewrite it.
- game/config.py (TITLE, WIDTH, HEIGHT, FPS: put every tunable number here), game/scenes.py (SCENES dict and
  FIRST; menu and play placeholders to replace or extend).
Rules: game logic goes in its own modules under game/ and never imports pygame (only scenes and drawing do).
Tests: pytest files tests/test_<task id>.py with plain asserts, importing from game.*; no window, no
pygame.display or event queue in tests. Use run_tests to run all tests and run_smoke for the autoplay run."""

    def skeleton(self, title: str) -> dict[str, str]:
        return {"game/__init__.py": '"""The game package."""\n', "game/engine.py": PY_ENGINE,
                "game/config.py": PY_CONFIG.format(title=title), "game/scenes.py": PY_SCENES, "main.py": PY_MAIN,
                "tests/__init__.py": "", "tests/test_engine.py": PY_TEST,
                ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n.studio/trace.json\nsave.json\n"}

    def test_file(self, task_id: str) -> str:
        return f"tests/test_{task_id.lower()}.py"

    def _env(self) -> dict:
        return {**os.environ, "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "PYTHONDONTWRITEBYTECODE": "1",
                "PYGAME_HIDE_SUPPORT_PROMPT": "1", "PYTHONIOENCODING": "utf-8"}

    def run_tests(self, root: Path) -> TestResult:
        code, out = _run([sys.executable, "-m", "pytest", "-q", "--tb=short", "--no-header", "-p", "no:cacheprovider",
                          self.test_dir], root, TEST_TIMEOUT, self._env())
        passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
        failed = sum(int(n) for n in re.findall(r"(\d+) (?:failed|error)", out))
        return TestResult(code == 0 and passed > 0, passed, failed, out.strip()[-6000:], pytest_failures(out))

    def smoke(self, root: Path, frames: int = 900) -> SmokeResult:
        trace = root / ".studio" / "trace.json"
        trace.unlink(missing_ok=True)
        code, out = _run([sys.executable, "main.py", "--autoplay", "--frames", str(frames), "--trace", str(trace)],
                         root, SMOKE_TIMEOUT, self._env())
        if code != 0 or not trace.is_file():
            return SmokeResult(False, error=out.strip()[-3000:] or f"exit code {code}, no trace written")
        data = json.loads(trace.read_text(encoding="utf-8"))
        return SmokeResult(True, _dedupe(data.get("scenes", [])), int(data.get("frames", 0)))

    def play_command(self, root: Path) -> str:
        return f'cd "{root}"; python main.py'


# ---------------------------------------------------------------- web

JS_ENGINE = '''// Engine skeleton made by Jarvis Studio: input as action names, scenes, the fixed-step Game, autoplay.
// No DOM or canvas here, so `node --test` can run it. Extend it; keep the Game/Scene/Input API.

export const ACTIONS = ["left", "right", "up", "down", "jump", "attack", "kick", "block", "special", "confirm", "back", "pause"];
export const DT = 1 / 60;

export class Input {
  constructor(held = [], pressed = []) {
    this.held = new Set(held);
    this.pressed = new Set(pressed);
  }
}

export class Scene {
  constructor(game) { this.game = game; }
  enter() {}
  update(inp, dt) { return null; }   // return the next scene's name to switch
  draw(ctx) {}
}

export class Game {
  constructor(scenes, first, data = {}) {
    this.sceneClasses = { ...scenes };
    this.data = data;
    this.frame = 0;
    this.trace = [];
    this.sceneName = null;
    this.scene = null;
    this.switch(first);
  }
  switch(name) {
    if (!(name in this.sceneClasses)) throw new Error(`no scene called ${name}; known: ${Object.keys(this.sceneClasses)}`);
    this.sceneName = name;
    this.scene = new this.sceneClasses[name](this);
    this.scene.enter();
    this.trace.push({ frame: this.frame, scene: name });
  }
  step(inp, dt = DT) {
    const next = this.scene.update(inp, dt);
    this.frame += 1;
    if (next && next !== this.sceneName) this.switch(next);
    return this.sceneName;
  }
  scenePath() { return this.trace.map((t) => t.scene); }
}

// Seeded random numbers (mulberry32), so autoplay and AI are repeatable in tests.
export function seeded(seed = 7) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class Autoplay {
  constructor(seed = 7) { this.rand = seeded(seed); this.prev = new Set(); }
  next(frame) {
    const held = new Set();
    if (frame % 45 < 2) held.add("confirm");
    if (this.rand() < 0.6) held.add(this.rand() < 0.5 ? "left" : "right");
    if (this.rand() < 0.3) held.add(["attack", "kick", "block", "jump", "special", "up", "down"][Math.floor(this.rand() * 7)]);
    const pressed = [...held].filter((a) => !this.prev.has(a));
    this.prev = held;
    return new Input(held, pressed);
  }
}
'''

JS_CONFIG = '''// Every tunable number lives here.
export const TITLE = {title};
export const WIDTH = 960;
export const HEIGHT = 540;
'''

JS_SCENES = '''// The game's screens. SCENES maps names to classes; FIRST is where the game starts.
import { Scene } from "./engine.js";
import { TITLE, WIDTH, HEIGHT } from "./config.js";

export class MenuScene extends Scene {
  update(inp) { return inp.pressed.has("confirm") ? "play" : null; }
  draw(ctx) {
    ctx.fillStyle = "#141828"; ctx.fillRect(0, 0, WIDTH, HEIGHT);
    ctx.fillStyle = "#f0f0f0"; ctx.font = "64px sans-serif"; ctx.textAlign = "center";
    ctx.fillText(TITLE, WIDTH / 2, HEIGHT / 2 - 30);
    ctx.font = "28px sans-serif"; ctx.fillText("Tap or press Enter", WIDTH / 2, HEIGHT / 2 + 40);
  }
}

export class PlayScene extends Scene {
  update(inp) { return inp.pressed.has("back") ? "menu" : null; }
  draw(ctx) { ctx.fillStyle = "#1e1e1e"; ctx.fillRect(0, 0, WIDTH, HEIGHT); }
}

export const SCENES = { menu: MenuScene, play: PlayScene };
export const FIRST = "menu";
'''

JS_MAIN = '''// Browser entry: canvas, keyboard + touch buttons -> Input, fixed 60 fps updates.
import { Game, Input, DT } from "./engine.js";
import { SCENES, FIRST } from "./scenes.js";
import { WIDTH, HEIGHT } from "./config.js";

const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");
const KEYS = { ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right", ArrowUp: "up", w: "up", ArrowDown: "down",
  s: "down", " ": "jump", j: "attack", k: "kick", l: "block", i: "special", Enter: "confirm", Escape: "back", p: "pause" };
const held = new Set();
let prev = new Set();
addEventListener("keydown", (e) => { if (KEYS[e.key]) { held.add(KEYS[e.key]); e.preventDefault(); } });
addEventListener("keyup", (e) => { if (KEYS[e.key]) held.delete(KEYS[e.key]); });
for (const button of document.querySelectorAll("[data-action]")) {
  const action = button.dataset.action;
  button.addEventListener("pointerdown", (e) => { held.add(action); e.preventDefault(); });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) button.addEventListener(ev, () => held.delete(action));
}

function fit() {
  const scale = Math.min(innerWidth / WIDTH, innerHeight / HEIGHT);
  const dpr = devicePixelRatio || 1;
  canvas.style.width = `${WIDTH * scale}px`; canvas.style.height = `${HEIGHT * scale}px`;
  canvas.width = WIDTH * dpr; canvas.height = HEIGHT * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", fit);
fit();

const game = new Game(SCENES, FIRST);
let last = performance.now(), acc = 0;
function frame(now) {
  acc += Math.min(0.25, (now - last) / 1000);
  last = now;
  while (acc >= DT) {
    const pressed = [...held].filter((a) => !prev.has(a));
    game.step(new Input(held, pressed), DT);
    prev = new Set(held);
    acc -= DT;
  }
  game.scene.draw(ctx);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
'''

JS_INDEX = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #000; overflow: hidden; touch-action: none; }}
  body {{ display: grid; place-items: center; }}
  #pad {{ position: fixed; inset: auto 0 12px 0; display: flex; justify-content: space-between; padding: 0 16px; pointer-events: none; }}
  #pad div {{ display: flex; gap: 10px; pointer-events: auto; }}
  #pad button {{ width: 64px; height: 64px; border-radius: 50%; border: 2px solid #fff6; background: #fff2; color: #fff; font: 600 14px sans-serif; }}
  @media (hover: hover) {{ #pad {{ display: none; }} }}
</style>
</head>
<body>
<canvas id="game"></canvas>
<div id="pad">
  <div><button data-action="left">◀</button><button data-action="right">▶</button><button data-action="jump">▲</button></div>
  <div><button data-action="block">BLK</button><button data-action="kick">K</button><button data-action="attack">A</button><button data-action="confirm">OK</button></div>
</div>
<script type="module" src="src/main.js"></script>
</body>
</html>
'''

JS_SMOKE = '''<!doctype html>
<html><head><meta charset="utf-8"><title>smoke</title></head>
<body>
<pre id="result">pending</pre>
<script>
// Made by Jarvis Studio: runs the game headless with scripted input and writes what happened into #result.
const out = (data) => { document.getElementById("result").textContent = JSON.stringify(data); };
window.addEventListener("error", (e) => out({ ok: false, error: String(e.message || e) }));
const frames = Number(new URLSearchParams(location.search).get("frames") || 900);
Promise.all([import("./src/engine.js"), import("./src/scenes.js")]).then(([engine, scenes]) => {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 960; canvas.height = 540;
    const ctx = canvas.getContext("2d");
    const game = new engine.Game(scenes.SCENES, scenes.FIRST);
    const auto = new engine.Autoplay();
    while (game.frame < frames && !game.data.quit) {
      game.step(auto.next(game.frame), engine.DT);
      game.scene.draw(ctx);
    }
    out({ ok: true, frames: game.frame, scenes: game.scenePath() });
  } catch (err) {
    out({ ok: false, error: String(err && err.stack || err) });
  }
}).catch((err) => out({ ok: false, error: "could not load the game: " + String(err && err.message || err) }));
</script>
</body></html>
'''

JS_TEST = '''// The skeleton's own tests (made by Jarvis Studio). Run: node --test
import test from "node:test";
import assert from "node:assert/strict";
import { Game, Input, Autoplay } from "../src/engine.js";
import { SCENES, FIRST } from "../src/scenes.js";

test("menu starts and confirm goes to play", () => {
  const game = new Game(SCENES, FIRST);
  assert.equal(game.sceneName, "menu");
  game.step(new Input(["confirm"], ["confirm"]));
  assert.equal(game.sceneName, "play");
});

test("unknown scene is an error", () => {
  assert.throws(() => new Game(SCENES, "nowhere"));
});

test("autoplay is repeatable", () => {
  const a = new Autoplay(1), b = new Autoplay(1);
  for (let i = 0; i < 50; i++) assert.deepEqual([...a.next(i).held], [...b.next(i).held]);
});
'''


def _browser() -> str | None:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                      ".mjs": "text/javascript"}

    def log_message(self, *args):
        pass


class WebStack(Stack):
    name = "web"
    TITLE_LINES = {"src/config.js": (r"^export const TITLE = .*$", lambda t: f"export const TITLE = {json.dumps(t)};"),
                   "index.html": (r"<title>.*?</title>", lambda t: f"<title>{_esc(t)}</title>")}
    config_file = "src/config.js"
    tech = ["HTML5 canvas 2D", "JavaScript ES modules, no build step", "node 24 (node --test for logic tests)",
            "headless Edge/Chrome (automatic play-through)", "git (one commit per finished task)"]
    approved = ["browser built-ins (canvas, requestAnimationFrame, Web Audio, localStorage, pointer events)",
                "node:test and node:assert (tests only)"]
    prohibited = ["npm packages and frameworks (Phaser, PixiJS, Three.js, React...)", "CDN scripts",
                  "network calls", "downloaded images, sounds or fonts (draw and synthesise in code)"]
    must = ["Logic modules in src/ never touch document, window or the canvas; only scenes' draw(ctx) and main.js do.",
            "Imports are relative and end in .js.", "Every scene is registered in src/scenes.js (SCENES, FIRST).",
            "Every tunable number and the colour PALETTE live in src/config.js.",
            "Each task's tests in tests/<task id>.test.mjs; all tests and the play-through must pass.",
            "Touch buttons and keyboard both work."]
    avoid = ["Editing tests to make them pass.", "setTimeout/real-time clocks in game logic.", "Global mutable state.",
             "Placeholders, unused helpers, copy-pasted blocks (Ponytail rule).", "Reusing names, characters, story or "
             "art of an existing game."]
    layout = {"index.html": "canvas, touch buttons", "src/main.js": "input -> Input, fixed-step loop",
              "src/engine.js": "Input, Scene, Game, Autoplay, seeded random", "src/config.js": "title, sizes, tuning, "
              "PALETTE", "src/scenes.js": "SCENES and FIRST", "src/<logic>.js": "entities and rules (no DOM)",
              "tests/": "node:test files, one per task", "smoke.html": "headless play-through",
              "docs/": "PRD, Architecture, Rules, memory, design, tasks, project.html", ".studio/": "studio state"}

    def palette_code(self, palette: list[dict]) -> str:
        items = "\n".join(f"  {json.dumps(c['name'].strip().lower().replace(' ', '_'))}: {json.dumps(c['hex'])},"
                          f"  // {c['use'][:60]}" for c in palette)
        return f"// palette (design.md)\nexport const PALETTE = {{\n{items}\n}};\n// end palette\n"
    conventions = """Technology: an HTML5 canvas game in plain JavaScript ES modules, no npm packages, no build step.
Already in the project (made by the studio, working):
- index.html (canvas + touch buttons for phones) and src/main.js (keyboard/touch -> Input, fixed 60 fps loop).
- src/engine.js: Input(held, pressed) with Sets, Scene (enter/update/draw; update returns the next scene name
  or null), Game(SCENES, FIRST) with .step(inp), .sceneName, .data, Autoplay, seeded(seed) random.
- src/config.js (TITLE, WIDTH, HEIGHT: put every tunable number here), src/scenes.js (SCENES and FIRST).
- smoke.html runs the game headless; keep src/scenes.js exporting SCENES and FIRST.
Rules: logic modules in src/ never touch document/window/canvas (only scenes' draw(ctx) and main.js do).
Imports always use relative paths with the .js extension. Tests: tests/<task id>.test.mjs with node:test and
node:assert/strict, importing from "../src/<module>.js". Use run_tests for all tests and run_smoke."""

    def skeleton(self, title: str) -> dict[str, str]:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "game"
        return {"package.json": json.dumps({"name": slug, "private": True, "type": "module",
                                            "scripts": {"test": "node --test"}}, indent=2) + "\n",
                "index.html": JS_INDEX.format(title=title), "src/engine.js": JS_ENGINE,
                "src/config.js": JS_CONFIG.format(title=json.dumps(title)), "src/scenes.js": JS_SCENES,
                "src/main.js": JS_MAIN, "smoke.html": JS_SMOKE, "tests/engine.test.mjs": JS_TEST,
                ".gitignore": "node_modules/\n.studio/trace.json\n"}

    def test_file(self, task_id: str) -> str:
        return f"tests/{task_id.lower()}.test.mjs"

    def available(self) -> str | None:
        if not shutil.which("node"):
            return "node isn't installed"
        if not _browser():
            return "no Edge or Chrome for the smoke run"
        return None

    def run_tests(self, root: Path) -> TestResult:
        node = shutil.which("node")
        if not node:
            return TestResult(False, output="node isn't installed")
        code, out = _run([node, "--test", "--test-reporter=tap"], root, TEST_TIMEOUT)
        passed = sum(int(n) for n in re.findall(r"^# pass (\d+)", out, re.M))
        failed = sum(int(n) for n in re.findall(r"^# fail (\d+)", out, re.M))
        return TestResult(code == 0 and passed > 0, passed, failed, out.strip()[-6000:], tap_failures(out))

    def smoke(self, root: Path, frames: int = 900) -> SmokeResult:
        browser = _browser()
        if not browser:
            return SmokeResult(False, error="no Edge or Chrome for the smoke run")
        handler = functools.partial(_Quiet, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as profile:
                url = f"http://127.0.0.1:{server.server_address[1]}/smoke.html?frames={frames}"
                code, out = _run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                                  "--no-default-browser-check", "--disable-extensions", f"--user-data-dir={profile}",
                                  "--virtual-time-budget=20000", "--dump-dom", url], root, SMOKE_TIMEOUT)
        finally:
            server.shutdown()
            server.server_close()
        m = re.search(r'<pre id="result">(.*?)</pre>', out, re.S)
        if not m:
            return SmokeResult(False, error=f"the smoke page didn't report (exit {code}): {out[-1500:]}")
        import html as _html

        text = _html.unescape(m.group(1))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return SmokeResult(False, error=f"the smoke page reported: {text[:1500]}")
        if not data.get("ok"):
            return SmokeResult(False, error=str(data.get("error", "unknown error"))[-3000:])
        return SmokeResult(True, _dedupe(data.get("scenes", [])), int(data.get("frames", 0)))

    def play_command(self, root: Path) -> str:
        from jarvis.config import Settings

        local = f'cd "{root}"; python -m http.server 8000 --bind 127.0.0.1   then open http://127.0.0.1:8000'
        public = Settings.from_env().public_url
        return f"On your iPhone (Tailscale on): {public}/games/{root.name}/\nOn the PC: {local}" if public else local


STACK_CLASSES = {"pygame": PygameStack, "web": WebStack}
from jarvis.studio.site import SiteStack  # noqa: E402  (its own module: skeleton + page checker are long)

STACK_CLASSES["site"] = SiteStack


def get(name: str) -> Stack:
    return STACK_CLASSES[name]()
