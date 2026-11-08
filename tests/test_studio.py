"""Jarvis Studio: plan checks, the task loop (with fake models and a fake helper), plugins, skills, graphs,
documents, status, and both stacks' skeletons for real (pygame headless; web via node + headless Edge)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jarvis.skills import parse, pick, select
from jarvis.studio import agents, docs, graph, plugins, stacks, status
from jarvis.studio.loop import Budget, Studio
from jarvis.studio.project import (Project, State, Task, next_task, reference_of, skip_unreachable, stack_for,
                                   tasks_from_plan, topo_order, validate_plan)


def good_plan(**over) -> dict:
    plan = {
        "title": "Ink Duel", "pitch": "Brush-stroke duels.", "art_style": "ink strokes on paper",
        "characters": [{"name": "Kaito Brush", "description": "a calligrapher"}],
        "controls": [{"action": "attack", "keys": "J"}], "core_loop": ["fight", "upgrade"],
        "features": [{"id": "F1", "name": "Combat", "description": "hits", "priority": "must"}],
        "scenes": ["menu", "play"], "transitions": [{"from": "menu", "to": "play", "trigger": "confirm"}],
        "modules": [{"name": "game.fighter", "purpose": "fighter state", "uses": []}],
        "tasks": [{"id": "T1", "title": "Fighter health", "feature": "F1", "depends_on": [], "files": ["game/fighter.py"],
                   "acceptance": ["take_hit lowers health"]},
                  {"id": "T2", "title": "Combat", "feature": "F1", "depends_on": ["T1"], "files": ["game/combat.py"],
                   "acceptance": ["a hit in reach lands"]},
                  {"id": "T3", "title": "Rounds", "feature": "F1", "depends_on": ["T2"], "files": ["game/rounds.py"],
                   "acceptance": ["2 round wins end the match"]}],
    }
    plan.update(over)
    return plan


# ---------------------------------------------------------------- reading the request, plans

@pytest.mark.parametrize("text,ref", [
    ("make me a game like shadow fight 2", "shadow fight 2"),
    ("build a game just like Shadow Fight 2 or similar", "Shadow Fight 2"),
    ("a platformer inspired by Celeste with dashes", "Celeste"),
    ("make me a snake game", None),
])
def test_reference_of(text, ref):
    assert reference_of(text) == ref


def test_stack_from_the_users_words():
    assert stack_for("make me a fighting game") == "pygame"
    assert stack_for("a game I can play on my iPhone") == "web"
    assert stack_for("an HTML5 browser game") == "web"
    assert stack_for("a pygame game for my phone") == "pygame"


def test_a_good_plan_passes_and_is_ordered():
    plan = good_plan()
    assert validate_plan(plan, "Shadow Fight 2") == []
    assert topo_order(plan["tasks"]) == ["T1", "T2", "T3"]


def test_plan_problems_are_found():
    tasks = good_plan()["tasks"]
    cyc = [dict(t) for t in tasks]
    cyc[0]["depends_on"] = ["T3"]
    assert any("cycle" in p for p in validate_plan(good_plan(tasks=cyc)))
    bad = [dict(t) for t in tasks] + [{"id": "T4", "title": "x", "feature": "F1", "depends_on": ["T9"], "files": [],
                                        "acceptance": []}]
    found = " ".join(validate_plan(good_plan(tasks=bad)))
    assert "unknown tasks T9" in found and "acceptance criteria" in found
    dup = [dict(t) for t in tasks] + [dict(tasks[0])]
    assert any("unique" in p for p in validate_plan(good_plan(tasks=dup)))
    assert any("scene 'shop'" in p for p in validate_plan(
        good_plan(transitions=[{"from": "menu", "to": "shop", "trigger": "x"}])))


def test_reference_names_are_refused_but_genre_words_are_fine():
    probs = validate_plan(good_plan(title="Shadow Fighter Legends"), "Shadow Fight 2")
    assert any("reuses the reference game's name" in p and "shadow" in p for p in probs)
    probs = validate_plan(good_plan(characters=[{"name": "Shadow", "description": "x"}]), "Shadow Fight 2")
    assert probs
    assert validate_plan(good_plan(title="Ink Fighters"), "Shadow Fight 2") == []  # "fight" is a genre word


def test_next_task_and_skipping_after_a_block():
    state = State("r", "n", "pygame", tasks=tasks_from_plan(good_plan()))
    assert next_task(state).id == "T1"
    state.task("T1").status = "done"
    assert next_task(state).id == "T2"
    state.task("T2").status = "blocked"
    assert next_task(state) is None
    assert skip_unreachable(state) == ["T3"] and state.task("T3").status == "skipped"


def test_state_round_trips_and_saves_atomically(tmp_path):
    pr = Project(tmp_path)
    state = State("make a game", "game", "pygame", reference="X", tasks=tasks_from_plan(good_plan()))
    state.task("T1").notes.append("hi")
    pr.save(state)
    back = pr.load()
    assert back.task("T1").notes == ["hi"] and back.reference == "X" and back.tasks[2].depends_on == ["T2"]
    assert not (tmp_path / ".studio" / "state.tmp").exists()
    pr.log("x", task="T1")
    (tmp_path / ".studio" / "log.jsonl").open("a").write('{"cut half')
    assert [e["event"] for e in pr.events()] == ["x"]


# ---------------------------------------------------------------- skills

def test_skills_parse_and_select_by_keyword_and_role():
    skill = parse("---\nname: t\nroles: coder\nkeywords: ai, fight\n---\nBody text")
    assert skill.name == "t" and skill.keywords == ("ai", "fight") and skill.body == "Body text"
    chosen = [s.name for s in pick("a fighting game with an enemy AI and upgrades", "coder")]
    assert chosen[:1] == ["fighting-game"] or "fighting-game" in chosen
    assert "enemy-ai" in chosen and "balancing" in chosen and "prd-writing" not in chosen
    assert "air" not in [k for s in pick("hot air balloon", "coder") for k in s.keywords if k == "ai"]
    assert {s.name for s in pick("anything", "planner")} >= {"prd-writing", "task-breakdown"}  # always-on
    assert len(select("fight enemy balance animation pygame web game", "coder", limit=1500)) <= 1600


# ---------------------------------------------------------------- plugins

def test_ponytail_flags_packages_unused_functions_and_duplicates(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "a.py").write_text("import numpy\nimport pygame\nimport json\n\n\ndef used():\n    return 1\n\n\n"
                                            "def unused_helper():\n    return 2\n", encoding="utf-8")
    block = "\n".join(f"    total_value_{i} = compute_something({i})" for i in range(9))
    (tmp_path / "game" / "b.py").write_text(f"from game.a import used\n\n\ndef f():\n{block}\n    return used()\n",
                                            encoding="utf-8")
    (tmp_path / "game" / "c.py").write_text(f"def g():\n{block}\n    return 0\n\n\ng()\nf = 1\n", encoding="utf-8")
    found = " | ".join(plugins.Ponytail("full").check(tmp_path, stacks.get("pygame")))
    assert "imports 'numpy'" in found and "pygame" not in found.split("numpy")[0][-30:] and "'json'" not in found
    assert "unused_helper" in found and "'used'" not in found
    assert "The same 8 lines appear" in found
    assert plugins.Ponytail("lite").check(tmp_path, stacks.get("pygame")) == []
    assert "Ponytail rule" in plugins.Ponytail().rules("coder") and plugins.Ponytail().rules("planner") == ""


def test_ponytail_web_checks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.js").write_text('import _ from "lodash";\nimport { a } from "./engine";\n', encoding="utf-8")
    found = " ".join(plugins.Ponytail().check(tmp_path, stacks.get("web")))
    assert "npm package" in found and "without the .js extension" in found


def test_phone_updates_send_and_never_raise():
    sent = []
    pu = plugins.PhoneUpdates(lambda t, b: sent.append((t, b)))
    pu.on_event("plan_ready", "Ink Duel", title="Ink Duel", tasks=12, stack="pygame")
    pu.on_event("round", "Ink Duel")  # not a phone event
    assert sent == [("Ink Duel: Plan ready: Ink Duel", "12 tasks, pygame. Building now.")]
    plugins.PhoneUpdates(lambda t, b: 1 / 0).on_event("done", "x")  # a broken phone link doesn't stop the build
    assert [p.name for p in plugins.load(["ponytail", "review", "nope"])] == ["ponytail", "review"]


# ---------------------------------------------------------------- graphs and documents

def test_module_graph_from_real_code(tmp_path):
    stacks.get("pygame").scaffold(tmp_path, "Ink Duel")
    nodes, edges = graph.module_graph(tmp_path, "pygame")
    assert {"main", "game.engine", "game.scenes", "game.config"} <= set(nodes)
    assert ("game.scenes", "game.engine", "") in edges and ("main", "game.scenes", "") in edges
    web = tmp_path / "web"
    stacks.get("web").scaffold(web, "Ink Duel")
    nodes, edges = graph.module_graph(web, "web")
    assert ("src/main", "src/engine", "") in edges and ("src/scenes", "src/config", "") in edges


def test_layers_handle_cycles_and_svg_marks_the_walked_path():
    nodes = ["menu", "play", "results"]
    edges = [("menu", "play", "confirm"), ("play", "results", "ko"), ("results", "menu", "confirm")]
    assert graph.layers(nodes, edges) == {"menu": 0, "play": 1, "results": 2}
    out = graph.svg(nodes, edges, visited=["menu", "play"])
    assert out.startswith("<svg") and out.count('class="edge walked"') == 1 and out.count('class="node visited"') == 2
    assert 'n_menu -->|"confirm"| n_play' in graph.mermaid(nodes, edges)


def test_documents_are_written(tmp_path):
    pr = Project(tmp_path)
    stacks.get("pygame").scaffold(tmp_path, "Ink Duel")
    state = State("make me a game like Shadow Fight 2", "game", "pygame", reference="Shadow Fight 2",
                  plan=good_plan(), tasks=tasks_from_plan(good_plan()))
    state.task("T1").status = "done"
    state.last_smoke = {"ok": True, "summary": "ran", "scenes": ["menu", "play"]}
    state.plan.update(problem="Quick duels for short breaks.", audience="Players who like timing-based fights.",
                      out_of_scope=["online play"], data_flow=["input -> Input", "scene updates rules", "draw"],
                      integrations=[{"name": "Save file", "purpose": "progress", "how": "save.json"}],
                      design={"palette": [{"name": "Ink", "hex": "#101820", "use": "background"}], "typography": "bold",
                              "shapes": "brush strokes", "layout": "health bars top", "motion": "hit flash",
                              "avoid_look": ["black silhouettes"], "must": ["tuning lives in config"],
                              "avoid": ["magic numbers in scenes"]})
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "architecture.md").write_text("old")  # renamed to Architecture.md
    (tmp_path / "docs" / "features.md").write_text("old")      # folded into PRD.md and tasks.md
    docs.write_all(pr, state, "python main.py")
    names = {p.name for p in (tmp_path / "docs").iterdir()}
    assert {"PRD.md", "Architecture.md", "Rules.md", "memory.md", "design.md", "tasks.md", "project.html"} <= names
    assert "architecture.md" not in names and "features.md" not in names
    prd = pr.read_doc("PRD.md")
    assert "# Ink Duel" in prd and "Inspired by the *mechanics* of Shadow Fight 2" in prd and "| attack | J |" in prd
    assert "## The problem it solves\nQuick duels for short breaks." in prd and "- online play" in prd
    arch = pr.read_doc("Architecture.md")
    assert "## Project directory" in arch and "main.py   # window, keyboard" in arch and "pygame 2.6" in arch
    assert "| Save file | progress | save.json |" in arch and "| Payment | none |" in arch and "```mermaid" in arch
    rules = pr.read_doc("Rules.md")
    assert "## Approved libraries" in rules and "numpy" in rules and "- tuning lives in config" in rules
    assert "- magic numbers in scenes" in rules and "never imports pygame" in rules
    design = pr.read_doc("design.md")
    assert "| Ink | #101820 | background |" in design and "- black silhouettes" in design
    board = pr.read_doc("tasks.md")
    assert "**Step 1: T1 Fighter health** `done`" in board and "1/3 done" in board
    assert pr.read_doc("memory.md").startswith("# Memory")
    page = pr.read_doc("project.html")
    assert "<svg" in page and "Ink Duel" in page and "menu → play" in page and 'http-equiv="refresh"' in page


def test_status_sentences(tmp_path):
    state = State("r", "game", "pygame", plan=good_plan(), tasks=tasks_from_plan(good_plan()), phase="build")
    state.task("T1").status = "done"
    state.task("T2").status = "blocked"
    state.current = "T3"
    assert status.describe(state) == "Ink Duel: building, 1 of 3 tasks done. Now on T3, Rounds. 1 task blocked."
    state.phase = "done"
    assert status.describe(state).startswith("Ink Duel is ready: 1 of 3")
    pr = Project(tmp_path / "ink-duel")
    pr.save(state)
    assert status.find(tmp_path, "ink duel").root == pr.root
    assert status.hud_line(tmp_path) == "Studio: Ink Duel 1/3 · done"


# ---------------------------------------------------------------- the loop, with fake agents

class FakeStack:
    """Tests pass once the 'code' file contains the word GREEN; smoke always runs."""
    name = "pygame"
    allowed_imports: set = set()
    conventions = "fake"

    def __init__(self):
        self.green: set[str] = set()

    def run_tests(self, root):
        active = [t for t in self.green if not (root / "tests" / f"{t}.py.parked").exists()]  # parked tests don't run
        ok = all((root / f"{t}.txt").is_file() and "GREEN" in (root / f"{t}.txt").read_text() for t in active)
        return stacks.TestResult(ok, 3 if ok else 1, 0 if ok else 2, "" if ok else "AssertionError")

    def smoke(self, root, frames=600):
        return stacks.SmokeResult(True, ["menu", "play"], frames)

    def test_file(self, task_id):
        return f"tests/{task_id}.py"

    def set_title(self, root, title):
        pass

    def set_palette(self, root, palette):
        pass

    def syntax_error(self, path):
        text = path.read_text() if path.is_file() else ""
        return "SyntaxError: broken" if "BROKEN" in text else None

    def play_command(self, root):
        return "python main.py"


def make_studio(tmp_path, monkeypatch, plan=None, code=None, helper=None, review=None, budget=None, flags=("test_first", "review")):
    pr = Project(tmp_path)
    pr.save(State("make me a game like Shadow Fight 2", "game", "pygame", reference="Shadow Fight 2"))
    stack = FakeStack()
    calls = {"tests": [], "code": [], "review": []}
    monkeypatch.setattr(agents, "research", lambda root, state, team: "notes")
    monkeypatch.setattr(agents, "plan", lambda root, state, team, st: plan or good_plan())
    monkeypatch.setattr(agents, "design", lambda root, state, team, st: {
        "palette": [{"name": "ink", "hex": "#101820", "use": "background"}], "typography": "", "shapes": "",
        "layout": "", "motion": "", "avoid_look": [], "must": [], "avoid": []})
    monkeypatch.setattr(agents, "approach", lambda root, state, task, team: f"Plan for {task.id}\nChanges:\n- x.py")
    monkeypatch.setattr(agents, "diagnose", lambda root, state, task, team, failure, previous="": (
        f"Root cause: {task.id} is broken" + (" (deeper)" if previous else "") + "\nEvidence: x.py:1\nFix:\n1. fix it"))

    def write_tests(root, state, task, team, st, feedback=""):
        calls["tests"].append(task.id + (" (rewrite)" if feedback else ""))
        if feedback:  # the rewritten test is fair: this task's check no longer needs the impossible thing
            stack.green.discard(task.id)
        else:
            stack.green.add(task.id)  # from now on this task's "tests" need its file
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / f"{task.id}.py").write_text("# test\n")
        return True

    def write_code(root, state, task, team, st, feedback="", model=None):
        calls["code"].append((task.id, feedback[:20]))
        calls.setdefault("models", []).append(model)
        (code or (lambda t, n: True))(task.id, len([c for c in calls["code"] if c[0] == task.id])) and \
            (root / f"{task.id}.txt").write_text("GREEN")

    def do_review(root, state, task, team, diff, summary):
        calls["review"].append(task.id)
        return (review or (lambda t, n: (True, [])))(task.id, len([r for r in calls["review"] if r == task.id]))

    monkeypatch.setattr(agents, "write_tests", write_tests)
    monkeypatch.setattr(agents, "write_code", write_code)
    monkeypatch.setattr(agents, "review", do_review)
    monkeypatch.setattr("jarvis.studio.loop.commit", lambda root, msg: None)
    monkeypatch.setattr("jarvis.studio.loop.diff", lambda root: "diff")
    sent = []
    team = agents.Team(None, None, say=lambda s: None,
                       plugins=[plugins.load([f])[0] for f in flags] + [plugins.PhoneUpdates(lambda t, b: sent.append(t))])
    return Studio(pr, team, stack, helper, budget or Budget(rounds=3)), calls, sent


def test_full_run_tests_first_then_code_then_review(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch)
    state = studio.run()
    assert state.phase == "done" and [t.status for t in state.tasks] == ["done"] * 3
    assert calls["tests"] == ["T1", "T2", "T3"] and [c[0] for c in calls["code"]] == ["T1", "T2", "T3"]
    assert calls["review"] == ["T1", "T2", "T3"]
    assert calls["code"][0][1].startswith("Tests:")  # the coder's first round sees the failing new tests
    assert any("Plan ready" in s for s in sent) and any("Feature done" in s for s in sent) and any(
        "Game ready" in s for s in sent)
    assert (tmp_path / "README.md").is_file() and (tmp_path / "docs" / "project.html").is_file()
    assert [e["event"] for e in Project(tmp_path).events()][:2] == ["research_done", "plan_ready"]


class FakeHelper:
    name = "Claude Code"

    def __init__(self, fixes=True):
        self.fixes, self.calls = fixes, []

    def available(self):
        return True

    def ask(self, root, task_text, failure, stack):
        self.calls.append(task_text.split(":")[0])
        if self.fixes:
            tid = task_text.split(":")[0].split()[-1]
            (root / f"{tid}.txt").write_text("GREEN")
        from jarvis.studio.helper import HelperResult
        return HelperResult(self.fixes, "fixed it" if self.fixes else "couldn't")


def test_stuck_task_goes_to_the_helper(tmp_path, monkeypatch):
    helper = FakeHelper(fixes=True)
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T2", helper=helper)
    state = studio.run()
    assert [t.status for t in state.tasks] == ["done"] * 3
    assert helper.calls == ["Task T2"] and state.task("T2").rounds == 3 and state.helper_calls == 1
    assert any("Asking Claude Code" in s for s in sent)


def test_still_stuck_blocks_the_task_and_skips_its_dependents(tmp_path, monkeypatch):
    helper = FakeHelper(fixes=False)
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T2", helper=helper,
                                      budget=Budget(rounds=2, helper_per_task=1))
    state = studio.run()
    # the final integration step tries once more to make everything pass (task INT), then gives up honestly
    assert [(t.id, t.status) for t in state.tasks] == [("T1", "done"), ("T2", "blocked"), ("T3", "skipped"),
                                                       ("INT", "blocked")]
    assert state.phase == "done" and any("Task blocked" in s for s in sent)
    assert "2 blocked" in state.message and "Some checks still fail" in state.message


def test_review_changes_then_approval(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(
        tmp_path, monkeypatch, review=lambda tid, n: (n > 1 or tid != "T1", [] if n > 1 or tid != "T1" else ["rename x"]))
    state = studio.run()
    assert calls["review"].count("T1") == 2 and state.task("T1").reviews == 1
    assert any(tid == "T1" and fb.startswith("The reviewer asked") for tid, fb in calls["code"])
    assert state.task("T1").status == "done"


def test_stop_file_pauses_and_resume_finishes(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch)
    real = studio.run_task

    def stop_after_first(state, task):
        real(state, task)
        (tmp_path / ".studio" / "STOP").write_text("")

    studio.run_task = stop_after_first
    state = studio.run()
    assert state.phase == "build" and state.message.startswith("Paused") and state.counts()["done"] == 1
    assert any("Studio paused" in s for s in sent)
    studio2, calls2, _ = make_studio(tmp_path, monkeypatch)
    Project(tmp_path).save(state)  # make_studio reset it; put the paused state back
    final = studio2.run()
    assert final.phase == "done" and calls2["code"] and calls2["code"][0][0] == "T2"


def test_time_budget_pauses(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch, budget=Budget(hours=1))
    ticks = iter([0, 0, 4000, 4000, 4000, 4000])
    studio.clock = lambda: next(ticks, 4000)
    state = studio.run()
    assert "budget" in state.message and state.phase != "done"


# ---------------------------------------------------------------- agents' guard rails

def test_coder_may_not_edit_tests_or_docs():
    assert agents._coder_may_write("tests/test_t1.py").startswith("Tests are fixed")
    assert agents._coder_may_write("docs/PRD.md") and agents._coder_may_write(".studio/state.json")
    assert agents._coder_may_write("game/fighter.py") is None


def test_planner_retries_with_problems(tmp_path):
    class Planner:
        def __init__(self):
            self.seen = []
            self.replies = [good_plan(title="Shadow Brawl"), good_plan()]

        def json(self, messages, schema):
            self.seen.append(messages)
            return self.replies.pop(0)

    planner = Planner()
    team = agents.Team(planner, None, say=lambda s: None)
    state = State("make me a game like Shadow Fight 2", "game", "pygame", reference="Shadow Fight 2")
    out = agents.plan(tmp_path, state, team, stacks.get("pygame"))
    assert out["title"] == "Ink Duel" and "reuses the reference" in planner.seen[1][-1]["content"]
    planner.replies = [good_plan(title="Shadow")] * 3
    with pytest.raises(agents.PlanError):
        agents.plan(tmp_path, state, team, stacks.get("pygame"))


def test_tester_can_only_write_its_test_file(tmp_path):
    class Coder:
        def __init__(self):
            self.n = 0

        def chat(self, messages, specs):
            self.n += 1
            if self.n == 1:
                return {"content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": {
                    "path": "game/x.py", "content": "x"}}}]}
            if self.n == 2:
                return {"content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": {
                    "path": "tests/test_t1.py", "content": "def test_x():\n    assert True\n"}}}]}
            return {"content": "done"}

    stacks.get("pygame").scaffold(tmp_path, "X")
    state = State("r", "n", "pygame", plan=good_plan())
    ok = agents.write_tests(tmp_path, state, Task("T1", "t", acceptance=["a"]), agents.Team(None, Coder(), say=lambda s: None),
                            stacks.get("pygame"))
    assert ok and not (tmp_path / "game" / "x.py").exists() and (tmp_path / "tests" / "test_t1.py").is_file()


def test_helper_builds_a_safe_command(tmp_path, monkeypatch):
    from jarvis.studio import helper as h

    seen = {}

    class Done:
        returncode, stdout, stderr = 0, json.dumps({"result": "Fixed the round timer.", "is_error": False}), ""

    def runner(argv, **kw):
        seen.update(argv=argv, **kw)
        return Done()

    monkeypatch.setattr(h, "claude_executable", lambda: ["claude.exe"])
    out = h.ClaudeCodeHelper(runner=runner).ask(tmp_path, "Task T3: Rounds", "AssertionError", stacks.get("pygame"))
    assert out.ok and out.summary == "Fixed the round timer."
    assert seen["argv"][:2] == ["claude.exe", "-p"] and "acceptEdits" in seen["argv"]
    assert seen["cwd"] == tmp_path and "Task T3: Rounds" in seen["input"] and "AssertionError" in seen["input"]
    assert "Bash(python -m pytest:*)" in seen["argv"] and "Bash" not in [a for a in seen["argv"] if a == "Bash"]


# ---------------------------------------------------------------- the real stacks

def test_pygame_skeleton_passes_its_tests_and_smoke_run(tmp_path):
    st = stacks.get("pygame")
    st.scaffold(tmp_path, "Untitled Game")
    st.set_title(tmp_path, "Ink's Duel")
    assert "TITLE = \"Ink's Duel\"" in (tmp_path / "game" / "config.py").read_text()
    assert st.run_tests(tmp_path).ok
    smoke = st.smoke(tmp_path, 200)
    assert smoke.ok and smoke.scenes[:2] == ["menu", "play"] and smoke.frames == 200


def test_pygame_smoke_reports_a_crash(tmp_path):
    st = stacks.get("pygame")
    st.scaffold(tmp_path, "X")
    scenes = tmp_path / "game" / "scenes.py"
    scenes.write_text(scenes.read_text().replace('return "menu"', "return 1 / 0").replace(
        'if "back" in inp.pressed:', "if True:"))
    smoke = st.smoke(tmp_path, 200)
    assert not smoke.ok and "ZeroDivisionError" in smoke.error


@pytest.mark.skipif(stacks.WebStack().available() is not None, reason="needs node and Edge/Chrome")
def test_web_skeleton_passes_node_tests_and_headless_smoke(tmp_path):
    st = stacks.get("web")
    st.scaffold(tmp_path, "Untitled Game")
    st.set_title(tmp_path, 'Ink "Duel"')
    assert 'export const TITLE = "Ink \\"Duel\\"";' in (tmp_path / "src" / "config.js").read_text()
    assert "<title>Ink &quot;Duel&quot;</title>" in (tmp_path / "index.html").read_text()
    tests = st.run_tests(tmp_path)
    assert tests.ok and tests.passed == 3
    smoke = st.smoke(tmp_path, 200)
    assert smoke.ok and smoke.scenes[:2] == ["menu", "play"]


# ---------------------------------------------------------------- real models

@pytest.mark.llm
def test_planner_makes_a_valid_plan_with_the_real_model(tmp_path, ollama):
    """qwen3 plans a small game: valid JSON, checked task graph, testable criteria, an original title."""
    from jarvis.config import Settings
    from jarvis.core.llm import OllamaClient

    s = Settings.from_env()
    planner = OllamaClient(s.ollama_url, s.studio_planner_model, timeout=900)
    team = agents.Team(planner, None, say=print)
    state = State("make me a small game like Pong", "game", "pygame", reference="Pong")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "reference.md").write_text("# Pong\nTwo paddles, a ball, first to 11 points wins.\n")
    plan = agents.plan(tmp_path, state, team, stacks.get("pygame"))
    assert validate_plan(plan, "Pong") == []
    assert 3 <= len(plan["tasks"]) <= 25 and "pong" not in plan["title"].lower()
    assert all(t["acceptance"] for t in plan["tasks"])


def test_a_round_that_writes_no_code_does_not_count(tmp_path, monkeypatch):
    # seen live, 7 Nov 2026: the skeleton's own tests passed, so empty tasks were marked done
    from jarvis.studio import loop as studio_loop

    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: False,
                                      budget=Budget(rounds=2), flags=("review",))
    studio.stack.green.clear()  # every check passes...
    studio_loop.git(tmp_path, "init", "-q")
    studio_loop.git(tmp_path, *("add", "-A"))
    studio_loop.git(tmp_path, "commit", "-q", "-m", "start")
    monkeypatch.setattr(studio_loop, "commit", lambda root, msg: (studio_loop.git(root, "add", "-A"),
                                                                 studio_loop.git(root, "commit", "-q", "-m", msg)))
    state = studio.run()
    assert state.task("T1").status == "blocked" and state.task("T1").rounds == 2  # ...but nothing was written
    assert any(c[1].startswith("No code was written") for c in calls["code"] if c[0] == "T1")
    import tempfile

    with tempfile.TemporaryDirectory() as plain:
        assert studio_loop.changed_code(plain) is None  # not a git repo: the guard stays out of the way


def test_stalled_rounds_get_a_second_opinion_then_the_helper(tmp_path, monkeypatch):
    helper = FakeHelper(fixes=True)
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T1", helper=helper,
                                      budget=Budget(rounds=6), flags=())
    studio.team.planner = "qwen3"  # stands in for the planner model
    studio.stack.green.add("T1")  # T1's check fails until its file exists (no tester in this run)
    state = studio.run()
    t1_models = [m for (tid, _), m in zip(calls["code"], calls["models"]) if tid == "T1"]
    # rounds 1-3 fail the same way (stalled twice) -> round 4 is qwen3's -> then Claude Code, not rounds 5-6
    assert t1_models == [None, None, None, "qwen3"] and state.task("T1").rounds == 4
    assert helper.calls == ["Task T1"] and state.task("T1").status == "done"


def test_research_reads_pages_in_python_then_the_model_writes(tmp_path, monkeypatch):
    from jarvis.coder import tools as coder_tools

    searched, read = [], []
    monkeypatch.setattr(coder_tools.Workspace, "search_web", lambda self, q: searched.append(q) or (
        "Guide\nhttps://guides.example/sf\nsnippet\n\nVideo\nhttps://www.youtube.com/watch?v=1\nx"))
    monkeypatch.setattr(coder_tools.Workspace, "read_webpage", lambda self, url: read.append(url) or (
        "Duels are best of three rounds. " * 40))

    class Planner:
        def chat(self, messages, specs):
            self.messages = messages
            return {"content": "# Reference: Shadow Fight 2\n\n## Mechanics\nBest of three rounds.\n" + "x" * 400}

    planner = Planner()
    state = State("make me a game like Shadow Fight 2", "game", "pygame", reference="Shadow Fight 2")
    out = agents.research(tmp_path, state, agents.Team(planner, None, say=lambda s: None))
    assert len(searched) == 3 and read == ["https://guides.example/sf"]  # one page per url, no video sites
    assert "best of three rounds" in planner.messages[1]["content"].lower()
    text = (tmp_path / "docs" / "reference.md").read_text(encoding="utf-8")
    assert text.startswith("# Reference: Shadow Fight 2") and "- https://guides.example/sf" in text
    assert out == "Studied 1 pages about Shadow Fight 2."


def test_studio_shares_jarvis_model_unless_told_otherwise():
    # one model on the GPU keeps Jarvis answering during a build (7 Nov 2026: 300 s timeouts otherwise)
    from jarvis.config import Settings
    from jarvis.studio.__main__ import make_team

    base = dict(ollama_model="qwen3:8b", studio_planner_model="qwen3:8b", studio_plugins=())
    team = make_team(Settings(**base, studio_coder_model=""))
    assert team.coder is team.planner and team.planner.keep_alive == -1
    team = make_team(Settings(**base, studio_coder_model="qwen2.5-coder:7b"))
    assert team.coder is not team.planner and team.coder.model == "qwen2.5-coder:7b"


# ---------------------------------------------------------------- playtest

def test_playtest_maps_scenes_values_and_drawing_of_a_real_pygame_game(tmp_path):
    from jarvis.studio import playtest

    stacks.get("pygame").scaffold(tmp_path, "X")
    scenes = tmp_path / "game" / "scenes.py"
    scenes.write_text(scenes.read_text().replace(
        "class PlayScene(Scene):\n    name = \"play\"\n",
        "class PlayScene(Scene):\n    name = \"play\"\n\n    def enter(self):\n        self.x, self.health = 100, 10\n"
    ).replace('        if "back" in inp.pressed:\n            return "menu"\n        return None\n\n    def draw(self, surface):\n'
              '        surface.fill((30, 30, 30))',
              '        if "right" in inp.held:\n            self.x += 1\n        if "pause" in inp.pressed:\n'
              '            raise RuntimeError("pause is broken")\n        if "back" in inp.pressed:\n            return "menu"\n'
              '        return None\n\n    def draw(self, surface):\n        import pygame\n\n        surface.fill((30, 30, 30))\n'
              '        pygame.draw.rect(surface, (200, 0, 0), (self.x, 100, 40, 80))'))
    report = playtest.run(tmp_path, stacks.get("pygame"), frames=600)
    assert report.ok, report.error
    d = report.data
    assert d["explore"]["reachable"] == ["menu", "play"] and d["explore"]["paths"]["play"] == "confirm"
    play = d["run"]["scenes"]["play"]
    assert "scene.x" in play["changed"] and "scene.health" in play["unchanged"]
    assert play["draws_per_frame"] == {"rect": 1.0} and play["distinct_screens"] > 1
    assert Path(d["shots"]["play"]).is_file()
    assert any(c["probe"] == "pause" and "pause is broken" in c["error"] for c in d["crashes"])
    text = playtest.summary(report)
    assert "NEVER changed: scene.health" in text and "rect 1.0" in text
    plan = good_plan(scenes=["menu", "play", "shop"], transitions=[{"from": "play", "to": "shop", "trigger": "confirm"}])
    titles = [p["title"] for p in playtest.certain_problems(report, plan)]
    assert "Make the 'shop' scene reachable" in titles and any("crash in scene 'play'" in t for t in titles)


@pytest.mark.skipif(stacks.WebStack().available() is not None, reason="needs node and Edge/Chrome")
def test_playtest_runs_web_games_too(tmp_path):
    from jarvis.studio import playtest

    stacks.get("web").scaffold(tmp_path, "X")
    report = playtest.run(tmp_path, stacks.get("web"), frames=400)
    assert report.ok, report.error
    assert report.data["explore"]["reachable"] == ["menu", "play"]
    assert report.data["run"]["scenes"]["play"]["draws_per_frame"] == {"fillRect": 1}
    assert Path(report.data["shots"]["play"]).is_file()


def test_frozen_screens_and_a_failed_playtest_become_problems():
    from jarvis.studio import playtest

    rep = playtest.Report(True, {"explore": {"reachable": ["menu", "play"]}, "run": {"scenes": {
        "play": {"samples": 20, "distinct_screens": 1}, "menu": {"samples": 3, "distinct_screens": 1}}}})
    assert [p["title"] for p in playtest.certain_problems(rep, good_plan())] == ["Make the 'play' scene respond on screen"]
    failed = playtest.certain_problems(playtest.Report(False, error="Traceback: boom"), good_plan())
    assert failed[0]["title"].startswith("Make the playtest run") and "boom" in failed[0]["evidence"]


def test_analyst_problems_are_kept_unless_already_filed():
    from jarvis.studio import playtest

    class Planner:
        def json(self, messages, schema):
            self.ask = messages[1]["content"]
            return {"problems": [
                {"title": "Shop and Pause Scenes Not Reachable", "scene": "play", "evidence": "x", "kind": "code",
                 "acceptance": ["a"]},
                {"title": "Player health never changes", "scene": "play", "evidence": "scene.health never changed",
                 "kind": "code", "acceptance": ["a hit lowers health"]},
                {"title": "Only rectangles are drawn", "scene": "play", "evidence": "rect 4.0", "kind": "visual",
                 "acceptance": ["fighters drawn as characters"]},
                {"title": "", "scene": "", "evidence": "", "kind": "code", "acceptance": []}]}

    planner = Planner()
    state = State("r", "n", "pygame", plan=good_plan())
    filed = [{"title": "Make the 'shop' scene reachable"}]
    out = playtest.analyse(state, playtest.Report(True, {}), agents.Team(planner, None, say=lambda s: None), filed)
    assert [(p["title"], p["kind"]) for p in out] == [("Player health never changes", "code"),
                                                      ("Only rectangles are drawn", "visual")]
    assert "Make the 'shop' scene reachable" in planner.ask and "Art style: ink strokes" in planner.ask
    tasks = playtest.tasks_for(out, State("r", "n", "pygame", tasks=[Task("P1", "old")]))
    assert [(t.id, t.kind, t.feature) for t in tasks] == [("P2", "code", "PT"), ("P3", "visual", "PT")]


def test_loop_playtests_files_fixes_builds_them_and_playtests_again(tmp_path, monkeypatch):
    from jarvis.studio import playtest

    rounds = []
    monkeypatch.setattr(playtest, "run", lambda root, stack, frames=4000: playtest.Report(True, {
        "explore": {"reachable": ["menu", "play"], "edges": []}, "run": {"scenes": {}}, "shots": {}}))
    monkeypatch.setattr(playtest, "analyse", lambda state, rep, team, filed: rounds.append(state.playtests) or (
        [{"title": "Hits never land", "scene": "play", "evidence": "health", "kind": "code", "acceptance": ["hit"]},
         {"title": "Draw real fighters", "scene": "play", "evidence": "rect", "kind": "visual",
          "acceptance": ["shapes"]}] if state.playtests == 1 else []))
    studio, calls, sent = make_studio(tmp_path, monkeypatch, flags=("test_first", "review", "playtest"))
    state = studio.run()
    assert rounds == [1, 2] and state.playtests == 2 and state.phase == "done"
    assert [(t.id, t.status, t.kind) for t in state.tasks[3:]] == [("P1", "done", "code"), ("P2", "done", "visual")]
    assert "P1" in calls["tests"] and "P2" not in calls["tests"]  # visual tasks skip test-first
    assert any(f["id"] == "PT" for f in state.plan["features"]) and state.last_playtest["problems"] == []
    assert any("Playtest" in s for s in sent)
    assert "Playtest (round 2)" in (tmp_path / "docs" / "project.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- getting unstuck without Claude Code

def test_a_stuck_task_is_split_into_smaller_steps_instead_of_asking_for_help(tmp_path, monkeypatch):
    helper = FakeHelper(fixes=True)
    # T2 fails every round until it has been split (its 4th try comes after its steps are done)
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T2" or n > 3, helper=helper,
                                      budget=Budget(rounds=3))
    monkeypatch.setattr(agents, "check_tests", lambda *a, **k: (False, "the test is fair"))
    asked = []
    monkeypatch.setattr(agents, "split", lambda root, state, task, team, failure: asked.append((task.id, failure[:6])) or [
        {"title": "Hit box from attack data", "files": ["game/combat.py"], "acceptance": ["hitbox in front"]},
        {"title": "Resolve a hit", "files": ["game/combat.py"], "acceptance": ["damage applied once"]}])
    state = studio.run()
    assert [(t.id, t.status, t.depth) for t in state.tasks] == [
        ("T1", "done", 0), ("T2a", "done", 1), ("T2b", "done", 1), ("T2", "done", 0), ("T3", "done", 0)]
    assert state.task("T2b").depends_on == ["T1", "T2a"] and state.task("T2").depends_on == ["T1", "T2a", "T2b"]
    assert asked == [("T2", "Root c")] and helper.calls == [] and state.helper_calls == 0  # split sees the diagnosis
    # T2's own tests were parked while its steps ran, then came back to check the whole
    assert (tmp_path / "tests" / "T2.py").is_file() and not list((tmp_path / "tests").glob("*.parked"))
    assert "T2 was too big for one go; split into T2a Hit box from attack data" in Project(tmp_path).read_doc("memory.md")
    board = Project(tmp_path).read_doc("tasks.md")
    assert "    - ✅ **↳: T2a Hit box from attack data** `done`" in board


def test_a_wrong_test_is_rewritten_before_splitting(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T1", budget=Budget(rounds=3))
    checked = []
    monkeypatch.setattr(agents, "check_tests", lambda root, state, task, team, st, failure: checked.append(task.id) or (
        True, "it asserts take_damage but the task says take_hit"))
    monkeypatch.setattr(agents, "split", lambda *a: pytest.fail("should not need splitting"))
    state = studio.run()
    assert checked == ["T1"] and "T1 (rewrite)" in calls["tests"] and state.task("T1").status == "done"
    assert state.task("T1").tests_checked and "its test was wrong" in Project(tmp_path).read_doc("memory.md")


def test_claude_code_is_a_small_last_resort(tmp_path, monkeypatch):
    helper = FakeHelper(fixes=False)
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: False, helper=helper,
                                      budget=Budget(rounds=1, split_depth=0, helper_total=2), flags=())
    for t in ("T1", "T2", "T3"):
        studio.stack.green.add(t)
    monkeypatch.setattr(agents, "check_tests", lambda *a, **k: (False, "fair"))
    state = studio.run()
    assert state.helper_calls == 2 and len(helper.calls) == 2  # never more than the budget
    from jarvis.config import Settings

    assert Settings().studio_helper_calls == 3 and Budget().helper_per_task == 1


def test_split_into_numbers_and_chains_the_steps():
    from jarvis.studio.project import split_into

    state = State("r", "n", "pygame", tasks=tasks_from_plan(good_plan()))
    subs = split_into(state, state.task("T3"), [{"title": "a", "files": ["x.py"], "acceptance": ["one"]},
                                               {"title": "b", "files": [], "acceptance": ["two"]}])
    assert [t.id for t in state.tasks] == ["T1", "T2", "T3a", "T3b", "T3"]
    deeper = split_into(state, subs[0], [{"title": "c", "files": [], "acceptance": ["x"]},
                                         {"title": "d", "files": [], "acceptance": ["y"]}])
    assert [t.id for t in deeper] == ["T3a1", "T3a2"] and deeper[0].depth == 2 and deeper[0].parent == "T3a"
    assert next_task(state).id == "T1"


def test_agents_get_the_rules_design_and_memory(tmp_path):
    pr = Project(tmp_path)
    pr.write_doc("Rules.md", "# X — Rules\n\nStrict: keep logic out of drawing.")
    pr.write_doc("design.md", "# X — Design\n\n| Colour | Hex | Used for |\n|---|---|---|\n| ink | #101820 | background |\n"
                              "\n## It must NOT look like\n- grey boxes")
    pr.remember("bug", "the round timer counted up")
    code_ctx = agents.context(tmp_path, Task("T1", "Combat rules"))
    assert "keep logic out of drawing" in code_ctx and "| ink | #101820 |" in code_ctx and "grey boxes" not in code_ctx
    assert "the round timer counted up" in code_ctx
    visual = agents.context(tmp_path, Task("P5", "Draw the fighters", kind="visual"))
    assert "grey boxes" in visual


def test_palette_goes_into_the_game_config(tmp_path):
    for name, check in (("pygame", "'ink': (16, 24, 32),"), ("web", '"ink": "#101820",')):
        root = tmp_path / name
        st = stacks.get(name)
        st.scaffold(root, "X")
        pal = [{"name": "Ink", "hex": "#101820", "use": "background"}]
        st.set_palette(root, pal)
        st.set_palette(root, pal)  # again: replaced, not duplicated
        text = (root / st.config_file).read_text()
        assert text.count("palette (design.md)") == 1 and check in text
    assert st.run_tests(tmp_path / "web").ok and stacks.get("pygame").run_tests(tmp_path / "pygame").ok


def test_decisions_go_to_the_thinker_with_thinking_on_and_review_stays_fast(tmp_path):
    class Model:
        def __init__(self, name):
            self.name, self.calls = name, []

        def json(self, messages, schema, think=False):
            self.calls.append(think)
            if "steps" in schema["properties"]:
                return {"steps": [{"title": "a", "files": [], "acceptance": ["x"]},
                                  {"title": "b", "files": [], "acceptance": ["y"]}]}
            return {"approve": True, "problems": []}

    fast, big = Model("8b"), Model("14b")
    team = agents.Team(fast, fast, say=lambda s: None, thinker=big)
    state = State("r", "n", "pygame", plan=good_plan())
    assert len(agents.split(tmp_path, state, Task("T2", "Combat", acceptance=["hits land"]), team, "AssertionError")) == 2
    agents.review(tmp_path, state, Task("T2", "Combat", acceptance=["hits land"]), team, "diff", "3 passed")
    assert big.calls == [True] and fast.calls == [False]  # split thought hard on the thinker; review stayed fast
    alone = agents.Team(fast, fast, say=lambda s: None)  # no thinker set: the planner, still with thinking on
    agents.split(tmp_path, state, Task("T2", "Combat", acceptance=["x"]), alone, "x")
    assert fast.calls[-1] is True


def test_the_team_gets_a_thinker_only_when_one_is_configured():
    from jarvis.config import Settings
    from jarvis.studio.__main__ import make_team

    base = dict(ollama_model="qwen3:8b", studio_planner_model="qwen3:8b", studio_plugins=())
    plain = make_team(Settings(**base))
    assert plain.thinker is plain.planner  # no thinker configured: the pinned model thinks, no swaps
    team = make_team(Settings(**base, studio_thinker_model="qwen3:14b"))
    assert team.thinker.model == "qwen3:14b" and team.thinker.keep_alive == "5m" and team.planner.keep_alive == -1


def test_each_task_gets_an_approach_and_failed_rounds_get_a_root_cause_diagnosis(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch, code=lambda tid, n: tid != "T1" or n >= 3,
                                      budget=Budget(rounds=4))
    state = studio.run()
    assert state.task("T1").approach.startswith("Plan for T1") and state.task("T1").status == "done"
    t1 = [fb for tid, fb in calls["code"] if tid == "T1"]
    assert t1[0].startswith("Tests:")                      # round 1: the new tests' raw failure
    assert t1[1] == "Root cause: T1 is br"     # round 2: a diagnosis first (the fake keeps 20 characters)
    events = [e for e in Project(tmp_path).events() if e["event"] == "diagnosis"]
    assert events and events[0]["task"] == "T1"
    assert "T1 approach: Plan for T1" in Project(tmp_path).read_doc("memory.md")
    assert "Approach (planned before coding):\nPlan for T1" in agents.describe_task(state.task("T1"))


# ---------------------------------------------------------------- detailed graphs

CODE = {
    "game/config.py": "SPEED = 3\n",
    "game/fighter.py": "from game import config\n\n\nclass Fighter:\n    def __init__(self):\n        self.health = 100\n\n"
                       "    def take_hit(self, n):\n        self.health -= n\n        self.flash()\n\n    def flash(self):\n"
                       "        pass\n",
    "game/combat.py": "from game.fighter import Fighter\n\n\ndef resolve(a, b):\n    b.take_hit(10)\n    return Fighter()\n",
    "game/scenes.py": "from game import combat\nfrom game.engine import Scene\n\n\nclass PlayScene(Scene):\n"
                      "    def update(self, inp, dt):\n        combat.resolve(None, None)\n        return None\n",
}


def test_code_map_reads_classes_functions_and_calls(tmp_path):
    stacks.get("pygame").scaffold(tmp_path, "X")
    for rel, text in CODE.items():
        (tmp_path / rel).write_text(text)
    cmap = graph.code_map(tmp_path, "pygame")
    assert cmap["modules"]["game.fighter"]["classes"] == {"Fighter": ["__init__", "take_hit", "flash"]}
    assert cmap["modules"]["game.combat"]["functions"] == ["resolve"]
    calls = set(cmap["calls"])
    assert ("game.fighter", "Fighter.take_hit", "game.fighter", "Fighter.flash") in calls       # self.method()
    assert ("game.combat", "resolve", "game.fighter", "Fighter") in calls                        # imported class
    assert ("game.scenes", "PlayScene.update", "game.combat", "resolve") in calls                # module alias
    nodes, edges = graph.frame_flow(cmap)
    assert nodes[:3] == ["main loop", "Game.step", "PlayScene.update"] and ("PlayScene.update", "resolve", "") in edges
    assert ("resolve", "Fighter", "") in edges and nodes[-1] == "draw"
    out = graph.code_map_svg(cmap)
    assert out.startswith("<svg") and "class Fighter: __init__, take_hit, flash" in out and "1 call" in out
    mm = graph.code_map_mermaid(cmap)
    assert "class Fighter {" in mm and "+take_hit()" in mm and "game_combat ..> Fighter : calls" in mm


def test_js_code_map_follows_imports(tmp_path):
    stacks.get("web").scaffold(tmp_path, "X")
    cmap = graph.code_map(tmp_path, "web")
    assert "Game" in cmap["modules"]["src/engine"]["classes"] and "seeded" in cmap["modules"]["src/engine"]["functions"]
    assert ("src/main", "(module)", "src/engine", "Game") in set(cmap["calls"])


def test_task_graph_and_scene_edges_show_status_and_playtest_findings():
    state = State("r", "n", "pygame", plan=good_plan(scenes=["menu", "play", "shop"], transitions=[
        {"from": "menu", "to": "play", "trigger": "confirm"}, {"from": "play", "to": "shop", "trigger": "confirm"}]),
        tasks=tasks_from_plan(good_plan()))
    state.task("T1").status = "done"
    nodes, edges, classes = graph.task_graph(state)
    assert classes["T1 Fighter health"] == "st-done" and ("T1 Fighter health", "T2 Combat", "") in edges
    edges, cls = graph.scene_edge_classes(state.plan, [["menu", "play", "confirm"], ["play", "menu", "back"]])
    assert cls == {("menu", "play"): "walked", ("play", "shop"): "missing", ("play", "menu"): "extra"}
    svg = graph.svg(["menu", "play", "shop"], edges, edge_classes=cls)
    assert 'class="edge missing"' in svg and 'class="edge extra"' in svg


# ---------------------------------------------------------------- sketches and colours

PALETTE = [{"name": "Ink", "hex": "#101820", "use": "background"}, {"name": "Kael", "hex": "#1c2a6a", "use": "player"},
           {"name": "Text", "hex": "#3a3a3a", "use": "UI text"}, {"name": "Gold", "hex": "#ffd23f", "use": "accent"}]


def test_contrast_is_fixed_before_anything_is_built():
    from jarvis.studio import colors

    assert round(colors.contrast("#000000", "#ffffff"), 1) == 21.0
    fixed, notes = colors.fix_contrast(PALETTE)
    by = {c["name"]: c["hex"] for c in fixed}
    assert colors.contrast(by["Text"], "#101820") >= 4.5 and colors.contrast(by["Kael"], "#101820") >= 3.0
    assert by["Gold"] == "#ffd23f" and by["Ink"] == "#101820"   # already fine / the background itself
    assert len(notes) == 2 and notes[0].startswith("Kael #1c2a6a →")


def test_screens_are_cleaned_and_sketched(tmp_path):
    from jarvis.studio import sketch

    screens = agents.clean_screens([
        {"scene": "Play", "elements": [{"kind": "character", "label": "Kael", "x": 20, "y": 50, "w": 10, "h": 40,
                                        "color": "kael"},
                                       {"kind": "laser", "label": "?", "x": 95, "y": -5, "w": 30, "h": 10, "color": "pink"},
                                       {"kind": "bar", "label": "HP", "x": "bad", "y": 0, "w": 1, "h": 1, "color": "Gold"}]},
        {"scene": "play", "elements": [{"kind": "text", "label": "dupe", "x": 1, "y": 1, "w": 1, "h": 1, "color": "Ink"}]},
        {"scene": "menu", "elements": []}], PALETTE, ["menu", "play"])
    assert [s["scene"] for s in screens] == ["play"]  # one per scene; empty ones dropped
    els = screens[0]["elements"]
    assert els[0]["color"] == "Kael" and els[1]["kind"] == "panel" and els[1]["color"] == "Kael"  # unknown -> fallback
    assert els[1]["x"] == 95 and els[1]["w"] == 5 and els[1]["y"] == 0 and len(els) == 2      # clamped; bad numbers out
    names = sketch.write_all(tmp_path, {"palette": PALETTE, "screens": screens}, phone_too=True)
    assert names == ["play.svg", "play-phone.svg"]
    svg = (tmp_path / "sketches" / "play.svg").read_text()
    assert svg.startswith("<svg") and "<circle" in svg and "Kael" in svg and 'fill="#101820"' in svg
    assert "character 'Kael' at (20,50) 10x40 Kael" in sketch.describe({"screens": screens})
    phone = sketch.stacked(screens[0])
    assert all(e["x"] == 6.0 and e["w"] == 88.0 for e in phone["elements"])


def test_design_md_and_page_show_the_concepts(tmp_path):
    pr = Project(tmp_path)
    stacks.get("pygame").scaffold(tmp_path, "X")
    screens = [{"scene": "play", "elements": [{"kind": "character", "label": "Kael", "x": 20, "y": 50, "w": 10,
                                               "h": 40, "color": "Kael"}]}]
    state = State("r", "n", "pygame", plan=good_plan(design={"palette": PALETTE, "screens": screens, "adjusted": ["x"]}),
                  tasks=tasks_from_plan(good_plan()))
    state.last_playtest = {"round": 1, "shots": {"play": "play.png"}, "problems": [], "edges": [], "reachable": []}
    docs.write_all(pr, state, "python main.py")
    design = pr.read_doc("design.md")
    assert "![play](sketches/play.svg)" in design and "## Contrast fixes" in design
    page = pr.read_doc("project.html")
    assert "Concept vs built" in page and 'src="sketches/play.svg"' in page and "../.studio/playtest/play.png" in page
    assert "Code map" in page and "One frame" in page and "Task graph" in page
    assert (tmp_path / "docs" / "sketches" / "play.svg").is_file()


def test_vision_review_reads_each_screenshot_against_the_design(tmp_path):
    from jarvis.studio import vision

    (tmp_path / "play.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "menu.png").write_bytes(b"\x89PNG fake")

    class Vision:
        def __init__(self):
            self.seen = []

        def json(self, messages, schema):
            self.seen.append(messages[0])
            if "'play'" in messages[0]["content"]:
                return {"problems": [{"title": "Fighters are plain rectangles", "evidence": "two coloured boxes",
                                      "acceptance": ["fighters drawn with heads and limbs"]}]}
            return {"problems": []}

    v = Vision()
    design = {"palette": PALETTE, "shapes": "brush strokes", "avoid_look": ["grey boxes"],
              "screens": [{"scene": "play", "elements": [{"kind": "character", "label": "Kael", "x": 20, "y": 50,
                                                          "w": 10, "h": 40, "color": "Kael"}]}]}
    out = vision.review({"menu": str(tmp_path / "menu.png"), "play": str(tmp_path / "play.png")}, design,
                        good_plan(scenes=["menu", "play"]), v)
    assert [m["content"].split("'")[1] for m in v.seen] == ["play", "menu"]  # sketched scenes first
    assert v.seen[0]["images"] and "character 'Kael' at (20,50)" in v.seen[0]["content"]
    assert out == [{"title": "play: Fighters are plain rectangles", "scene": "play", "kind": "visual",
                    "evidence": "vision review: two coloured boxes", "acceptance": ["fighters drawn with heads and limbs"]}]


def test_playtest_round_adds_the_vision_reviews_problems(tmp_path, monkeypatch):
    from jarvis.studio import playtest, vision

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    monkeypatch.setattr(playtest, "run", lambda root, stack, frames=4000: playtest.Report(True, {
        "explore": {"reachable": ["menu", "play"], "edges": []}, "run": {"scenes": {}}, "shots": {"play": str(shot)}}))
    monkeypatch.setattr(playtest, "analyse", lambda state, rep, team, filed: [])
    looked = []
    monkeypatch.setattr(vision, "review", lambda shots, design, plan, client, what="game": looked.append(what) or (
        [{"title": "play: Fighters are boxes", "scene": "play", "kind": "visual", "evidence": "v",
          "acceptance": ["drawn fighters"]}] if len(looked) == 1 else []))
    studio, calls, sent = make_studio(tmp_path, monkeypatch, flags=("test_first", "playtest", "vision"))
    studio.team.vision = object()
    state = studio.run()
    assert looked == ["game", "game"] and any(t.title == "play: Fighters are boxes" and t.kind == "visual"
                                              for t in state.tasks)


def test_vision_review_drops_colour_guesses(tmp_path):
    from jarvis.studio import vision

    (tmp_path / "menu.png").write_bytes(b"png")

    class Vision:
        def json(self, messages, schema):
            return {"problems": [
                {"title": "Background color is not from the palette", "evidence": "#1a1a1a", "acceptance": ["x"]},
                {"title": "Title text is cut off at the right edge", "evidence": "the 'l' is missing",
                 "acceptance": ["the title fits on screen"]}]}

    out = vision.review({"menu": str(tmp_path / "menu.png")}, {"palette": PALETTE}, good_plan(), Vision())
    assert [p["title"] for p in out] == ["menu: Title text is cut off at the right edge"]


# ---------------------------------------------------------------- the website stack

def test_websites_get_the_site_stack():
    assert stack_for("build me an animated portfolio website") == "site"
    assert stack_for("make a landing page for my bakery") == "site"
    assert stack_for("a web game for my iPhone") == "web"
    assert stack_for("make a game website") == "pygame"  # game words win: it's a game
    assert validate_plan(good_plan(title="Stripe Studio"), "Stripe's website")  # the reference name is refused
    assert validate_plan(good_plan(title="Northwind Studio"), "Stripe's website") == []  # generic words are fine


@pytest.mark.skipif(stacks.get("site").available() is not None, reason="needs node and Edge/Chrome")
def test_site_skeleton_passes_its_tests_page_check_and_screenshots(tmp_path):
    st = stacks.get("site")
    st.scaffold(tmp_path, "Untitled Site")
    st.set_title(tmp_path, 'Lumen "Studio"')
    st.set_palette(tmp_path, [{"name": "Night", "hex": "#0b0d14", "use": "background"},
                              {"name": "Ink Text", "hex": "#f2efe8", "use": "body text"},
                              {"name": "Glow", "hex": "#7cf2c8", "use": "accent"}])
    assert "<title>Lumen &quot;Studio&quot;</title>" in (tmp_path / "index.html").read_text()
    css = (tmp_path / "css" / "styles.css").read_text()
    assert "--night: #0b0d14;" in css and "--bg: var(--night);" in css and "--text: var(--ink-text);" in css
    tests = st.run_tests(tmp_path)
    assert tests.ok and tests.passed == 6
    data = st.check(tmp_path)
    desk, phone = data["viewports"]
    assert data["ok"] and desk["sections"] == ["hero"] and desk["keyframes"] == 1 and desk["reduced_motion"]
    assert not phone["overflow"] and desk["external"] == [] and desk["broken_anchors"] == []
    assert st.smoke(tmp_path).summary() == "page check: sections hero"
    shots = st.screenshots(tmp_path, tmp_path / "shots")
    assert set(shots) == {"desktop", "phone", "overview"} and all(Path(p).stat().st_size > 1000 for p in shots.values())


@pytest.mark.skipif(stacks.get("site").available() is not None, reason="needs node and Edge/Chrome")
def test_site_playtest_finds_errors_missing_sections_and_overflow(tmp_path):
    from jarvis.studio import playtest

    st = stacks.get("site")
    st.scaffold(tmp_path, "X")
    index = tmp_path / "index.html"
    index.write_text(index.read_text().replace(
        '<nav id="nav"><a href="#hero">Home</a></nav>', '<nav id="nav"><a href="#hero">Home</a> <a href="#work">Work</a></nav>'
    ).replace("</main>", '<div style="width:900px">too wide</div><script>undefinedThing.x()</script></main>'))
    report = playtest.run(tmp_path, st)
    assert report.ok and report.data["kind"] == "site" and set(report.data["shots"]) == {"desktop", "phone"}
    plan = good_plan(scenes=["hero", "work"], transitions=[{"from": "hero", "to": "work", "trigger": "nav link"}])
    titles = [p["title"] for p in playtest.certain_problems(report, plan)]
    assert "Fix the script error on the page" in titles and "Add the 'work' section" in titles
    assert "Make the page fit a phone screen" in titles and "Fix links to sections that don't exist" in titles
    assert "undefinedThing" in playtest.summary(report) and "sideways overflow: YES" in playtest.summary(report)


def test_syntax_errors_in_test_files_are_found_by_python_and_node(tmp_path):
    good, bad = tmp_path / "a.test.mjs", tmp_path / "b.test.mjs"
    good.write_text("import test from 'node:test';\ntest('x', () => {});\n")
    bad.write_text("const r = /style=\"background: (linear-gradient/;\n")
    st = stacks.get("site")
    if stacks.shutil.which("node"):
        assert st.syntax_error(good) is None and "SyntaxError" in st.syntax_error(bad)
    py = tmp_path / "test_x.py"
    py.write_text("def test_x(:\n    pass\n")
    assert stacks.get("pygame").syntax_error(py).startswith("line 1")


def test_a_test_that_never_parses_is_rewritten_then_dropped(tmp_path, monkeypatch):
    studio, calls, sent = make_studio(tmp_path, monkeypatch, flags=("test_first",))
    real = agents.write_tests
    rewrites = []

    def broken_tests(root, state, task, team, st, feedback=""):
        out = real(root, state, task, team, st, feedback)
        if task.id == "T1":  # the tester keeps writing an unparsable file for T1
            (root / "tests" / "T1.py").write_text("BROKEN")
            rewrites.append(feedback[:20])
        return out

    monkeypatch.setattr(agents, "write_tests", broken_tests)
    state = studio.run()
    assert rewrites == ["", "it doesn't even pars", "it doesn't even pars"]  # first try + two rewrites
    assert not (tmp_path / "tests" / "T1.py").exists() and state.task("T1").status == "done"
    assert "didn't parse after two rewrites" in Project(tmp_path).read_doc("memory.md")


def test_failures_are_reported_per_test_with_their_file():
    tap = ("# Subtest: reveal shows items\nnot ok 6 - reveal shows items\n  ---\n  duration_ms: 1\n"
           "  location: 'C:\\Users\\x\\tests\\site.test.mjs:33:1'\n  ...\nok 7 - other\n")
    assert stacks.tap_failures(tap) == ["site.test.mjs::reveal shows items"]
    out = "FAILED tests/test_t1.py::test_hit - AssertionError\nERROR tests/test_engine.py::test_menu\n"
    assert stacks.pytest_failures(out) == ["test_t1.py::test_hit", "test_engine.py::test_menu"]


def test_broken_old_tests_are_called_regressions_with_the_diff(tmp_path, monkeypatch):
    from jarvis.studio import loop as studio_loop

    studio, calls, sent = make_studio(tmp_path, monkeypatch)
    monkeypatch.setattr(studio_loop, "code_diff", lambda root: "Diff since the last commit:\n-def reveal(root, Observer)")
    tests = stacks.TestResult(False, 5, 2, "out", ["site.test.mjs::reveal shows items", "T1.py::new behaviour"])
    assert studio.regressions(tests, Task("T1", "x")) == ["site.test.mjs::reveal shows items"]
    studio.stack.run_tests = lambda root: tests
    state = Project(tmp_path).load()
    check = studio.check(state, Task("T1", "x"))
    assert check.feedback.startswith("REGRESSION: these tests passed before this task")
    assert "- site.test.mjs::reveal shows items" in check.feedback and "-def reveal(root, Observer)" in check.feedback
    assert "T1.py::new behaviour" not in check.feedback.split("Diff since")[0]  # its own new test isn't a regression
