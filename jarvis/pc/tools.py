"""PC-control tools for the model. Only offered when the request comes from the user
directly (PC voice, typed chat, the iPhone Shortcut or web app), never for shared
messages, files or offline items: those carry other people's words."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from jarvis.config import Settings
from jarvis.core.toolkit import Args, Tool, ToolContext, ToolError, ToolResult, spec, string
from jarvis.pc import actions, confirm

NAMES = {"start_coding", "open_app", "close_app", "open_project", "open_folder", "open_website", "media_control",
         "lock_screen", "take_screenshot", "run_command", "show_interface"}


class AppArgs(Args):
    name: str = Field(min_length=1, max_length=80)


class ProjectArgs(Args):
    name: str = Field(min_length=1, max_length=80)
    task: str | None = Field(default=None, max_length=500)


class CodingArgs(Args):
    task: str | None = Field(default=None, max_length=500)
    project: str | None = Field(default=None, max_length=80)


class FolderArgs(Args):
    name: str = Field(min_length=1, max_length=120)


class WebsiteArgs(Args):
    url: str = Field(min_length=3, max_length=500)


class MediaArgs(Args):
    action: str = Field(min_length=2, max_length=30)
    times: int = Field(default=1, ge=1, le=20)


class NoArgs(Args):
    pass


class InterfaceArgs(Args):
    action: str = Field(default="show", max_length=10)


class CommandArgs(Args):
    command: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(default="", max_length=200)


def _guard(ctx: ToolContext) -> None:
    if not ctx.allow_pc:
        raise ToolError("PC control isn't available for this request.")


def _projects() -> list[actions.Project]:
    return actions.discover_projects(Settings.from_env().projects)


def _do(fn, *a) -> ToolResult:
    try:
        return ToolResult(True, {"done": True}, fn(*a))
    except actions.ActionError as e:
        return ToolResult(True, {"done": False, "problem": str(e)}, str(e))


def open_app(args: AppArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    return _do(actions.open_app, args.name)


def close_app(args: AppArgs, ctx: ToolContext) -> ToolResult:
    """Like clicking the app's X: it can still ask to save. Never the Windows shell or Jarvis itself."""
    _guard(ctx)
    return _do(actions.close_app, args.name)


ASK_TASK = "Say it in one go, like 'a snake game in Python'. What would you like me to build?"
# Words that only say "open the coder", not what to build: "open Claude Code", "start coding".
_NO_WORK = re.compile(r"\b(open|launch|run|show|bring|fire|start|coding|cli|terminal|window|up|now)\b", re.I)


def _has_work(task: str) -> bool:
    return actions.project_name_for(_NO_WORK.sub(" ", task)) != "new-project"


_CLAUDE = re.compile(r"\bclaude\b", re.I)


def start_coding(args: CodingArgs, ctx: ToolContext) -> ToolResult:
    """Build or change something with a coding agent: in the named existing project, or a new one.
    Claude Code by default (JARVIS_CODER=claude, user's choice 8 Nov 2026); JARVIS_CODER=local for Jarvis's own."""
    _guard(ctx)
    settings = Settings.from_env()
    asked_for_claude = bool(_CLAUDE.search(f"{ctx.raw_text or ''} {args.task or ''}"))
    coder = "claude" if asked_for_claude else settings.coder
    task = actions.clean_task(args.task or "")
    projects = _projects()
    existing = None
    if args.project:
        try:
            existing = actions.find_project(args.project, projects)
        except actions.ActionError:
            existing = None  # "make a game project": a new one
    if not _has_work(task):
        # Seen live: "Can you open Claude Code for me?" -> start_coding("Open Claude Code") made a project "open".
        if existing is not None:
            return _do(actions.start_ai_coder, existing, "", coder) if task else open_project(
                ProjectArgs(name=existing.name), ctx)
        if asked_for_claude:
            return _do(actions.open_claude_code, actions.HOME)
        return ToolResult(True, {"done": False, "asked": True}, ASK_TASK)
    if existing is not None:
        return _do(actions.start_ai_coder, existing, task, coder)
    root = Path(settings.new_projects_dir)
    project = actions.create_project(actions.project_name_for(args.project or task), root)
    result = _do(actions.start_ai_coder, project, task, coder)
    if result.data.get("done"):
        who = "Claude Code" if coder == "claude" else "my coder"
        result.confirmation = (f"I made a new project, {project.name}, in your {root.name} folder, opened it in "
                               f"VS Code and started {who} on: {task}. It will ask before changing anything.")
    return result


def open_project(args: ProjectArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    if args.task:
        return start_coding(CodingArgs(task=args.task, project=args.name), ctx)
    try:
        project = actions.find_project(args.name, _projects())
    except actions.ActionError as e:
        return ToolResult(True, {"done": False, "problem": str(e)}, str(e))
    return _do(actions.open_in_vscode, project)


def open_folder(args: FolderArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    return _do(actions.open_folder, args.name, _projects())


def _site_named(url: str, ctx: ToolContext) -> bool:
    """Did the user say this site's name? ('youtube.com' for 'open YouTube' or 'you tube')."""
    if ctx.raw_text is None:
        return True
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0].removeprefix("www.")
    label = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
    said = re.sub(r"[^a-z0-9]", "", f"{ctx.raw_text} {ctx.replying_to or ''}".lower())
    return bool(label) and label in said


def open_website(args: WebsiteArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    if not _site_named(args.url, ctx):
        raise ToolError("The user didn't name that website. Only open a site they named. If they want "
                        "something built (a website, app, game), call start_coding with their request.")
    return _do(actions.open_website, args.url)


def media_control(args: MediaArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    return _do(actions.press_media, args.action, args.times)


def lock_screen(args: NoArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    return _do(actions.lock_screen)


def take_screenshot(args: NoArgs, ctx: ToolContext) -> ToolResult:
    _guard(ctx)
    return _do(actions.screenshot)


def show_interface(args: InterfaceArgs, ctx: ToolContext) -> ToolResult:
    """Open or close Jarvis's own window on the PC (closed, it shrinks to the orb by the mouse)."""
    from jarvis.desktop.bus import BUS

    _guard(ctx)
    if not BUS.ui_running:
        return ToolResult(True, {"done": False}, "My desktop window isn't running. Start it with: python -m jarvis up.")
    hide = args.action.strip().lower() in ("hide", "close", "minimise", "minimize")
    BUS.publish("ui", action="hide" if hide else "show")
    return ToolResult(True, {"done": True}, "Done." if hide else "Here I am.")


def run_command(args: CommandArgs, ctx: ToolContext) -> ToolResult:
    """Never runs anything: it only proposes, and asks the user."""
    _guard(ctx)
    if (reason := confirm.refuse_reason(args.command)) is not None:
        return ToolResult(True, {"proposed": False, "problem": reason}, reason)
    confirm.PENDING.propose(ctx.session or ctx.source, args.command, args.purpose, ctx.now)
    what = f" to {args.purpose.rstrip('.')}" if args.purpose else ""
    return ToolResult(True, {"proposed": args.command},
                      f"I'd run this{what}: {args.command}. Shall I go ahead?")


SPECS = [
    spec("start_coding", "Build, make, create, code, fix or change software with an AI coding agent on this PC: a "
         "website, game, app, script, feature or bug fix. Call it straight away with the user's request; do NOT "
         "ask them for details first, the coding agent asks its own questions. Works for a new project (just pass "
         "the task) or one of the user's existing projects (pass its name).",
         {"task": string("What to build or change, in the user's words, including details they gave earlier."),
          "project": string("Name of an EXISTING project only if the user named one, e.g. 'Jarvis'.")}),
    spec("open_app", "Open an app installed on the user's PC, e.g. 'Spotify', 'Chrome', 'VS Code', 'calculator'.",
         {"name": string("The app's name as the user said it.")}, ("name",)),
    spec("close_app", "Close an app that is open on the PC, e.g. 'close Chrome', 'quit Spotify', 'close the "
         "folders'. The app may still ask to save.", {"name": string("The app's name as the user said it.")},
         ("name",)),
    spec("open_project", "Open one of the user's EXISTING coding projects in VS Code, just to look at it. To build "
         "or change something, use start_coding instead.",
         {"name": string("The project's name, e.g. 'Jarvis'."),
          "task": string("What to build or fix, in the user's words, only if they said.")}, ("name",)),
    spec("open_folder", "Open a folder in File Explorer: Downloads, Documents, Desktop, Pictures, or a project.",
         {"name": string("The folder, e.g. 'downloads' or a project name.")}, ("name",)),
    spec("open_website", "Open a website in the browser.", {"url": string("The address, e.g. 'youtube.com'.")},
         ("url",)),
    spec("media_control", "Play/pause music, next/previous track, volume up/down, mute.",
         {"action": string("One of: play pause, next, previous, volume up, volume down, mute."),
          "times": {"type": "integer", "description": "How many steps, for volume. Default 1."}}, ("action",)),
    spec("lock_screen", "Lock the PC.", {}),
    spec("take_screenshot", "Take a screenshot of the PC screen.", {}),
    spec("show_interface", "Show or hide Jarvis's own window on the PC screen ('show yourself', 'open your "
         "interface', 'hide').", {"action": string("show or hide. Default show.")}),
    spec("run_command", "Anything else on the PC that needs a command (PowerShell). This only PROPOSES the "
         "command; the user must say yes before it runs.",
         {"command": string("The PowerShell command."),
          "purpose": string("A few words on what it does, e.g. 'show free disk space'.")}, ("command",)),
]

TOOLS: dict[str, Tool] = {
    "start_coding": (CodingArgs, start_coding), "close_app": (AppArgs, close_app),
    "open_app": (AppArgs, open_app), "open_project": (ProjectArgs, open_project),
    "open_folder": (FolderArgs, open_folder), "open_website": (WebsiteArgs, open_website),
    "media_control": (MediaArgs, media_control), "lock_screen": (NoArgs, lock_screen),
    "take_screenshot": (NoArgs, take_screenshot), "run_command": (CommandArgs, run_command),
    "show_interface": (InterfaceArgs, show_interface),
}
