"""Playtest: does the finished game actually play the way the PRD says?

Per-task tests check pieces on their own and the smoke run only checks that nothing crashes; Echo Duel
(7 Nov 2026) passed all of that while its fighters barely moved, no hit ever landed and two scenes could
not be reached. So after the build, a harness is written into the game's .studio/ folder and run there:

1. explore: from every scene reached so far, try simple input probes (confirm, back, pause, attacks, menu
   navigation, and letting autoplay run until the scene changes, e.g. a round timer) and map which scenes
   can really be reached, and how;
2. long play: autoplay for a few thousand frames, drawing each frame, recording per scene which numbers on
   the scene and in game.data ever change (health, positions, score...), how many distinct screens appear,
   which drawing calls are used (rectangles only? characters?), and a screenshot;
3. crashes anywhere, with the traceback.

Python turns the certain failures (crash, unreachable planned scene, frozen screen) into fix tasks; the
planner model reads the report against the PRD and adds what's evidently wrong (e.g. "health never drops:
hits never land", "everything is drawn as rectangles but the PRD wants drawn characters"). The fix tasks
go through the normal build loop, then the playtest runs again (up to Budget.playtests rounds).
"""

from __future__ import annotations

import base64
import functools
import html
import http.server
import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.studio.project import State, Task

NO_WINDOW = __import__("subprocess").CREATE_NO_WINDOW if os.name == "nt" else 0
FRAMES = 4000
TIMEOUT = 240

# ---------------------------------------------------------------- the harnesses

PY_HARNESS = r'''"""Jarvis Studio playtest harness (written by the studio; safe to delete)."""
import argparse, hashlib, json, os, sys, time, traceback

sys.path.insert(0, os.getcwd())
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--shots", required=True)
ap.add_argument("--frames", type=int, default=4000)
args = ap.parse_args()

CURRENT = ["?"]
DRAWS = {}
def _wrap(name):
    fn = getattr(pygame.draw, name)
    def counted(*a, **k):
        d = DRAWS.setdefault(CURRENT[0], {})
        d[name] = d.get(name, 0) + 1
        return fn(*a, **k)
    setattr(pygame.draw, name, counted)
for _n in ("rect", "circle", "ellipse", "line", "lines", "aaline", "aalines", "polygon", "arc"):
    if hasattr(pygame.draw, _n):
        _wrap(_n)

from game.engine import Autoplay, Game, Input  # after the wrapping, so "from pygame.draw import rect" is counted
from game.scenes import FIRST, SCENES
try:
    from game import config
    W, H = int(config.WIDTH), int(config.HEIGHT)
except Exception:
    W, H = 960, 540

PROBES = [["confirm"], ["back"], ["pause"], ["attack"], ["block"], ["jump"], ["kick"], ["special"],
          ["down", "confirm"], ["up", "confirm"], ["right", "confirm"], ["left", "confirm"],
          ["down", "down", "confirm"], ["AUTO"]]
DEADLINE = time.time() + 90
report = {"explore": {}, "run": {}, "crashes": []}


def idle(game, n):
    for _ in range(n):
        game.step(Input())


def probe(game, steps):
    start = game.scene_name
    for a in steps:
        if a == "AUTO":
            auto = Autoplay(seed=11)
            for _ in range(5000):
                game.step(auto.next(game.frame))
                if game.scene_name != start:
                    break
        else:
            game.step(Input(held={a}, pressed={a}))
            idle(game, 15)


def replay(path):
    game = Game(SCENES, FIRST)
    for steps in path:
        probe(game, steps)
    return game


found, edges, queue = {FIRST: []}, [], [FIRST]
while queue and time.time() < DEADLINE:
    scene = queue.pop(0)
    for steps in PROBES:
        if time.time() > DEADLINE:
            break
        try:
            game = replay(found[scene] + [steps])
        except Exception:
            report["crashes"].append({"where": "explore", "scene": scene, "probe": "+".join(steps),
                                      "error": traceback.format_exc()[-1800:]})
            continue
        to = game.scene_name
        if to != scene and not any(e[0] == scene and e[1] == to for e in edges):
            edges.append([scene, to, "+".join(steps)])
        if to not in found and len(found[scene]) < 5:
            found[to] = found[scene] + [steps]
            queue.append(to)
report["explore"] = {"reachable": sorted(found), "edges": edges,
                     "paths": {s: " > ".join("+".join(p) for p in path) or "(start)" for s, path in found.items()}}


def numbers(obj, prefix, out, depth=0, seen=None):
    seen = set() if seen is None else seen
    if depth > 2 or len(out) > 300 or id(obj) in seen:
        return out
    seen.add(id(obj))
    if isinstance(obj, dict):
        items = list(obj.items())
    elif hasattr(obj, "__dict__"):
        items = list(vars(obj).items())
    else:
        return out
    for k, v in items:
        k = str(k)
        if k.startswith("_") or k in ("game", "scene_classes", "trace"):
            continue
        name = prefix + k
        if isinstance(v, bool):
            out[name] = int(v)
        elif isinstance(v, (int, float)):
            out[name] = round(float(v), 3)
        elif isinstance(v, dict) or (hasattr(v, "__dict__") and not isinstance(v, type)
                                     and type(v).__module__.split(".")[0] == "game"):
            numbers(v, name + ".", out, depth + 1, seen)
    return out


pygame.init()
screen = pygame.display.set_mode((W, H))
os.makedirs(args.shots, exist_ok=True)
stats, shots = {}, {}
game, auto = Game(SCENES, FIRST), Autoplay(seed=5)
try:
    for f in range(args.frames):
        CURRENT[0] = game.scene_name
        game.step(auto.next(game.frame))
        scene = CURRENT[0] = game.scene_name
        game.scene.draw(screen)
        st = stats.setdefault(scene, {"frames": 0, "screens": set(), "samples": 0, "values": {}})
        st["frames"] += 1
        if st["frames"] % 15 == 1:
            st["samples"] += 1
            st["screens"].add(hashlib.md5(pygame.image.tobytes(screen, "RGB")).hexdigest())
            seen = set()  # game.data first, so a scene's reference to it isn't counted twice
            vals = numbers(game.data, "data.", {}, 0, seen)
            numbers(game.scene, "scene.", vals, 0, seen)
            for k, v in vals.items():
                rec = st["values"].setdefault(k, {"first": v, "min": v, "max": v, "seen": set()})
                rec["min"], rec["max"] = min(rec["min"], v), max(rec["max"], v)
                if len(rec["seen"]) < 50:
                    rec["seen"].add(v)
        if scene not in shots or st["frames"] == 240:
            path = os.path.join(args.shots, scene + ".png")
            pygame.image.save(screen, path)
            shots[scene] = path
except Exception:
    report["crashes"].append({"where": "long play", "scene": game.scene_name, "probe": "autoplay",
                              "error": traceback.format_exc()[-1800:]})
run = {}
for scene, st in stats.items():
    changed = {k: [r["min"], r["max"], len(r["seen"])] for k, r in st["values"].items() if len(r["seen"]) > 1}
    unchanged = sorted(k for k, r in st["values"].items() if len(r["seen"]) <= 1)
    run[scene] = {"frames": st["frames"], "samples": st["samples"], "distinct_screens": len(st["screens"]),
                  "draws_per_frame": {k: round(v / max(1, st["frames"]), 1) for k, v in DRAWS.get(scene, {}).items()},
                  "changed": changed, "unchanged": unchanged}
report["run"] = {"frames": game.frame, "path": [t["scene"] for t in game.trace][:60], "scenes": run}
report["shots"] = shots
with open(args.out, "w", encoding="utf-8") as fh:
    json.dump(report, fh)
pygame.quit()
'''

JS_HARNESS = r'''<!doctype html>
<html><head><meta charset="utf-8"><title>playtest</title></head><body>
<pre id="result">pending</pre>
<script>
// Jarvis Studio playtest harness (written by the studio; safe to delete).
const out = (d) => { document.getElementById("result").textContent = JSON.stringify(d); };
const FRAMES = Number(new URLSearchParams(location.search).get("frames") || 4000);
const CURRENT = ["?"], DRAWS = {};
const P = CanvasRenderingContext2D.prototype;
for (const n of ["fillRect", "strokeRect", "arc", "ellipse", "lineTo", "bezierCurveTo", "quadraticCurveTo", "fillText", "drawImage"]) {
  const fn = P[n];
  P[n] = function (...a) { const d = DRAWS[CURRENT[0]] ||= {}; d[n] = (d[n] || 0) + 1; return fn.apply(this, a); };
}
const PROBES = [["confirm"], ["back"], ["pause"], ["attack"], ["block"], ["jump"], ["kick"], ["special"],
  ["down", "confirm"], ["up", "confirm"], ["right", "confirm"], ["left", "confirm"], ["down", "down", "confirm"], ["AUTO"]];
Promise.all([import("../src/engine.js"), import("../src/scenes.js")]).then(([E, S]) => {
  const report = { explore: {}, run: {}, crashes: [], shots: {} };
  const idle = (g, n) => { for (let i = 0; i < n; i++) g.step(new E.Input()); };
  const probe = (g, steps) => {
    const start = g.sceneName;
    for (const a of steps) {
      if (a === "AUTO") { const au = new E.Autoplay(11); for (let i = 0; i < 5000; i++) { g.step(au.next(g.frame)); if (g.sceneName !== start) break; } }
      else { g.step(new E.Input([a], [a])); idle(g, 15); }
    }
  };
  const replay = (path) => { const g = new E.Game(S.SCENES, S.FIRST); for (const s of path) probe(g, s); return g; };
  const found = { [S.FIRST]: [] }, edges = [], queue = [S.FIRST], deadline = Date.now() + 60000;
  while (queue.length && Date.now() < deadline) {
    const scene = queue.shift();
    for (const steps of PROBES) {
      let g;
      try { g = replay(found[scene].concat([steps])); }
      catch (err) { report.crashes.push({ where: "explore", scene, probe: steps.join("+"), error: String(err && err.stack || err).slice(-1800) }); continue; }
      const to = g.sceneName;
      if (to !== scene && !edges.some((e) => e[0] === scene && e[1] === to)) edges.push([scene, to, steps.join("+")]);
      if (!(to in found) && found[scene].length < 5) { found[to] = found[scene].concat([steps]); queue.push(to); }
    }
  }
  report.explore = { reachable: Object.keys(found).sort(), edges,
    paths: Object.fromEntries(Object.entries(found).map(([s, p]) => [s, p.map((x) => x.join("+")).join(" > ") || "(start)"])) };
  const numbers = (obj, prefix, outv, depth = 0, seen = new Set()) => {
    if (depth > 2 || Object.keys(outv).length > 300 || !obj || typeof obj !== "object" || seen.has(obj)) return outv;
    seen.add(obj);
    for (const [k, v] of Object.entries(obj)) {
      if (k.startsWith("_") || k === "game" || k === "sceneClasses" || k === "trace") continue;
      const name = prefix + k;
      if (typeof v === "boolean") outv[name] = v ? 1 : 0;
      else if (typeof v === "number" && isFinite(v)) outv[name] = Math.round(v * 1000) / 1000;
      else if (v && typeof v === "object" && !Array.isArray(v) && !(v instanceof Set) && !(v instanceof Map)) numbers(v, name + ".", outv, depth + 1, seen);
    }
    return outv;
  };
  const canvas = document.createElement("canvas"); canvas.width = 960; canvas.height = 540;
  const ctx = canvas.getContext("2d"), small = document.createElement("canvas"); small.width = 320; small.height = 180;
  const stats = {}, game = new E.Game(S.SCENES, S.FIRST), au = new E.Autoplay(5);
  try {
    for (let f = 0; f < FRAMES; f++) {
      CURRENT[0] = game.sceneName;
      game.step(au.next(game.frame));
      const scene = CURRENT[0] = game.sceneName;
      game.scene.draw(ctx);
      const st = stats[scene] ||= { frames: 0, screens: new Set(), samples: 0, values: {} };
      st.frames += 1;
      if (st.frames % 15 === 1) {
        st.samples += 1;
        st.screens.add(canvas.toDataURL().length + ":" + ctx.getImageData(0, 0, 960, 540).data.reduce((a, b, i) => (i % 97 ? a : (a * 31 + b) | 0), 7));
        const seen = new Set(), vals = numbers(game.data, "data.", {}, 0, seen); numbers(game.scene, "scene.", vals, 0, seen);
        for (const [k, v] of Object.entries(vals)) {
          const r = st.values[k] ||= { min: v, max: v, seen: new Set() };
          r.min = Math.min(r.min, v); r.max = Math.max(r.max, v); if (r.seen.size < 50) r.seen.add(v);
        }
      }
      if (!(scene in report.shots) || st.frames === 240) {
        CURRENT[0] = "__harness__";  // the screenshot's own drawImage isn't the game's drawing
        small.getContext("2d").drawImage(canvas, 0, 0, 320, 180);
        report.shots[scene] = small.toDataURL("image/png");
      }
    }
  } catch (err) { report.crashes.push({ where: "long play", scene: game.sceneName, probe: "autoplay", error: String(err && err.stack || err).slice(-1800) }); }
  const run = {};
  for (const [scene, st] of Object.entries(stats)) {
    const changed = {}, unchanged = [];
    for (const [k, r] of Object.entries(st.values)) { if (r.seen.size > 1) changed[k] = [r.min, r.max, r.seen.size]; else unchanged.push(k); }
    const per = {}; for (const [k, v] of Object.entries(DRAWS[scene] || {})) per[k] = Math.round(10 * v / Math.max(1, st.frames)) / 10;
    run[scene] = { frames: st.frames, samples: st.samples, distinct_screens: st.screens.size, draws_per_frame: per, changed, unchanged: unchanged.sort() };
  }
  report.run = { frames: game.frame, path: game.trace.map((t) => t.scene).slice(0, 60), scenes: run };
  out(report);
}).catch((err) => out({ crashes: [{ where: "load", scene: "?", probe: "-", error: String(err && err.stack || err) }] }));
</script></body></html>
'''


@dataclass
class Report:
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""


def run(root: Path, stack, frames: int = FRAMES) -> Report:
    play = root / ".studio" / "playtest"
    play.mkdir(parents=True, exist_ok=True)
    if stack.name == "web":
        return _run_web(root, play, frames)
    if stack.name == "site":
        return _run_site(root, play, stack)
    harness = root / ".studio" / "playtest_harness.py"
    harness.write_text(PY_HARNESS, encoding="utf-8")
    out = play / "report.json"
    out.unlink(missing_ok=True)
    env = {**os.environ, "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "PYTHONIOENCODING": "utf-8",
           "PYGAME_HIDE_SUPPORT_PROMPT": "1"}
    import subprocess

    try:
        done = subprocess.run([sys.executable, str(harness), "--out", str(out), "--shots", str(play), "--frames",
                               str(frames)], cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT, env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return Report(False, error=f"the playtest didn't finish within {TIMEOUT} s (an endless loop?)")
    if not out.is_file():
        return Report(False, error=(done.stderr or done.stdout or f"exit code {done.returncode}")[-3000:])
    return Report(True, json.loads(out.read_text(encoding="utf-8")))


def _run_web(root: Path, play: Path, frames: int) -> Report:
    from jarvis.studio.stacks import SMOKE_TIMEOUT, _browser, _Quiet, _run

    (root / ".studio" / "playtest.html").write_text(JS_HARNESS, encoding="utf-8")
    browser = _browser()
    if not browser:
        return Report(False, error="no Edge or Chrome for the playtest")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as profile:
            url = f"http://127.0.0.1:{server.server_address[1]}/.studio/playtest.html?frames={frames}"
            code, out = _run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                              "--disable-extensions", f"--user-data-dir={profile}", "--virtual-time-budget=120000",
                              "--dump-dom", url], root, max(SMOKE_TIMEOUT, TIMEOUT))
    finally:
        server.shutdown()
        server.server_close()
    m = re.search(r'<pre id="result">(.*?)</pre>', out, re.S)
    try:
        data = json.loads(html.unescape(m.group(1))) if m else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return Report(False, error=f"the playtest page didn't report (exit {code}): {out[-1500:]}")
    for scene, uri in list(data.get("shots", {}).items()):  # data: URIs -> files next to the report
        if isinstance(uri, str) and uri.startswith("data:image/png;base64,") and re.fullmatch(r"[\w-]+", scene):
            path = play / f"{scene}.png"
            path.write_bytes(base64.b64decode(uri.split(",", 1)[1]))
            data["shots"][scene] = str(path)
    (play / "report.json").write_text(json.dumps(data), encoding="utf-8")
    return Report(True, data)


# ---------------------------------------------------------------- reading the report

_KEY = re.compile(r"(health|hp|life|lives|score|coin|money|gold|hit|damage|stamina|energy|level|wave|ammo|\.x$|\.y$|"
                  r"pos|speed|vel)", re.I)


def _rank(name: str) -> tuple[int, str]:
    """Gameplay values first (health, score, position...), so the cut-off never hides them."""
    return (0 if _KEY.search(name) else 1, name)


def _run_site(root: Path, play: Path, stack) -> Report:
    """Websites: the page check at desktop and phone sizes, plus screenshots, in the same report shape."""
    data = stack.check(root)
    if "viewports" not in data:
        return Report(False, error=str(data.get("error", "the page check failed"))[-3000:])
    shots = stack.screenshots(root, play)
    views = {v["name"]: v for v in data["viewports"]}
    sections = views.get("desktop", data["viewports"][0]).get("sections", [])
    crashes = [{"where": "page", "scene": v["name"], "probe": "loading and scrolling the page", "error": e}
               for v in data["viewports"] for e in v.get("errors", [])]
    report = {"kind": "site", "explore": {"reachable": sections, "edges": [], "paths": {}},
              "run": {"frames": 0, "path": sections, "scenes": views}, "crashes": crashes,
              "shots": {k: v for k, v in shots.items() if k in ("desktop", "phone")},
              "overview": shots.get("overview", "")}
    (play / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return Report(True, report)


def _site_summary(d: dict) -> str:
    lines = [f"Sections on the page: {', '.join(d['explore'].get('reachable', [])) or 'none'}"]
    for name, v in d["run"]["scenes"].items():
        lines.append(f"{name} ({v.get('width')} px wide, page {v.get('height')} px tall): {len(v.get('errors', []))} "
                     f"script errors, {v.get('keyframes', 0)} @keyframes, {v.get('transitions', 0)} elements with "
                     f"transitions, {v.get('animated', 0)} running animations, {v.get('reveal_total', 0)} reveal "
                     f"elements, sideways overflow: {'YES (' + str(v.get('scroll_width')) + ' px)' if v.get('overflow') else 'no'}, "
                     f"images without alt: {v.get('images_without_alt', 0)}, broken anchors: "
                     f"{', '.join(v.get('broken_anchors', [])) or 'none'}, external resources: "
                     f"{', '.join(v.get('external', [])[:3]) or 'none'}, reduced-motion rule: "
                     f"{'yes' if v.get('reduced_motion') else 'NO'}.")
    for c in d.get("crashes", [])[:5]:
        lines.append(f"SCRIPT ERROR ({c['scene']}): {c['error'][:300]}")
    return "\n".join(lines)


def _site_problems(d: dict, plan: dict) -> list[dict]:
    problems, seen = [], set()
    for c in d.get("crashes", []):
        if c["error"][:120] in seen:
            continue
        seen.add(c["error"][:120])
        problems.append({"title": "Fix the script error on the page", "scene": c["scene"], "kind": "code",
                         "evidence": c["error"][:600], "acceptance": ["the page loads and scrolls with no script errors"]})
    views = d["run"]["scenes"]
    have = set(d["explore"].get("reachable", [])) | {i for v in views.values() for i in v.get("ids", [])}
    for section in plan.get("scenes", []):
        if section not in have:
            problems.append({"title": f"Add the '{section}' section", "scene": section, "kind": "code",
                             "evidence": f"No element with id=\"{section}\" on the page.",
                             "acceptance": [f"index.html has <section id=\"{section}\"> inside <main> with its planned "
                                            "content", f"the nav links to #{section}"]})
    phone = views.get("phone", {})
    if phone.get("overflow"):
        problems.append({"title": "Make the page fit a phone screen", "scene": "phone", "kind": "code",
                         "evidence": f"At 390 px the page is {phone.get('scroll_width')} px wide (sideways scrolling).",
                         "acceptance": ["at 390 px wide nothing is wider than the screen", "the layout stacks into one column"]})
    desk = views.get("desktop", phone)
    if not desk.get("keyframes") and not desk.get("transitions"):
        problems.append({"title": "Add the planned animations", "scene": "page", "kind": "code",
                         "evidence": "No @keyframes and no transitions on the page.",
                         "acceptance": ["the hero has an entrance animation (@keyframes)", "sections reveal on scroll"]})
    if not desk.get("reduced_motion"):
        problems.append({"title": "Respect reduced motion", "scene": "page", "kind": "code",
                         "evidence": "No prefers-reduced-motion rule.",
                         "acceptance": ["styles.css has @media (prefers-reduced-motion: reduce) turning motion off"]})
    for key, title, crit in (("broken_anchors", "Fix links to sections that don't exist", "every #link has a matching id"),
                             ("external", "Remove external resources", "the page loads nothing from other sites")):
        found = sorted({x for v in views.values() for x in v.get(key, [])})
        if found:
            problems.append({"title": title, "scene": "page", "kind": "code", "evidence": ", ".join(found)[:300],
                             "acceptance": [crit]})
    if any(v.get("images_without_alt") for v in views.values()):
        problems.append({"title": "Give every image alt text", "scene": "page", "kind": "code",
                         "evidence": "Some <img> elements have no alt.", "acceptance": ["every <img> has alt text"]})
    return problems


def summary(report: Report, limit: int = 25) -> str:
    """The report in a few hundred words, for the planner model (and the log)."""
    if not report.ok:
        return f"The playtest itself failed: {report.error[-1500:]}"
    d = report.data
    if d.get("kind") == "site":
        return _site_summary(d)
    ex, run_ = d.get("explore", {}), d.get("run", {})
    lines = [f"Reachable scenes from the start: {', '.join(ex.get('reachable', []))}",
             "How each was reached: " + "; ".join(f"{s}: {p}" for s, p in ex.get("paths", {}).items()),
             f"Long autoplay ({run_.get('frames', 0)} frames) went through: {' -> '.join(run_.get('path', [])[:20])}"]
    for scene, st in run_.get("scenes", {}).items():
        changed = ", ".join(f"{k} {v[0]}..{v[1]}" for k, v in
                            sorted(st.get("changed", {}).items(), key=lambda kv: _rank(kv[0]))[:limit]) or "none"
        unchanged = ", ".join(sorted(st.get("unchanged", []), key=_rank)[:limit]) or "none"
        draws = ", ".join(f"{k} {v}" for k, v in st.get("draws_per_frame", {}).items()) or "no shape calls (text/images only)"
        lines.append(f"Scene '{scene}': {st.get('frames')} frames, {st.get('distinct_screens')} different screens in "
                     f"{st.get('samples')} samples. Values that changed: {changed}. Values that NEVER changed: "
                     f"{unchanged}. Drawing calls per frame: {draws}.")
    for c in d.get("crashes", [])[:5]:
        lines.append(f"CRASH in {c.get('where')} (scene {c.get('scene')}, input {c.get('probe')}): "
                     f"{c.get('error', '')[-600:]}")
    return "\n".join(lines)


def certain_problems(report: Report, plan: dict) -> list[dict]:
    """Failures that need no judgement: crashes, planned scenes that can't be reached, frozen screens."""
    if not report.ok:
        return [{"title": "Make the playtest run: the game fails under automatic play",
                 "scene": "", "evidence": report.error[-800:], "kind": "code",
                 "acceptance": ["the game runs under automatic input for thousands of frames without an error",
                                "all tests pass"]}]
    d, problems = report.data, []
    if d.get("kind") == "site":
        return _site_problems(d, plan)
    seen_errors = set()
    for c in d.get("crashes", []):
        key = (c.get("scene"), c.get("error", "")[-200:])
        if key in seen_errors:
            continue
        seen_errors.add(key)
        problems.append({"title": f"Fix the crash in scene '{c.get('scene')}' (input: {c.get('probe')})",
                         "scene": c.get("scene", ""), "evidence": c.get("error", "")[-800:], "kind": "code",
                         "acceptance": [f"pressing {c.get('probe')} in the '{c.get('scene')}' scene no longer raises "
                                        "an error", "all tests pass"]})
    reachable = set(d.get("explore", {}).get("reachable", []))
    planned = {t.get("to", "").lower(): t for t in plan.get("transitions", [])}
    for scene in plan.get("scenes", []):
        if scene not in reachable:
            how = planned.get(scene)
            hint = f" The plan says: {how['from']} -> {scene} on {how['trigger']}." if how else ""
            problems.append({"title": f"Make the '{scene}' scene reachable", "scene": scene, "kind": "code",
                             "evidence": f"Automatic exploration (confirm, back, pause, attack, menu navigation, "
                                         f"waiting for timers) never reached '{scene}'.{hint}",
                             "acceptance": [f"starting from the first scene, the game reaches '{scene}' with the "
                                            f"normal controls{(' (' + how['trigger'] + ' in ' + how['from'] + ')') if how else ''}",
                                            f"'{scene}' can be left again to another scene"]})
    for scene, st in d.get("run", {}).get("scenes", {}).items():
        if st.get("samples", 0) >= 10 and st.get("distinct_screens", 2) <= 1:
            problems.append({"title": f"Make the '{scene}' scene respond on screen", "scene": scene, "kind": "code",
                             "evidence": f"{st['samples']} samples during autoplay all showed the identical screen.",
                             "acceptance": [f"while keys are pressed in '{scene}', what's drawn changes"]})
    return problems


PROBLEMS_SCHEMA = {"type": "object", "properties": {"problems": {"type": "array", "items": {
    "type": "object", "properties": {
        "title": {"type": "string"}, "scene": {"type": "string"}, "evidence": {"type": "string"},
        "kind": {"type": "string", "enum": ["code", "visual"]},
        "acceptance": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "scene", "evidence", "kind", "acceptance"]}}}, "required": ["problems"]}

ANALYST = """You are the playtester of Jarvis Studio. A game was built from the PRD below and then played automatically.
Compare the playtest report with what the PRD promises and list what is evidently broken or missing for a player.
Look especially at: values that NEVER change but must for the core loop to work (health that never drops means hits
never land; a position that never changes means characters can't move; coins/score always 0), scenes or features
the PRD promises that the report shows no sign of, and drawing that doesn't match the art style (only rectangles
when the PRD describes drawn characters -> a "visual" problem).
Rules: only problems the report gives evidence for; at most {limit}, most important first; skip anything already in
"Already filed". Each problem has a short fix-task title, the scene, the evidence (cite the report), kind "code" or
"visual", and 2-3 acceptance criteria a test can check without a window ("after holding right for 60 frames in play,
the player's x is larger"; for visual ones, what the drawing must contain). If the game plays as the PRD describes,
return an empty list. Answer in JSON."""


def analyse(state: State, report: Report, team, filed: list[dict], limit: int = 4) -> list[dict]:
    p = state.plan
    prd = (f"Title: {p.get('title')}\nPitch: {p.get('pitch')}\nCore loop: {' / '.join(p.get('core_loop', []))}\n"
           f"Features: {'; '.join(f['name'] + ': ' + f['description'] for f in p.get('features', []))}\n"
           f"Scenes: {', '.join(p.get('scenes', []))}\nTransitions: "
           f"{'; '.join(t['from'] + ' -> ' + t['to'] + ' (' + t['trigger'] + ')' for t in p.get('transitions', []))}\n"
           f"Controls: {', '.join(c['action'] + '=' + c['keys'] for c in p.get('controls', []))}\n"
           f"Art style: {p.get('art_style')}")
    ask = (f"PRD:\n{prd}\n\nPlaytest report:\n{summary(report)}\n\nAlready filed:\n"
           + ("\n".join(f"- {f['title']}" for f in filed) or "(nothing)"))
    out = team.think([{"role": "system", "content": ANALYST.format(limit=limit)},
                             {"role": "user", "content": ask}], PROBLEMS_SCHEMA)
    good: list[dict] = []
    for pr in out.get("problems", []):
        if not (isinstance(pr, dict) and pr.get("title", "").strip() and pr.get("acceptance")):
            continue
        if _duplicate(pr["title"], filed + good):  # seen live: it re-filed "shop and pause not reachable"
            continue
        pr["kind"] = "visual" if pr.get("kind") == "visual" else "code"
        good.append(pr)
    return good[:limit]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3} - {"make", "scene", "scenes", "never", "with"}


def _duplicate(title: str, filed: list[dict]) -> bool:
    words = _words(title)
    for f in filed:
        other = _words(f["title"])
        if words and other and len(words & other) / len(words | other) >= 0.5:
            return True
        if "reach" in title.lower() and "reach" in f["title"].lower():
            return True
    return False


def tasks_for(problems: list[dict], state: State) -> list[Task]:
    """Fix tasks P1, P2, ... (numbered across playtest rounds), feature PT."""
    taken = {t.id for t in state.tasks}
    n, out = 1, []
    for pr in problems:
        while f"P{n}" in taken:
            n += 1
        tid = f"P{n}"
        taken.add(tid)
        out.append(Task(tid, pr["title"][:120], feature="PT", acceptance=[a for a in pr["acceptance"] if a.strip()][:4],
                        kind=pr.get("kind", "code"), notes=[f"playtest: {pr.get('evidence', '')[:400]}"]))
    return out
