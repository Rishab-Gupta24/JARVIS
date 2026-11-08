"""A studio project on disk: the plan, the task board and the log, all inside the game's folder.

    <project>/docs/            request, reference study, PRD, features, tasks, architecture, project.html
    <project>/.studio/state.json   phase, plan, tasks (written atomically: a crash never leaves half a file)
    <project>/.studio/log.jsonl    one line per step, for the timeline and for debugging
    <project>/.studio/STOP         create it to make the loop pause after the current step
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PHASES = ("research", "plan", "build", "playtest", "integrate", "done")
TASK_STATES = ("todo", "testing", "coding", "review", "done", "blocked", "skipped")
STACKS = ("pygame", "web")
MAX_TASKS = 25
# Words a game title may share with its reference ("a game like Shadow Fight 2" may still be a "fight" game).
GENERIC = {"game", "games", "the", "a", "an", "of", "and", "fight", "fighter", "fighting", "battle", "war", "run",
           "runner", "jump", "puzzle", "racing", "race", "shooter", "quest", "legend", "legends", "hero", "heroes",
           "world", "city", "super", "mini", "online", "simulator", "craft", "tycoon", "io", "2d", "3d",
           "website", "site", "web", "page", "pages", "app", "studio", "shop", "store", "portfolio", "agency", "labs"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    title: str
    feature: str = ""
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    status: str = "todo"
    rounds: int = 0          # coding rounds used
    reviews: int = 0         # review rejections
    helper_calls: int = 0
    notes: list[str] = field(default_factory=list)
    kind: str = "code"       # "visual": drawing only, no test-first (checked by the play-through and review)
    depth: int = 0           # 0 = planned task; 1, 2 = made by splitting a stuck task
    parent: str = ""
    tests_checked: bool = False  # the planner already judged this task's tests against its criteria
    approach: str = ""       # the tech lead's plan for this task, made before testing and coding

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class State:
    request: str
    name: str
    stack: str
    reference: str | None = None
    phase: str = "research"
    plan: dict = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)
    current: str | None = None
    helper_calls: int = 0
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    seconds_used: float = 0.0
    last_tests: dict = field(default_factory=dict)
    last_smoke: dict = field(default_factory=dict)
    message: str = ""
    playtests: int = 0                                     # playtest rounds run
    last_playtest: dict = field(default_factory=dict)      # summary, problems, reachable scenes, shots

    def task(self, task_id: str) -> Task:
        return next(t for t in self.tasks if t.id == task_id)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in TASK_STATES}
        for t in self.tasks:
            out[t.status] += 1
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tasks"] = [asdict(t) for t in self.tasks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        d = dict(d)
        d["tasks"] = [Task.from_dict(t) for t in d.get("tasks", [])]
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class Project:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.studio = self.root / ".studio"
        self.docs = self.root / "docs"

    @property
    def state_file(self) -> Path:
        return self.studio / "state.json"

    def exists(self) -> bool:
        return self.state_file.is_file()

    def load(self) -> State:
        return State.from_dict(json.loads(self.state_file.read_text(encoding="utf-8")))

    def save(self, state: State) -> None:
        state.updated_at = now_iso()
        self.studio.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def log(self, event: str, **data) -> None:
        self.studio.mkdir(parents=True, exist_ok=True)
        with open(self.studio / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now_iso(), "event": event, **data}, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        path = self.studio / "log.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut by a crash
        return out

    def write_doc(self, name: str, text: str) -> Path:
        self.docs.mkdir(parents=True, exist_ok=True)
        path = self.docs / name
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def read_doc(self, name: str) -> str:
        path = self.docs / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    MEMORY_HEAD = ("# Memory\n\nImportant decisions, changes, bugs and project context, newest last. Written by the "
                   "studio as it works; the agents read the latest entries before each step.\n")

    def remember(self, kind: str, text: str) -> None:
        """One line in docs/memory.md: kind is decision, change, bug or context."""
        path = self.docs / "memory.md"
        self.docs.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_text(self.MEMORY_HEAD + "\n", encoding="utf-8")
        line = " ".join(text.split())[:400]
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {datetime.now().strftime('%d %b %H:%M')} **{kind}**: {line}\n")

    def memory(self, chars: int = 2500) -> str:
        """The latest memory entries, for the agents' prompts."""
        lines = [ln for ln in self.read_doc("memory.md").splitlines() if ln.startswith("- ")]
        out, used = [], 0
        for ln in reversed(lines):
            if used + len(ln) > chars:
                break
            out.append(ln)
            used += len(ln) + 1
        return "\n".join(reversed(out))

    def stop_requested(self) -> bool:
        return (self.studio / "STOP").exists()


# ---------- reading the request ----------

_REF = re.compile(r"\b(?:just\s+)?(?:like|similar\s+to|inspired\s+by|based\s+on|clone\s+of|version\s+of|in\s+the\s+style\s+of)"
                  r"\s+(.+?)(?=\s+(?:but|with|for|on|that|which|where|and\s+make)\b|[.,!?;]|$)", re.I)


def reference_of(request: str) -> str | None:
    """'make me a game like Shadow Fight 2 with swords' -> 'Shadow Fight 2'."""
    m = _REF.search(request)
    if not m:
        return None
    ref = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.I)
    ref = re.sub(r"\s+(?:or\s+similar|or\s+something|game)$", "", ref, flags=re.I).strip()
    return ref or None


_WEB = re.compile(r"\b(web|html|html5|browser|iphone|phone|mobile|ipad|canvas|javascript|js|online)\b", re.I)
_PYGAME = re.compile(r"\b(pygame|python|desktop|pc\s+game)\b", re.I)
SITE = re.compile(r"\b(web\s?sites?|landing\s?pages?|portfolio|home\s?page|web\s?pages?|personal\s+site|"
                  r"business\s+site|blog\s+site|site\s+for)\b", re.I)
GAME_WORDS = re.compile(r"\b(games?|platformer|shooter|rpg|roguelike|arcade|metroidvania)\b", re.I)


def stack_for(request: str) -> str:
    """The user's own words decide: a website (no game words) -> the site stack; a game -> pygame unless they said
    web/iPhone/browser."""
    if SITE.search(request) and not GAME_WORDS.search(request):
        return "site"
    if _PYGAME.search(request):
        return "pygame"
    return "web" if _WEB.search(request) else "pygame"


def reference_words(reference: str | None) -> set[str]:
    words = {w for w in re.findall(r"[a-z0-9]+", (reference or "").lower()) if len(w) > 1 and not w.isdigit()}
    return words - GENERIC


# ---------- the plan ----------

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "pitch": {"type": "string"},
        "problem": {"type": "string"},
        "audience": {"type": "string"},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
        "data_flow": {"type": "array", "items": {"type": "string"}},
        "integrations": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "purpose": {"type": "string"}, "how": {"type": "string"}},
            "required": ["name", "purpose", "how"]}},
        "art_style": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name", "description"]}},
        "controls": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"}, "keys": {"type": "string"}}, "required": ["action", "keys"]}},
        "core_loop": {"type": "array", "items": {"type": "string"}},
        "features": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
            "priority": {"type": "string", "enum": ["must", "should"]}},
            "required": ["id", "name", "description", "priority"]}},
        "scenes": {"type": "array", "items": {"type": "string"}},
        "transitions": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "string"}, "to": {"type": "string"}, "trigger": {"type": "string"}},
            "required": ["from", "to", "trigger"]}},
        "modules": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "purpose": {"type": "string"},
            "uses": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "purpose", "uses"]}},
        "tasks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "feature": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "files": {"type": "array", "items": {"type": "string"}},
            "acceptance": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "title", "feature", "depends_on", "files", "acceptance"]}},
    },
    "required": ["title", "pitch", "problem", "audience", "out_of_scope", "data_flow", "integrations", "art_style",
                 "characters", "controls", "core_loop", "features", "scenes", "transitions", "modules", "tasks"],
}

DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "palette": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "hex": {"type": "string"}, "use": {"type": "string"}},
            "required": ["name", "hex", "use"]}},
        "typography": {"type": "string"},
        "shapes": {"type": "string"},
        "layout": {"type": "string"},
        "motion": {"type": "string"},
        "avoid_look": {"type": "array", "items": {"type": "string"}},
        "must": {"type": "array", "items": {"type": "string"}},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "screens": {"type": "array", "items": {"type": "object", "properties": {
            "scene": {"type": "string"},
            "elements": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["text", "button", "bar", "character", "panel", "image", "shape",
                                                    "icon"]},
                "label": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                "w": {"type": "number"}, "h": {"type": "number"}, "color": {"type": "string"}},
                "required": ["kind", "label", "x", "y", "w", "h", "color"]}}},
            "required": ["scene", "elements"]}},
    },
    "required": ["palette", "typography", "shapes", "layout", "motion", "avoid_look", "must", "avoid", "screens"],
}
SKETCH_KINDS = ("text", "button", "bar", "character", "panel", "image", "shape", "icon")

SPLIT_SCHEMA = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "object", "properties": {
    "title": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
    "acceptance": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "files", "acceptance"]}}},
    "required": ["steps"]}

TESTCHECK_SCHEMA = {"type": "object", "properties": {"test_is_wrong": {"type": "boolean"}, "reason": {"type": "string"}},
                    "required": ["test_is_wrong", "reason"]}


def split_into(state: "State", task: "Task", steps: list[dict]) -> list["Task"]:
    """Put a stuck task's smaller steps in front of it; the task itself waits for them, then checks the whole."""
    taken = {t.id for t in state.tasks}
    subs, prev = [], None
    for i, step in enumerate(steps):
        sid = f"{task.id}{'abcdefgh'[i]}" if task.depth == 0 else f"{task.id}{i + 1}"
        while sid in taken:
            sid += "x"
        taken.add(sid)
        deps = list(task.depends_on) + ([prev] if prev else [])
        subs.append(Task(sid, step["title"][:120], feature=task.feature, depends_on=deps,
                         files=[f for f in step.get("files", []) if f.strip()][:3],
                         acceptance=[a for a in step.get("acceptance", []) if a.strip()][:3], kind=task.kind,
                         depth=task.depth + 1, parent=task.id))
        prev = sid
    at = state.tasks.index(task)
    state.tasks[at:at] = subs
    task.depends_on = list(dict.fromkeys(task.depends_on + [s.id for s in subs]))
    task.status, task.rounds = "todo", 0
    task.notes.append("split into " + ", ".join(s.id for s in subs))
    return subs


def topo_order(tasks: list[dict]) -> list[str] | None:
    """Task ids in an order where every task comes after the tasks it depends on; None if there's a cycle."""
    deps = {t["id"]: [d for d in t.get("depends_on", []) if d != t["id"]] for t in tasks}
    order, state = [], {}

    def visit(tid: str) -> bool:
        if state.get(tid) == "done":
            return True
        if state.get(tid) == "visiting":
            return False
        state[tid] = "visiting"
        for d in deps.get(tid, []):
            if d in deps and not visit(d):
                return False
        state[tid] = "done"
        order.append(tid)
        return True

    for t in tasks:
        if not visit(t["id"]):
            return None
    return order


def validate_plan(plan: dict, reference: str | None = None) -> list[str]:
    """Problems with a plan, in words the planner can act on. Empty list = usable."""
    problems = []
    tasks = plan.get("tasks") or []
    if not plan.get("title", "").strip():
        problems.append("The game needs a title.")
    if not 3 <= len(tasks) <= MAX_TASKS:
        problems.append(f"Give between 3 and {MAX_TASKS} tasks (you gave {len(tasks)}).")
    ids = [t.get("id", "") for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"Task ids must be unique; repeated: {', '.join(dupes)}.")
    known = set(ids)
    for t in tasks:
        missing = [d for d in t.get("depends_on", []) if d not in known]
        if missing:
            problems.append(f"Task {t.get('id')} depends on unknown tasks {', '.join(missing)}.")
        if t.get("id") in t.get("depends_on", []):
            problems.append(f"Task {t.get('id')} depends on itself.")
        if not [a for a in t.get("acceptance", []) if a.strip()]:
            problems.append(f"Task {t.get('id')} needs acceptance criteria a test can check.")
        if not t.get("title", "").strip():
            problems.append(f"Task {t.get('id')} needs a title.")
    if not dupes and topo_order(tasks) is None:
        problems.append("The task dependencies form a cycle; a task can only depend on earlier work.")
    scenes = [s.lower() for s in plan.get("scenes", [])]
    if not scenes:
        problems.append("List the game's scenes (e.g. menu, play, results).")
    for tr in plan.get("transitions", []):
        for end in (tr.get("from", ""), tr.get("to", "")):
            if end.lower() not in scenes:
                problems.append(f"Transition uses scene '{end}', which isn't in the scene list.")
    banned = reference_words(reference)
    if banned:
        names = [plan.get("title", "")] + [c.get("name", "") for c in plan.get("characters", [])]
        for name in names:
            hit = banned & set(re.findall(r"[a-z0-9]+", name.lower()))
            if hit:
                problems.append(f"'{name}' reuses the reference game's name ({', '.join(sorted(hit))}). Invent an "
                                "original name: borrow mechanics, never names, characters or story.")
    return problems


def tasks_from_plan(plan: dict) -> list[Task]:
    order = topo_order(plan["tasks"]) or [t["id"] for t in plan["tasks"]]
    by_id = {t["id"]: t for t in plan["tasks"]}
    return [Task.from_dict({**by_id[i], "status": "todo"}) for i in order]


def next_task(state: State) -> Task | None:
    """The first unfinished task whose dependencies are all done."""
    done = {t.id for t in state.tasks if t.status == "done"}
    for t in state.tasks:
        if t.status in ("todo", "testing", "coding", "review") and all(d in done for d in t.depends_on):
            return t
    return None


def skip_unreachable(state: State) -> list[str]:
    """Tasks that wait on a blocked (or skipped) task can never run: mark them skipped."""
    skipped, changed = [], True
    while changed:
        changed = False
        dead = {t.id for t in state.tasks if t.status in ("blocked", "skipped")}
        for t in state.tasks:
            if t.status in ("todo", "testing", "coding", "review") and any(d in dead for d in t.depends_on):
                t.status = "skipped"
                t.notes.append("skipped: depends on " + ", ".join(d for d in t.depends_on if d in dead))
                skipped.append(t.id)
                changed = True
    return skipped
