"""The studio loop: research -> plan -> build every task -> integrate -> done, resumable at any point.

Per task: tests first (tester) -> code rounds (coder) until all tests and the play-through pass and the
plugins' checks are clean -> review -> commit. Stuck after the coding rounds (or two rejected reviews):
ask Claude Code. Still stuck: the task is blocked, tasks that need it are skipped, the rest carries on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.studio import agents, docs
from jarvis.studio.project import Project, State, Task, next_task, skip_unreachable, tasks_from_plan

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIT_ID = ["-c", "user.name=Jarvis Studio", "-c", "user.email=studio@jarvis.local", "-c", "commit.gpgsign=false"]


@dataclass
class Budget:
    hours: float = 6.0
    rounds: int = 3            # coder rounds per task before getting unstuck (test check, then split)
    reviews: int = 2           # rejected reviews before asking for help
    split_depth: int = 2       # a stuck task splits into smaller steps, and those can split once more
    helper_per_task: int = 1   # Claude Code: a last resort only (user, 7 Nov 2026: "not Claude Code dependent")
    helper_total: int = 3
    playtests: int = 2         # playtest rounds (each can add fix tasks)


@dataclass
class Check:
    ok: bool
    feedback: str
    tests: object
    smoke: object
    split: bool = False        # the task was split into smaller steps that now come first


def git(root, *args: str) -> str:
    exe = shutil.which("git")
    if not exe:
        return ""
    done = subprocess.run([exe, *GIT_ID, *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW)
    return done.stdout


def commit(root, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def diff(root) -> str:
    git(root, "add", "-A")
    return git(root, "diff", "--cached", "--stat") + "\n" + git(root, "diff", "--cached")


def code_diff(root, chars: int = 2500) -> str:
    """What this task changed in the code (not tests/docs) since the last commit."""
    text = git(root, "diff", "HEAD", "--", ".", ":(exclude)tests", ":(exclude)docs", ":(exclude).studio")
    return ("Diff since the last commit:\n" + text[:chars] + ("\n... (diff cut)" if len(text) > chars else "")
            if text.strip() else "(no code changes since the last commit)")


def changed_code(root) -> list[str] | None:
    """Files changed since the last commit, outside tests/docs/.studio. None if this isn't a git repo."""
    if not git(root, "rev-parse", "--is-inside-work-tree").strip():
        return None
    files = [line[3:].strip().strip('"') for line in git(root, "status", "--porcelain", "-uall").splitlines()]
    return [f for f in files if f and not f.startswith(("tests/", "docs/", ".studio/"))]


NO_CHANGE = ("No code was written for this task: nothing outside tests/ changed. Use write_file to create or change "
             "the files the task names, with complete contents.")


class Studio:
    def __init__(self, project: Project, team: agents.Team, stack, helper=None, budget: Budget | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_plan: Callable[[Project], Project | None] | None = None):
        self.project, self.team, self.stack, self.helper = project, team, stack, helper
        self.budget = budget or Budget()
        self.clock = clock
        self.on_plan = on_plan
        self.say = team.say
        self.flags = {p.name for p in team.plugins}

    # ---------- plumbing ----------

    def event(self, state: State, name: str, **data) -> None:
        self.project.log(name, **data)
        for p in self.team.plugins:
            p.on_event(name, state.plan.get("title", state.name), **data)

    def save(self, state: State) -> None:
        self.project.save(state)
        try:
            docs.write_all(self.project, state, self.stack.play_command(self.project.root))
        except Exception as e:  # documents are a view; never stop the build for them
            self.project.log("docs_failed", detail=repr(e))

    def regressions(self, tests, task: Task | None) -> list[str]:
        """Failing tests that aren't this task's own: they passed at the last commit (every finished step is
        committed green), so this task's change broke them."""
        if task is None or tests.ok:
            return []
        own = Path(self.stack.test_file(task.id)).name
        return [f for f in tests.failing if f.split("::")[0] and f.split("::")[0] != own]

    def check(self, state: State, task: Task | None = None) -> Check:
        tests = self.stack.run_tests(self.project.root)
        smoke = self.stack.smoke(self.project.root, 600)
        problems = [p for plugin in self.team.plugins for p in plugin.check(self.project.root, self.stack)]
        state.last_tests = {"ok": tests.ok, "summary": tests.summary()}
        state.last_smoke = {"ok": smoke.ok, "summary": smoke.summary(), "scenes": smoke.scenes}
        feedback = []
        broke = self.regressions(tests, task)
        if broke:
            # Found on the first website run: a rewrite changed reveal()'s contract, the skeleton's test (green
            # before) failed, and every later diagnosis blamed that test. An engineer looks at the diff first.
            feedback.append("REGRESSION: these tests passed before this task and fail now, so this task's change broke "
                            "them. Don't change those tests: restore the behaviour they check (the diff of the code "
                            "so far is below).\n- " + "\n- ".join(broke[:8]) + "\n\n" + code_diff(self.project.root))
        if not tests.ok:
            feedback.append(f"Tests: {tests.summary()}\n{tests.output[-3000:]}")
        if not smoke.ok:
            feedback.append(f"Automatic play-through crashed:\n{smoke.error[-2000:]}")
        if problems:
            feedback.append("Code checks:\n- " + "\n- ".join(problems[:10]))
        return Check(tests.ok and smoke.ok and not problems, "\n\n".join(feedback), tests, smoke)

    # ---------- the run ----------

    def run(self) -> State:
        state = self.project.load()
        started = self.clock()
        used_before = state.seconds_used
        if state.plan and not (self.project.docs / "memory.md").is_file():  # a project from before memory.md
            c = state.counts()
            self.project.remember("context", f"Request: “{state.request}”. {state.plan.get('title')} ({state.stack}); "
                                  f"{c['done']}/{len(state.tasks)} tasks done when the memory was started.")
        if state.plan and not state.plan.get("design"):  # a project from before the design guide existed
            self.ensure_design(state)
            self.save(state)
        steps = {"research": self.research, "plan": self.plan, "build": self.build_step, "playtest": self.playtest,
                 "integrate": self.integrate}
        try:
            while state.phase != "done":
                if self.project.stop_requested():
                    (self.project.studio / "STOP").unlink(missing_ok=True)
                    state.message = "Paused (stop requested). Resume to carry on."
                    self.event(state, "stopped", reason="stop requested")
                    break
                if used_before + self.clock() - started > self.budget.hours * 3600:
                    state.message = f"Paused: the {self.budget.hours:g}-hour budget is used up. Resume to carry on."
                    self.event(state, "stopped", reason=state.message)
                    break
                steps[state.phase](state)
                state.seconds_used = used_before + self.clock() - started
                self.save(state)
        except KeyboardInterrupt:
            state.message = "Paused (Ctrl-C). Resume to carry on."
            self.event(state, "stopped", reason="Ctrl-C")
        finally:
            state.seconds_used = used_before + self.clock() - started
            self.save(state)
        return state

    def research(self, state: State) -> None:
        state.message = f"Researching {state.reference or 'the idea'}"
        self.save(state)
        self.say(f"[research] {state.reference or 'no reference game'}")
        out = agents.research(self.project.root, state, self.team)
        self.event(state, "research_done", detail=out[:200])
        state.phase = "plan"

    def plan(self, state: State) -> None:
        state.message = "Planning: PRD, features, tasks"
        self.save(state)
        self.say("[plan] writing the PRD and tasks")
        state.plan = agents.plan(self.project.root, state, self.team, self.stack)
        state.tasks = tasks_from_plan(state.plan)
        title = state.plan["title"]
        self.stack.set_title(self.project.root, title)
        self.ensure_design(state)
        self.project.remember("context", f"Request: “{state.request}”. Building {title} with {state.stack}"
                              + (f"; mechanics studied from {state.reference}, identity original." if state.reference else "."))
        self.project.remember("decision", f"Plan: {len(state.tasks)} tasks; scenes {', '.join(state.plan.get('scenes', []))}; "
                              f"out of scope: {'; '.join(state.plan.get('out_of_scope', []))[:200] or 'nothing listed'}")
        state.phase = "build"
        self.save(state)
        commit(self.project.root, f"Plan: {title}")
        self.event(state, "plan_ready", title=title, tasks=len(state.tasks), stack=state.stack)
        self.say(f"[plan] {title}: {len(state.tasks)} tasks")
        if self.on_plan:
            moved = self.on_plan(self.project)  # e.g. the folder renamed after the game
            if moved is not None and moved.root != self.project.root:
                self.project = moved
                state.name = moved.root.name
                self.save(state)

    def ensure_design(self, state: State) -> None:
        """The design guide and rule book (design.md, Rules.md), and the palette in the game's config."""
        if state.plan.get("design"):
            return
        try:
            state.plan["design"] = agents.design(self.project.root, state, self.team, self.stack)
        except Exception as e:  # without it the docs fall back to the general rules
            self.project.log("design_failed", detail=repr(e)[:200])
            return
        self.stack.set_palette(self.project.root, state.plan["design"]["palette"])
        self.project.remember("decision", "Design: " + ", ".join(
            f"{c['name']} {c['hex']}" for c in state.plan["design"]["palette"]))

    def build_step(self, state: State) -> None:
        task = next_task(state)
        if task is None:
            skipped = skip_unreachable(state)
            if skipped:
                self.event(state, "skipped", detail=", ".join(skipped))
            if next_task(state) is None:
                more = "playtest" in self.flags and state.playtests < self.budget.playtests
                state.phase = "playtest" if more else "integrate"
            return
        state.current = task.id
        self.run_task(state, task)
        state.current = None

    def run_task(self, state: State, task: Task) -> None:
        root = self.project.root
        self.say(f"[{task.id}] {task.title}")
        self.unpark_tests(task)  # back from being split: its own tests check the whole again
        if not task.approach and task.status in ("todo", "testing"):
            state.message = f"Planning the approach for {task.id}: {task.title}"
            self.save(state)
            try:
                task.approach = agents.approach(root, state, task, self.team)
                self.project.remember("decision", f"{task.id} approach: {task.approach.splitlines()[0][:220]}")
            except Exception as e:  # the task can still be built without it
                self.project.log("approach_failed", task=task.id, detail=repr(e)[:200])
        has_tests = (root / self.stack.test_file(task.id)).is_file()
        if task.status in ("todo", "testing") and "test_first" in self.flags and task.kind != "visual" and not (
                has_tests and any(t.parent == task.id for t in state.tasks)):
            task.status = "testing"
            state.message = f"Writing tests for {task.id}: {task.title}"
            self.save(state)
            if not agents.write_tests(root, state, task, self.team, self.stack):
                task.notes.append("the tester didn't write a test file; checked by the existing tests only")
            self.event(state, "tests_written", task=task.id)
            self.tests_must_parse(state, task)
        task.status = "coding"
        check = self.code_until_green(state, task)
        if check.split:  # smaller steps were put in front of it; it comes back when they're done
            return
        if check.ok and "review" in self.flags:
            task.status = "review"
            check = self.review_until_ok(state, task, check)
            if check.split:
                return
        if check.ok:
            task.status = "done"
            files = changed_code(root) or []
            commit(root, f"{task.id}: {task.title}")
            self.event(state, "task_done", task=task.id, title=task.title)
            self.project.remember("change", f"{task.id} {task.title} done" + (f" ({', '.join(files[:6])})" if files else ""))
            self.feature_check(state, task)
        else:
            task.status = "blocked"
            task.notes.append(check.feedback[-300:])
            commit(root, f"{task.id} (blocked): {task.title}")
            self.event(state, "blocked", task=task.id, title=task.title)
            self.project.remember("bug", f"{task.id} {task.title} blocked: {check.feedback[-250:]}")

    def code_until_green(self, state: State, task: Task, feedback: str = "") -> Check:
        check = self.check(state, task) if not feedback else None
        if check and check.ok and task.rounds > 0:
            return check  # resumed after the work was already done
        feedback = feedback or (check.feedback if check and not check.ok else "")  # e.g. the new tests failing
        last, stalled = None, 0
        while task.rounds < self.budget.rounds:
            task.rounds += 1
            separate = self.team.planner is not None and self.team.planner is not self.team.coder
            final = task.rounds == self.budget.rounds or stalled >= 2
            # with two different models, the last round is the planner's: a second model often sees it
            model = self.team.planner if final and separate else None
            state.message = f"Coding {task.id} (round {task.rounds}{', second opinion' if model else ''}): {task.title}"
            self.save(state)
            agents.write_code(self.project.root, state, task, self.team, self.stack, feedback, model=model)
            check = self.check(state, task)
            if check.ok and not self.made_progress(task):
                check = Check(False, NO_CHANGE, check.tests, check.smoke)
            self.event(state, "round", task=task.id, detail=f"round {task.rounds}{' (second model)' if model else ''}: "
                       f"{'ok' if check.ok else 'no code written' if check.feedback == NO_CHANGE else check.tests.summary()}")
            if check.ok:
                return check
            same = (check.tests.summary() if check.tests else "", check.feedback[:300])
            stalled = stalled + 1 if same == last else 0
            last = same
            feedback = self.diagnosed(state, task, check.feedback, feedback)
            if final:
                break
        return self.unstick(state, task, feedback)

    def tests_must_parse(self, state: State, task: Task) -> None:
        """A test file that doesn't even parse is the tester's mistake for certain (no judgement needed): rewrite it
        with the exact error, twice at most; still broken, drop it and let the other tests and the review check
        the task. Found live: a JS regex with an unmatched ')' took 6 rounds, a test check and a split."""
        test = self.project.root / self.stack.test_file(task.id)
        for attempt in range(3):
            error = self.stack.syntax_error(test)
            if error is None:
                return
            if attempt == 2:
                test.unlink()
                task.notes.append("its test never parsed; dropped (checked by the other tests and the review)")
                self.project.remember("bug", f"{task.id}: the tester's test file didn't parse after two rewrites; "
                                             f"dropped. Last error: {error[:160]}")
                return
            self.event(state, "test_syntax", task=task.id, detail=error[:200])
            agents.write_tests(self.project.root, state, task, self.team, self.stack,
                               feedback=f"it doesn't even parse: {error[:400]}. Write valid code; prefer simple "
                                        "string checks (includes, startswith) over complicated regular expressions.")

    def diagnosed(self, state: State, task: Task, failure: str, before: str = "") -> str:
        """Root cause before the next attempt (thinking on): the coder gets the diagnosis plus the trimmed raw
        output, not just the raw output. If the last diagnosis didn't fix it, the next one looks deeper."""
        if failure == NO_CHANGE:
            return failure
        state.message = f"Diagnosing {task.id}"
        self.save(state)
        previous = before.split("\n\nRaw output", 1)[0] if before.startswith("Root cause:") else ""
        try:
            diagnosis = agents.diagnose(self.project.root, state, task, self.team, failure, previous)
        except Exception as e:  # without a diagnosis the raw output still helps
            self.project.log("diagnose_failed", task=task.id, detail=repr(e)[:200])
            return failure
        self.event(state, "diagnosis", task=task.id, detail=diagnosis.splitlines()[0][:200])
        return f"{diagnosis}\n\nRaw output (trimmed):\n{failure[-2000:]}"

    def unstick(self, state: State, task: Task, failure: str) -> Check:
        """Stuck after the coding rounds. In order: are the tests wrong? (the small tester writes some wrong
        ones) -> split the task into smaller steps (twice at most) -> Claude Code, a last resort with a small
        budget -> blocked. (User, 7 Nov 2026: break stuck tasks down instead of falling back to Claude Code.)"""
        root = self.project.root
        self.tests_must_parse(state, task)  # a broken test file first: that one needs no judgement
        if not task.tests_checked and (root / self.stack.test_file(task.id)).is_file() and failure != NO_CHANGE:
            task.tests_checked = True
            try:
                wrong, reason = agents.check_tests(root, state, task, self.team, self.stack, failure)
            except Exception as e:  # a planner hiccup: carry on as if the test were fair
                wrong, reason = False, f"check failed: {e!r}"[:200]
            self.event(state, "test_check", task=task.id, detail=("wrong: " if wrong else "fair: ") + reason[:160])
            if wrong:
                self.project.remember("bug", f"{task.id}: its test was wrong ({reason}); rewritten")
                agents.write_tests(root, state, task, self.team, self.stack, feedback=reason)
                task.rounds = max(0, self.budget.rounds - 2)  # two fresh rounds against the fixed test
                return self.code_until_green(state, task)
        if task.depth < self.budget.split_depth:
            try:
                steps = agents.split(root, state, task, self.team, failure)
            except Exception as e:  # a planner hiccup shouldn't lose the task
                self.project.log("split_failed", task=task.id, detail=repr(e)[:200])
                steps = []
            if steps:
                from jarvis.studio.project import split_into

                subs = split_into(state, task, steps)
                self.park_tests(task)
                names = ", ".join(f"{s.id} {s.title}" for s in subs)
                self.event(state, "split", task=task.id, detail=names[:300])
                self.project.remember("decision", f"{task.id} was too big for one go; split into {names}")
                self.say(f"[{task.id}] split into {', '.join(s.id for s in subs)}")
                return Check(False, failure, None, None, split=True)
        return self.ask_helper(state, task, failure)

    def made_progress(self, task: Task) -> bool:
        """Passing doesn't count if nothing was written: the skeleton's own tests pass on an empty task
        (seen 7 Nov 2026: four tasks 'done' with no code). The task's own passing tests do count."""
        changed = changed_code(self.project.root)
        return changed is None or bool(changed) or (self.project.root / self.stack.test_file(task.id)).is_file()

    PARKED = ".parked"

    def park_tests(self, task: Task) -> None:
        """While a split task's steps run, its own (still failing) tests would fail every step's 'all tests pass'
        check, so they wait under another name (pytest and node --test ignore it) until the task comes back."""
        test = self.project.root / self.stack.test_file(task.id)
        if test.is_file():
            test.replace(test.with_name(test.name + self.PARKED))

    def unpark_tests(self, task: Task) -> None:
        test = self.project.root / self.stack.test_file(task.id)
        parked = test.with_name(test.name + self.PARKED)
        if parked.is_file():
            parked.replace(test)

    def ask_helper(self, state: State, task: Task, failure: str) -> Check:
        if (self.helper is None or task.helper_calls >= self.budget.helper_per_task
                or state.helper_calls >= self.budget.helper_total or not self.helper.available()):
            return Check(False, failure, None, None)
        task.helper_calls += 1
        state.helper_calls += 1
        state.message = f"Asking {self.helper.name} for help with {task.id}"
        self.save(state)
        self.event(state, "helper", task=task.id, title=task.title)
        self.say(f"[{task.id}] stuck: asking {self.helper.name}")
        result = self.helper.ask(self.project.root, agents.describe_task(task), failure, self.stack)
        task.notes.append(f"{self.helper.name}: {result.summary[:200]}")
        self.project.remember("decision", f"{task.id} still stuck after splitting: asked {self.helper.name}. "
                                          f"{result.summary[:200]}")
        check = self.check(state, task)
        self.event(state, "helper_done", task=task.id, detail=f"{'fixed' if check.ok else 'not fixed'}: "
                   f"{result.summary[:120]}")
        return check

    def review_until_ok(self, state: State, task: Task, check: Check) -> Check:
        while True:
            approve, problems = agents.review(self.project.root, state, task, self.team, diff(self.project.root),
                                              check.tests.summary())
            self.event(state, "review", task=task.id, detail="approved" if approve else "; ".join(problems)[:200])
            if approve:
                return check
            task.reviews += 1
            feedback = "The reviewer asked for these changes:\n- " + "\n- ".join(problems)
            if task.reviews >= self.budget.reviews:
                fixed = self.ask_helper(state, task, feedback)
                if not fixed.ok:  # tests still pass from before? keep the working version, note the concerns
                    task.notes.append("review concerns left open: " + "; ".join(problems)[:300])
                    return self.check(state, task)
                return fixed
            agents.write_code(self.project.root, state, task, self.team, self.stack, feedback)
            check = self.check(state, task)
            if not check.ok:
                check = self.code_until_green(state, task, check.feedback)
                if not check.ok:
                    return check

    def feature_check(self, state: State, task: Task) -> None:
        same = [t for t in state.tasks if t.feature == task.feature]
        if task.feature and all(t.status == "done" for t in same):
            name = next((f["name"] for f in state.plan.get("features", []) if f["id"] == task.feature), task.feature)
            c = state.counts()
            self.event(state, "feature_done", feature=name, done=c["done"], total=len(state.tasks))

    def playtest(self, state: State) -> None:
        """Play the finished game and turn what's broken into fix tasks (see playtest.py)."""
        from jarvis.studio import playtest

        state.playtests += 1
        state.message = f"Playtesting (round {state.playtests})"
        self.save(state)
        self.say(f"[playtest {state.playtests}] playing the game automatically")
        report = playtest.run(self.project.root, self.stack)
        problems = playtest.certain_problems(report, state.plan)
        try:
            problems += playtest.analyse(state, report, self.team, problems)
        except Exception as e:  # the model's judgement is a bonus; the certain problems still count
            self.project.log("playtest_analysis_failed", detail=repr(e)[:200])
        if "vision" in self.flags and self.team.vision is not None and report.ok and report.data.get("shots"):
            from jarvis.studio import vision

            state.message = "Vision review of the screenshots"
            self.save(state)
            self.say("  vision model reviewing the screenshots")
            try:
                seen = vision.review(report.data["shots"], state.plan.get("design", {}), state.plan, self.team.vision,
                                     what="website" if state.stack == "site" else "game")
                problems += [p for p in seen if not playtest._duplicate(p["title"], problems)]
                self.event(state, "vision_review", detail=f"{len(seen)} problem(s): "
                           + "; ".join(p["title"] for p in seen)[:250])
            except Exception as e:  # the vision model is a bonus too
                self.project.log("vision_review_failed", detail=repr(e)[:200])
        fixes = playtest.tasks_for(problems[:7], state)
        d = report.data if report.ok else {}
        state.last_playtest = {"round": state.playtests, "summary": playtest.summary(report)[:6000],
                               "problems": [p["title"] for p in problems[:6]],
                               "reachable": d.get("explore", {}).get("reachable", []),
                               "edges": d.get("explore", {}).get("edges", []),
                               "shots": {s: Path(p).name for s, p in d.get("shots", {}).items()}}
        if fixes and not any(f.get("id") == "PT" for f in state.plan.get("features", [])):
            state.plan.setdefault("features", []).append(
                {"id": "PT", "name": "Playtest fixes", "description": "Problems found by playing the game.",
                 "priority": "must"})
        state.tasks += fixes
        self.event(state, "playtest", detail=f"round {state.playtests}: {len(fixes)} problem(s): "
                   + "; ".join(f.title for f in fixes)[:300])
        for f in fixes:
            self.say(f"  found: {f.id} {f.title}")
            self.project.remember("bug", f"playtest {state.playtests}: {f.title} ({f.notes[-1][10:180] if f.notes else ''})")
        state.phase = "build" if fixes else "integrate"

    def integrate(self, state: State) -> None:
        state.message = "Final play-through and checks"
        self.save(state)
        self.say("[integrate] final checks")
        check = self.check(state)
        if not check.ok and not any(t.id == "INT" for t in state.tasks):
            fix = Task("INT", "Make the whole game pass its tests and the play-through", feature="",
                       acceptance=["all tests pass", "the automatic play-through runs without crashing"])
            state.tasks.append(fix)
            fix.status = "coding"
            check = self.code_until_green(state, fix, check.feedback)
            if check.split:  # its smaller steps go through the build loop, then integration runs again
                state.phase = "build"
                return
            fix.status = "done" if check.ok else "blocked"
            commit(self.project.root, "Integration fixes")
        scenes = set(state.last_smoke.get("scenes", []))
        missing = [s for s in state.plan.get("scenes", []) if s not in scenes]
        if missing:
            self.project.log("unvisited_scenes", detail=", ".join(missing))
        play = self.stack.play_command(self.project.root)
        self.project.root.joinpath("README.md").write_text(docs.readme(state, play) + "\n", encoding="utf-8")
        state.phase = "done"
        c = state.counts()
        state.message = (f"Done: {c['done']}/{len(state.tasks)} tasks"
                         + (f", {c['blocked']} blocked" if c["blocked"] else "") + ". " +
                         ("All checks pass." if check.ok else "Some checks still fail; see the task board."))
        commit(self.project.root, "Game ready")
        self.event(state, "done", title=state.plan.get("title", state.name), done=c["done"], total=len(state.tasks),
                   play=play)
        self.say(f"[done] {state.message}")


def open_page(project: Project) -> None:
    page = project.docs / "project.html"
    if page.is_file() and hasattr(os, "startfile"):
        os.startfile(page)  # noqa: S606 - our own generated page
