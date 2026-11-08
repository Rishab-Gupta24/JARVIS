"""Jarvis Studio: plans, builds and tests a whole game with a team of agents, in a loop until it's done.

    python -m jarvis.studio new "make me a game like Shadow Fight 2" [--stack pygame|web] [--hours 6]
    python -m jarvis.studio resume [<project folder or name>]
    python -m jarvis.studio status [<project>]
    python -m jarvis.studio playtest [<project>]   (play a finished game again and fix what is broken)
    python -m jarvis.studio graph [<project>]      (rewrite docs/ and open docs/project.html)
    python -m jarvis.studio list

Jarvis starts `new` in a terminal window when you ask it for a game. Projects go in your Projects folder
(JARVIS_NEW_PROJECTS_DIR). Create <project>/.studio/STOP (or press Ctrl-C) to pause; `resume` carries on.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from jarvis.config import Settings
from jarvis.studio import agents, docs, plugins, stacks, status
from jarvis.studio.helper import ClaudeCodeHelper
from jarvis.studio.loop import Budget, Studio, commit, open_page
from jarvis.studio.project import Project, State, _REF, reference_of, stack_for

PLACEHOLDER = "Untitled Game"


def _say(text: str) -> None:
    print(text, flush=True)  # live in the terminal window and in redirected logs


def make_team(settings: Settings, say=_say) -> agents.Team:
    from jarvis.core.llm import OllamaClient

    # keep_alive as Jarvis's own: the planner model is usually Jarvis's voice brain, pinned in memory
    shared = settings.studio_planner_model == settings.ollama_model  # don't undo Jarvis pinning it (-1)
    planner = OllamaClient(settings.ollama_url, settings.studio_planner_model, timeout=900,
                           keep_alive=-1 if shared else settings.keep_alive)
    coder_model = settings.studio_coder_model or settings.studio_planner_model
    coder = planner if coder_model == settings.studio_planner_model else OllamaClient(
        settings.ollama_url, coder_model, timeout=900)
    thinker_model = settings.studio_thinker_model or settings.studio_planner_model
    # a separate thinker stays loaded a few minutes (plan then design back to back), then the pinned model
    # returns; with OLLAMA_MAX_LOADED_MODELS=1 the next fast-model request evicts it anyway
    thinker = planner if thinker_model == settings.studio_planner_model else OllamaClient(
        settings.ollama_url, thinker_model, timeout=1800, keep_alive="5m")
    send = plugins.phone_sender(settings) if "phone_updates" in settings.studio_plugins else None
    vision = None
    if "vision" in settings.studio_plugins:  # unloads a minute after the review so qwen3 comes straight back
        from jarvis.core.llm import VISION_NUM_CTX

        vision = OllamaClient(settings.ollama_url, settings.vision_model, timeout=900, keep_alive="1m",
                              num_ctx=VISION_NUM_CTX)
    return agents.Team(planner, coder, say, plugins.load(settings.studio_plugins, settings.studio_ponytail, send),
                       thinker=thinker, vision=vision)


def folder_name(request: str) -> str:
    """'make me a game like Shadow Fight 2' -> 'game' (the reference's name never goes into the folder)."""
    from jarvis.pc.actions import project_name_for

    return project_name_for(_REF.sub(" ", request))


def create(request: str, settings: Settings, stack_name: str | None = None) -> Project:
    from jarvis.pc.actions import create_project

    stack_name = stack_name or stack_for(request)
    stack = stacks.get(stack_name)
    if (missing := stack.available()):
        raise SystemExit(f"Can't build a {stack_name} game here: {missing}.")
    made = create_project(folder_name(request), Path(settings.new_projects_dir))
    project = Project(made.path)
    stack.scaffold(project.root, PLACEHOLDER)
    project.write_doc("request.md", f"# Request\n\n“{request}”\n")
    state = State(request=request, name=made.name, stack=stack_name, reference=reference_of(request))
    project.save(state)
    commit(project.root, "Studio skeleton")
    project.log("created", detail=request, stack=stack_name)
    return project


def rename_to_title(project: Project) -> Project:
    """After planning, name the folder after the game (if that name is free)."""
    state = project.load()
    slug = "-".join(re.findall(r"[a-z0-9]+", state.plan.get("title", "").lower()))[:40].strip("-")
    target = project.root.parent / slug
    if not slug or target.exists() or slug == project.root.name:
        return project
    try:
        project.root.rename(target)
    except OSError:
        return project  # something has the folder open; keep the old name
    return Project(target)


def run(project: Project, settings: Settings, hours: float | None = None, open_browser: bool = True) -> State:
    state = project.load()
    stack = stacks.get(state.stack)
    team = make_team(settings)
    helper = ClaudeCodeHelper() if settings.studio_helper == "claude" else None
    budget = Budget(hours=hours or settings.studio_hours, helper_total=settings.studio_helper_calls)
    def on_plan(p: Project) -> Project:
        moved = rename_to_title(p)
        docs.write_all(moved, moved.load(), stack.play_command(moved.root))
        if open_browser:
            open_page(moved)
        print(f"Project page: {moved.docs / 'project.html'}")
        return moved

    studio = Studio(project, team, stack, helper, budget, on_plan=on_plan)
    print(f"Jarvis Studio: {state.request}\nFolder: {project.root}\nStack: {state.stack} · plugins: "
          f"{', '.join(p.name for p in team.plugins) or 'none'} · helper: {helper.name if helper else 'none'}\n")
    final = studio.run()
    print("\n" + status.describe(final))
    return final


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m jarvis.studio", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("request", nargs="+")
    p_new.add_argument("--stack", choices=list(stacks.STACK_CLASSES))
    p_new.add_argument("--hours", type=float)
    p_new.add_argument("--no-browser", action="store_true")
    for name in ("resume", "playtest", "status", "graph"):
        p = sub.add_parser(name)
        p.add_argument("project", nargs="?")
        if name in ("resume", "playtest"):
            p.add_argument("--hours", type=float)
            p.add_argument("--no-browser", action="store_true")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    folder = Path(settings.new_projects_dir)
    interactive = sys.stdin.isatty()
    try:
        if args.cmd == "new":
            project = create(" ".join(args.request), settings, args.stack)
            run(project, settings, args.hours, not args.no_browser)
        elif args.cmd == "list":
            for pr in status.projects(folder):
                print(f"{pr.root.name:30s} {status.describe(pr.load())}")
        else:
            project = status.find(folder, args.project)
            if project is None:
                print("No studio project found.")
                return 1
            if args.cmd == "status":
                print(status.describe(project.load()))
            elif args.cmd == "graph":
                state = project.load()
                docs.write_all(project, state, stacks.get(state.stack).play_command(project.root))
                open_page(project)
                print(project.docs / "project.html")
            else:
                if args.cmd == "playtest":  # play a (finished) game again and fix what's broken
                    state = project.load()
                    state.phase, state.playtests, state.message = "playtest", 0, "Playtest requested"
                    project.save(state)
                run(project, settings, args.hours, not args.no_browser)
    except KeyboardInterrupt:
        print("\nStopped.")
    if interactive and args.cmd in ("new", "resume", "playtest"):
        try:
            input("\nPress Enter to close this window.")
        except EOFError:  # Git Bash reports a terminal even when nothing can be typed
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
