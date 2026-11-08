"""The studio's agents. Each one is the coder's tool loop (jarvis/coder/agent.py) with its own system
prompt, tools and write rules; the planner and reviewer answer in JSON instead.

    researcher  qwen3      web search + pages about the reference -> docs/reference.md
    planner     qwen3      JSON plan (PRD, features, scenes, modules, tasks), checked in Python
    tester      coder model writes ONE task's tests before the code (it may write only that test file)
    coder       coder model makes the tests and the smoke run pass (it may not touch tests or docs)
    reviewer    qwen3      approve / list problems for a finished task, JSON
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jarvis.coder import agent as loop
from jarvis.coder.tools import SPECS as CODER_SPECS
from jarvis.coder.tools import Workspace
from jarvis.skills import select as skills_for
from jarvis.studio.project import (DESIGN_SCHEMA, PLAN_SCHEMA, SPLIT_SCHEMA, TESTCHECK_SCHEMA, State, Task,
                                   validate_plan)

REVIEW_SCHEMA = {"type": "object", "properties": {
    "approve": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}},
    "required": ["approve", "problems"]}


class PlanError(RuntimeError):
    pass


@dataclass
class Team:
    planner: object            # chat + json: research writing, review (qwen3:8b, shared with Jarvis)
    coder: object              # chat: tester and coder tool loops
    say: Callable[[str], None] = print
    plugins: list = field(default_factory=list)
    thinker: object = None     # rare heavy decisions with thinking on; None = the planner (STUDIO_THINKER_MODEL)
    vision: object = None      # the vision model for screenshot reviews (plugin "vision"), or None

    def rules(self, role: str) -> str:
        return "".join(p.rules(role) for p in self.plugins)

    def think(self, messages: list[dict], schema: dict) -> dict:
        """A decision (plan, design, split, test check, playtest analysis): the thinker model with Qwen3's
        thinking on. Used only at these rare points, so a bigger thinker swaps in seldom ("one-shot
        planning, sticky execution"); the tool loop and Jarvis stay on the fast pinned model."""
        model = self.thinker or self.planner
        try:
            return model.json(messages, schema, think=True)
        except TypeError:  # a model object without the thinking switch (tests' fakes)
            return model.json(messages, schema)


@dataclass
class StudioWorkspace(Workspace):
    stack: object = None
    smoke_frames: int = 600

    def run_tests(self) -> str:
        r = self.stack.run_tests(self.root)
        return f"Tests: {r.summary()}\n{r.output[-3500:]}"

    def run_smoke(self) -> str:
        s = self.stack.smoke(self.root, self.smoke_frames)
        return f"Automatic play-through {s.summary()}"


def _spec(name: str, desc: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {}, "required": []}}}


RUN_SPECS = [_spec("run_tests", "Run all the project's tests and see the results."),
             _spec("run_smoke", "Run the game automatically without a window for a few hundred frames (scripted "
                                "input) and see which scenes it visited, or the crash.")]


def specs(*names: str) -> list[dict]:
    pool = {s["function"]["name"]: s for s in CODER_SPECS + RUN_SPECS}
    return [pool[n] for n in names]


def summary_of_plan(state: State) -> str:
    p = state.plan
    mods = "; ".join(f"{m['name']}: {m['purpose']}" for m in p.get("modules", []))
    return (f"Game: {p.get('title')} — {p.get('pitch')}\nScenes: {', '.join(p.get('scenes', []))}\n"
            f"Transitions: {'; '.join(f'{t['from']} -> {t['to']} ({t['trigger']})' for t in p.get('transitions', []))}\n"
            f"Modules: {mods}\nControls: {', '.join(f'{c['action']}={c['keys']}' for c in p.get('controls', []))}")


def describe_task(task: Task) -> str:
    part = f" (a smaller step of {task.parent})" if task.parent else ""
    text = (f"Task {task.id}{part}: {task.title}\nFiles: {', '.join(task.files) or '(your choice)'}\n"
            "Acceptance criteria:\n" + "\n".join(f"- {a}" for a in task.acceptance))
    return text + (f"\n\nApproach (planned before coding):\n{task.approach}" if task.approach else "")


# ---------------------------------------------------------------- thinking like an engineer

APPROACH_SCHEMA = {"type": "object", "properties": {
    "approach": {"type": "string"},
    "changes": {"type": "array", "items": {"type": "object", "properties": {
        "file": {"type": "string"}, "what": {"type": "string"}}, "required": ["file", "what"]}},
    "edge_cases": {"type": "array", "items": {"type": "string"}},
    "verify": {"type": "string"}}, "required": ["approach", "changes", "edge_cases", "verify"]}

APPROACH_SYSTEM = """You are the tech lead of Jarvis Studio. Before anyone codes this task, plan it like an engineer:
the approach in two sentences, exactly which files change and what changes in each (use the project's real files and
names from the code map below; reuse what exists), the edge cases that could break it, and how the tests will prove
it works. Keep it small: the least code that meets the acceptance criteria. Answer in JSON.{context}"""


def code_outline(root: Path, stack_name: str, chars: int = 2500) -> str:
    """The project's modules with their classes and functions, from the code itself (graph.code_map)."""
    from jarvis.studio import graph

    cmap = graph.code_map(root, stack_name)
    lines = []
    for mod, info in cmap["modules"].items():
        members = [f"{c}({', '.join(ms[:8])})" for c, ms in info["classes"].items()] + info["functions"][:10]
        lines.append(f"- {mod}: {'; '.join(members) or '(no functions)'}")
    return "\n".join(lines)[:chars]


def approach(root: Path, state: State, task: Task, team: Team) -> str:
    ask = (f"{summary_of_plan(state)}\n\nCode map (the project as it is now):\n{code_outline(root, state.stack)}"
           f"\n\n{describe_task(task)}")
    out = team.think([{"role": "system", "content": APPROACH_SYSTEM.format(context=context(root, task))},
                      {"role": "user", "content": ask}], APPROACH_SCHEMA)
    changes = "\n".join(f"- {c.get('file')}: {c.get('what')}" for c in out.get("changes", [])[:6])
    edges = "; ".join(out.get("edge_cases", [])[:5])
    return (f"{out.get('approach', '').strip()}\nChanges:\n{changes}" + (f"\nEdge cases: {edges}" if edges else "")
            + (f"\nVerified by: {out.get('verify', '').strip()}" if out.get("verify") else ""))[:1500]


DIAGNOSE_SCHEMA = {"type": "object", "properties": {
    "cause": {"type": "string"}, "evidence": {"type": "string"},
    "fix": {"type": "array", "items": {"type": "string"}}, "files": {"type": "array", "items": {"type": "string"}}},
    "required": ["cause", "evidence", "fix", "files"]}

DIAGNOSE_SYSTEM = """You are a senior engineer debugging for Jarvis Studio. Find the ROOT cause of the failure below,
not the symptom: follow the traceback to the line that is really wrong, check imports (circular imports: a module
imported while it is still loading), names and APIs against the files shown. Give the cause in one sentence, the
evidence (file and line), and the smallest fix as concrete steps (which line to remove or change, and how). Never
propose changing a test unless it contradicts the task. If the failure lists REGRESSION tests, they passed before this
task: the cause is in this task's diff (shown), so say which change in the diff broke them and how to restore what they
expect; those tests are right. Answer in JSON.{context}"""

_PATH = re.compile(r"((?:game|src|js|css|tests)/[\w/.-]+\.(?:py|js|mjs|css|html)|main\.py|index\.html)")


def diagnose(root: Path, state: State, task: Task, team: Team, failure: str, previous: str = "") -> str:
    """Root cause before the next attempt. Reads the files the failure points at (and the task's files)."""
    named = list(dict.fromkeys(_PATH.findall(failure.replace("\\", "/")) + task.files))
    files = []
    for rel in named:
        path = root / rel
        if path.is_file() and len(files) < 4:
            files.append(f"--- {rel} ---\n{path.read_text(encoding='utf-8', errors='replace')[:2500]}")
    ask = (f"{describe_task(task)}\n\nFailure:\n{failure[-3500:]}\n\nFiles:\n" + "\n\n".join(files)
           + (f"\n\nThe previous diagnosis didn't fix it, so look deeper:\n{previous}" if previous else ""))
    out = team.think([{"role": "system", "content": DIAGNOSE_SYSTEM.format(context=context(root, task))},
                      {"role": "user", "content": ask}], DIAGNOSE_SCHEMA)
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(out.get("fix", [])[:6], 1))
    return f"Root cause: {out.get('cause', '').strip()}\nEvidence: {out.get('evidence', '').strip()}\nFix:\n{steps}"


def context(root: Path, task: Task | None = None) -> str:
    """The project's rule book, design and latest memory, for every agent's prompt (cut to stay small)."""
    from jarvis.studio.project import Project

    pr = Project(root)
    parts = []
    rules = pr.read_doc("Rules.md")
    if rules:
        parts.append("PROJECT RULES (docs/Rules.md):\n" + rules.split("\n", 2)[-1][:2500])
    design = pr.read_doc("design.md")
    if design and (task is None or task.kind == "visual" or "draw" in task.title.lower()):
        parts.append("DESIGN (docs/design.md):\n" + design.split("\n", 2)[-1][:2000])
    elif design:  # the palette is short and keeps colours consistent everywhere
        palette = [ln for ln in design.splitlines() if ln.startswith("| ") and "#" in ln]
        if palette:
            parts.append("Colour palette (docs/design.md; use PALETTE from the config module):\n" + "\n".join(palette))
    memory = pr.memory(1800)
    if memory:
        parts.append("PROJECT MEMORY (latest decisions, changes, bugs):\n" + memory)
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# ---------------------------------------------------------------- researcher

RESEARCH_SYSTEM = """You are the researcher of Jarvis Studio, a team that builds original games.
The user asked: "{request}". The reference game is {reference}. Below are public web pages about it.
Write the study as Markdown with these sections:
# Reference: {reference}
## Overview
## Core loop
## Controls
## Mechanics   (moves, combat rules, special systems: be concrete, with numbers where the sources give them)
## Progression (unlocks, upgrades, currencies, opponents/bosses)
## What makes it fun
## Mechanics we keep
## Identity we must NOT copy   (names, characters, story, art style, music, logos, distinctive UI)
## Our original take   (a new title idea, new characters and setting, and an art style clearly different from the
   reference's signature look)
Use only what the sources say plus general genre knowledge; never copy their sentences. List the sources at the end.{skills}"""
SITE_RESEARCH_SYSTEM = """You are the researcher of Jarvis Studio, a team that builds original websites.
The user asked: "{request}". The reference website is {reference}. Below are public pages about it.
Write the study as Markdown with these sections:
# Reference: {reference}
## Overview  (what the site is for and who it speaks to)
## Structure  (the sections in order and what each contains)
## Visual style  (layout, typography, colour mood, imagery)
## Motion  (entrance animations, scroll effects, hover states, signature effects)
## What makes it work
## Ideas we keep  (structure and techniques)
## Identity we must NOT copy  (name, logo, text, images, brand colours, distinctive illustrations)
## Our original take  (a new name idea, our own content and a visual style clearly different from the reference)
Use only what the sources say plus general web design knowledge; never copy their sentences. List the sources at
the end.{skills}"""
SEARCHES = ("{ref} gameplay mechanics", "{ref} combat controls moves", "{ref} upgrades progression guide")
SITE_SEARCHES = ("{ref} website design", "{ref} website animation effects", "{ref} landing page sections layout")
SKIP_SITES = ("youtube.com", "youtu.be", "tiktok.com", "facebook.com", "instagram.com", "play.google.com",
              "apps.apple.com", "reddit.com")


def gather_sources(ws: Workspace, reference: str, pages: int = 4, say=print,
                   searches: tuple[str, ...] = SEARCHES) -> list[tuple[str, str]]:
    """Search a few times and read the best pages. Done in Python: given the tools, qwen3 used all its steps
    searching and reading and never wrote the notes (7 Nov 2026)."""
    urls: list[str] = []
    for q in searches:
        try:
            results = ws.search_web(q.format(ref=reference))
        except Exception as e:  # offline, blocked...
            say(f"  search failed: {e!r}")
            continue
        for line in results.splitlines():
            if line.startswith("http") and line not in urls and not any(s in line for s in SKIP_SITES):
                urls.append(line.strip())
    sources = []
    for url in urls:
        if len(sources) >= pages:
            break
        try:
            text = ws.read_webpage(url)
        except Exception:
            continue
        if len(text) > 500:
            sources.append((url, text[:3500]))
            say(f"  read {url}")
    return sources


def research(root: Path, state: State, team: Team) -> str:
    ref = root / "docs" / "reference.md"
    ref.parent.mkdir(exist_ok=True)
    if not state.reference:
        ref.write_text(f"# Reference\n\nNo reference game was named. Request: “{state.request}”.\n\n"
                       "The design is original; genre conventions come from the skills the planner uses.\n",
                       encoding="utf-8")
        return "No reference game named; original design."
    site = state.stack == "site"
    sources = gather_sources(Workspace(root, confirm=lambda q: True), state.reference, say=team.say,
                             searches=SITE_SEARCHES if site else SEARCHES)
    material = "\n\n".join(f"--- Source: {url}\n{text}" for url, text in sources) or (
        "(No pages could be read; use general knowledge of the genre.)")
    system = (SITE_RESEARCH_SYSTEM if site else RESEARCH_SYSTEM).format(
        request=state.request, reference=state.reference, skills=skills_for(state.request, "researcher"))
    msg = team.planner.chat([{"role": "system", "content": system},
                             {"role": "user", "content": f"Sources:\n\n{material}"}], [])
    text = (msg.get("content") or "").strip()
    if len(text) < 300:
        text = (f"# Reference: {state.reference}\n\nThe research notes came out empty. Request: “{state.request}”."
                "\n\nRule: use the genre's mechanics; invent original names, characters, story and art.")
    if sources and "Sources" not in text[-1500:]:
        text += "\n\n## Sources\n" + "\n".join(f"- {url}" for url, _ in sources)
    ref.write_text(text + "\n", encoding="utf-8")
    return f"Studied {len(sources)} pages about {state.reference}."


# ---------------------------------------------------------------- planner

PLAN_SYSTEM = """You are the lead designer and planner of Jarvis Studio, a team of small AI coding agents that builds
original games and websites. Turn the request into a complete plan for a small {what} that can really be finished,
as JSON.

{conventions}

Planning rules:
- title: an ORIGINAL name. characters: original names and descriptions. art_style: drawn in code, original.
- problem: what the game is for (the fun or need it serves); audience: who plays it; out_of_scope: what this first
  version deliberately leaves out.
- data_flow: 4-7 steps of how a frame and a session flow (input -> rules -> state -> drawing -> save...).
- integrations: every outside thing the game connects to (save file, audio, keyboard/touch...). Payment, GitHub,
  online services or AI models only if the request asks for them; otherwise leave them out.
{planning}
- Each task: 1-3 files, depends_on earlier task ids only, 2-4 acceptance criteria that a test can check without a
  window or a human.
- modules: name them like the files (e.g. "game.fighter" or "src/fighter"), with what each one uses.
- features: ids "F1", "F2"...; every task's feature is one of them. priority "must" for the first playable version.{skills}"""


def plan(root: Path, state: State, team: Team, stack, attempts: int = 3) -> dict:
    reference = (root / "docs" / "reference.md").read_text(encoding="utf-8") if (
        root / "docs" / "reference.md").is_file() else ""
    system = PLAN_SYSTEM.format(conventions=stack.conventions, what=stack.what, planning=stack.planning,
                                skills=skills_for(f"{state.request} {reference[:3000]}", "planner", limit=9000))
    ask = f"Request: {state.request}\nTechnology: {state.stack}\n\nResearch notes:\n{reference[:6000]}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ask}]
    problems: list[str] = []
    for _ in range(attempts):
        result = team.think(messages, PLAN_SCHEMA)
        result = normalise_plan(result)
        problems = validate_plan(result, state.reference)
        if not problems:
            return result
        team.say("  plan problems: " + "; ".join(problems[:4]))
        messages += [{"role": "assistant", "content": json.dumps(result)[:12000]},
                     {"role": "user", "content": "Fix these problems and send the whole plan again:\n- "
                                                 + "\n- ".join(problems)}]
    raise PlanError("The planner couldn't make a valid plan: " + "; ".join(problems[:5]))


def normalise_plan(p: dict) -> dict:
    p = dict(p)
    p["scenes"] = [s.strip().lower() for s in p.get("scenes", []) if s.strip()]
    p["transitions"] = [{**t, "from": t.get("from", "").strip().lower(), "to": t.get("to", "").strip().lower()}
                        for t in p.get("transitions", [])]
    tasks = []
    for t in p.get("tasks", []):
        t = dict(t)
        t["id"] = t.get("id", "").strip().upper()
        t["depends_on"] = [d.strip().upper() for d in t.get("depends_on", []) if d.strip()]
        t["acceptance"] = [a.strip() for a in t.get("acceptance", []) if a.strip()][:6]
        t["files"] = [f.strip() for f in t.get("files", []) if f.strip()][:5]
        tasks.append(t)
    p["tasks"] = tasks
    return p


# ---------------------------------------------------------------- tester

TEST_SYSTEM = """You are the tester of Jarvis Studio. Write the tests for ONE task before its code exists.

{conventions}

{plan}

Write only {test_file}. Look at the existing code first (list_files, read_file) so the tests use the real API
(Game, Input, scenes...) and the module/function names the task gives. Every acceptance criterion gets at least one
small test. Tests must run without a window and without waiting. Then run_tests once (this task's tests are
expected to fail until the code exists) and reply with one sentence.{rules}{skills}"""


def write_tests(root: Path, state: State, task: Task, team: Team, stack, feedback: str = "") -> bool:
    test_file = stack.test_file(task.id)
    ws = StudioWorkspace(root, confirm=lambda q: True, stack=stack,
                         refuse_write=lambda rel: None if rel == test_file else
                         f"As the tester you may only write {test_file}.")
    system = TEST_SYSTEM.format(conventions=stack.conventions, plan=summary_of_plan(state), test_file=test_file,
                                rules=team.rules("tester"),
                                skills=skills_for(f"{state.request} {task.title} {' '.join(task.acceptance)}", "tester")
                                ) + context(root, task)
    before = (root / test_file).read_text(encoding="utf-8") if (root / test_file).is_file() else None
    ask = describe_task(task) + (f"\n\nRewrite {test_file}: {feedback}" if feedback else "")
    for nudge in ("", f"\nYou haven't written {test_file} yet. Write it now with write_file."):
        loop.run(ask + nudge, root, team.coder, ws.confirm, say=team.say, max_steps=16, system=system,
                 workspace=ws, specs=specs("list_files", "read_file", "write_file", "run_tests"))
        now = (root / test_file).read_text(encoding="utf-8") if (root / test_file).is_file() else None
        if now is not None and (not feedback or now != before):
            return True
    return False


# ---------------------------------------------------------------- getting unstuck without help

TESTCHECK_SYSTEM = """You check tests for Jarvis Studio. The coder failed to make this task's tests pass several times.
Decide whether the TEST is wrong: it asserts something the task's acceptance criteria don't ask for, contradicts them,
uses names or an API that the task never mentions while the existing code offers another, or is impossible to satisfy.
If the test is a fair check of the criteria and the code is simply not there yet, the test is not wrong.
Give a one-sentence reason that tells the tester exactly what to change. Answer in JSON."""


def check_tests(root: Path, state: State, task: Task, team: Team, stack, failure: str) -> tuple[bool, str]:
    test = root / stack.test_file(task.id)
    if not test.is_file():
        return False, "no test file"
    ask = (f"{describe_task(task)}\n\nThe test file {stack.test_file(task.id)}:\n{test.read_text(encoding='utf-8')[:6000]}"
           f"\n\nLatest failure:\n{failure[-3000:]}")
    out = team.think([{"role": "system", "content": TESTCHECK_SYSTEM},
                             {"role": "user", "content": ask}], TESTCHECK_SCHEMA)
    return bool(out.get("test_is_wrong")), str(out.get("reason", ""))[:400]


SPLIT_SYSTEM = """You plan for Jarvis Studio. A small coding agent could not finish the task below in several attempts.
Split it into 2 to 4 smaller steps, in order, so that each step:
- changes ONE file (sometimes two) and does one thing,
- can be checked by 1-2 acceptance criteria a test can verify without a window,
- builds on the steps before it, using the project's existing code and names.
Together the steps must fully meet the original task's acceptance criteria. Use the failure output to see where it
got stuck, and make that part its own small step. Steps change the project's code only, never test files (tests
are written by the tester and are fixed). Answer in JSON.{context}"""


def split(root: Path, state: State, task: Task, team: Team, failure: str) -> list[dict]:
    ask = f"{summary_of_plan(state)}\n\n{describe_task(task)}\n\nWhere it got stuck (latest output):\n{failure[-3500:]}"
    out = team.think([{"role": "system", "content": SPLIT_SYSTEM.format(context=context(root))},
                             {"role": "user", "content": ask}], SPLIT_SCHEMA)
    steps = [s for s in out.get("steps", []) if isinstance(s, dict) and s.get("title", "").strip()
             and [a for a in s.get("acceptance", []) if a.strip()]]
    return steps[:4] if len(steps) >= 2 else []


# ---------------------------------------------------------------- design and rules

DESIGN_SYSTEM = """You are the designer and tech lead of Jarvis Studio. From the plan below write the game's design
guide and rule book as JSON:
- palette: 5-8 colours, each {{name, hex "#rrggbb", use}} (background, player, opponent, accent, UI text, danger...),
  matching the art style; strong contrast between characters and background.
- typography, shapes (how characters/objects are drawn in code), layout (where HUD, health bars, menus go), motion
  (animation and feedback: hit flash, shake...).
- avoid_look: 3-6 things it must NOT look like (e.g. default grey rectangles, the reference game's signature style,
  clutter, low contrast).
- must: 4-8 strict project rules beyond the general ones (e.g. "all tuning numbers live in config").
- avoid: 3-6 prohibited practices for this project.
- screens: a rough wireframe of EVERY scene (for a website: every section), 3-10 elements each:
  {{kind: text|button|bar|character|panel|image|shape|icon, label, x, y, w, h, color}}. x, y (top-left), w, h are
  percentages of the screen (0-100); color is one of the palette names. Lay them out as a real screen would be:
  health bars along the top, characters on the floor, menus centred, a hero banner across the top of a page...
  Show the real parts, never one big shape for a whole screen: a gallery is 4-6 'image' elements in a grid, a list
  of testimonials is 2-3 'panel' elements with 'text' inside, a form is 'panel' inputs plus a 'button'; every website
  section starts with its heading as a 'text' element; a fight screen has both characters, both health bars and the
  timer.
Original look only: never the reference game's style.

{conventions}"""


def design(root: Path, state: State, team: Team, stack) -> dict:
    ask = f"{summary_of_plan(state)}\nArt style: {state.plan.get('art_style', '')}\nReference (mechanics only): " \
          f"{state.reference or 'none'}"
    out = team.think([{"role": "system", "content": DESIGN_SYSTEM.format(conventions=stack.conventions)},
                             {"role": "user", "content": ask}], DESIGN_SCHEMA)
    import re as _re

    out["palette"] = [c for c in out.get("palette", []) if _re.fullmatch(r"#[0-9a-fA-F]{6}", c.get("hex", "").strip())]
    if not out["palette"]:  # never leave the game without colours
        out["palette"] = [{"name": "background", "hex": "#141828", "use": "screen background"},
                          {"name": "player", "hex": "#4fa3ff", "use": "player"},
                          {"name": "opponent", "hex": "#ff6f5a", "use": "opponent"},
                          {"name": "accent", "hex": "#ffd23f", "use": "highlights, health"},
                          {"name": "text", "hex": "#f0f0f0", "use": "UI text"}]
    from jarvis.studio import colors

    out["palette"], out["adjusted"] = colors.fix_contrast(out["palette"])
    out["screens"] = clean_screens(out.get("screens", []), out["palette"], state.plan.get("scenes", []))
    return out


def clean_screens(screens: list, palette: list[dict], scenes: list[str]) -> list[dict]:
    """Keep the wireframes drawable: known kinds, numbers inside 0-100, palette colour names, one per scene."""
    from jarvis.studio.project import SKETCH_KINDS

    names = {c["name"].strip().lower(): c["name"] for c in palette}
    fallback = palette[min(1, len(palette) - 1)]["name"]
    out, seen = [], set()
    for sc in screens if isinstance(screens, list) else []:
        scene = str(sc.get("scene", "")).strip().lower()
        if not scene or scene in seen:
            continue
        seen.add(scene)
        elements = []
        for el in sc.get("elements", [])[:12]:
            try:
                x, y, w, h = (max(0.0, min(100.0, float(el.get(k, 0)))) for k in ("x", "y", "w", "h"))
            except (TypeError, ValueError):
                continue
            elements.append({"kind": el.get("kind") if el.get("kind") in SKETCH_KINDS else "panel",
                             "label": str(el.get("label", ""))[:40], "x": x, "y": y,
                             "w": max(2.0, min(w, 100 - x)), "h": max(2.0, min(h, 100 - y)),
                             "color": names.get(str(el.get("color", "")).strip().lower(), fallback)})
        if elements:
            out.append({"scene": scene, "elements": elements})
    order = {s: i for i, s in enumerate(scenes)}
    return sorted(out, key=lambda s: order.get(s["scene"], 99))[:8]


# ---------------------------------------------------------------- coder

CODE_SYSTEM = """You are the coder of Jarvis Studio. Implement ONE task so that ALL tests pass and the automatic
check (the play-through, or the page check for websites) runs without errors.

{conventions}

{plan}

Work like an engineer:
1. Read the task's tests, the approach and diagnosis you're given, and every file you will change. Never guess an
   API or a name: read the code that defines it.
2. Before changing anything, state the root cause (or the plan) in one sentence.
3. Make the smallest change that does it: replace_in_file for existing files (copy the exact current text from
   read_file), write_file only for new files (complete, never placeholders).
4. run_tests; if something fails, read the output and fix the cause, not the symptom; then run_smoke.
5. Stop when both pass and reply with one sentence.
Tests and docs are fixed: change the code, never the tests. If unsure about an API, search_references or
search_web.{rules}{skills}"""


def _coder_may_write(rel: str) -> str | None:
    if rel.startswith("tests/"):
        return "Tests are fixed while coding: change the code so they pass."
    if rel.startswith(("docs/", ".studio/", ".git/")):
        return "The coder doesn't write docs or studio files."
    return None


def write_code(root: Path, state: State, task: Task, team: Team, stack, feedback: str = "", model=None) -> str:
    ws = StudioWorkspace(root, confirm=lambda q: True, stack=stack, refuse_write=_coder_may_write)
    system = CODE_SYSTEM.format(conventions=stack.conventions, plan=summary_of_plan(state), rules=team.rules("coder"),
                                skills=skills_for(f"{state.request} {task.title} {' '.join(task.acceptance)}", "coder")
                                ) + context(root, task)
    ask = describe_task(task) + f"\nTests for this task: {stack.test_file(task.id)}"
    if feedback:
        ask += f"\n\nWhat still needs fixing:\n{feedback[-5000:]}"
    # 16 steps: a round of 30 took 10+ minutes with the 7B model rewriting whole files (7 Nov 2026); more
    # rounds with fresh feedback, then the second opinion and the helper, do better than one long round
    return loop.run(ask, root, model or team.coder, ws.confirm, say=team.say, max_steps=16, system=system, workspace=ws,
                    specs=specs("list_files", "read_file", "replace_in_file", "write_file", "run_tests", "run_smoke",
                                "search_references", "search_web", "read_webpage"))


# ---------------------------------------------------------------- reviewer

REVIEW_SYSTEM = """You are the reviewer of Jarvis Studio. Check one finished task: does the change do what the task
and its acceptance criteria say, keep game logic out of drawing code, and follow the project's rules?
Approve when it works and is reasonably simple. Reject only for real problems, each stated as one concrete fix.
Answer in JSON.{rules}"""


def review(root: Path, state: State, task: Task, team: Team, diff: str, test_summary: str) -> tuple[bool, list[str]]:
    messages = [{"role": "system", "content": REVIEW_SYSTEM.format(rules=team.rules("reviewer")) + context(root, task)},
                {"role": "user", "content": f"{summary_of_plan(state)}\n\n{describe_task(task)}\n\nTests: "
                                            f"{test_summary}\n\nThe change (git diff):\n{diff[:14000]}"}]
    out = team.planner.json(messages, REVIEW_SCHEMA)
    problems = [p for p in out.get("problems", []) if isinstance(p, str) and p.strip()][:6]
    return bool(out.get("approve")) or not problems, problems
