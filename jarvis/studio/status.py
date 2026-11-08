"""Where the studio is, in one sentence: for the CLI, for "how's the game going?", and for the HUD."""

from __future__ import annotations

from pathlib import Path

from jarvis.studio.project import Project, State


def projects(folder: Path) -> list[Project]:
    """Studio projects in the projects folder, newest activity first."""
    if not folder.is_dir():
        return []
    found = [Project(p) for p in folder.iterdir() if (p / ".studio" / "state.json").is_file()]
    return sorted(found, key=lambda pr: pr.state_file.stat().st_mtime, reverse=True)


def find(folder: Path, name: str | None) -> Project | None:
    items = projects(folder)
    if not name:
        return items[0] if items else None
    path = Path(name)
    if (path / ".studio" / "state.json").is_file():
        return Project(path)
    low = name.lower().replace(" ", "-")
    for pr in items:
        title = pr.load().plan.get("title", "").lower().replace(" ", "-")
        if low in (pr.root.name.lower(), title) or low in pr.root.name.lower():
            return pr
    return None


def describe(state: State) -> str:
    title = state.plan.get("title") or state.name
    c = state.counts()
    total = len(state.tasks)
    if state.phase == "research":
        return f"{title}: researching {state.reference or 'the idea'} before planning."
    if state.phase == "plan":
        return f"{title}: writing the PRD and the task list."
    blocked = f" {c['blocked']} task{'s' if c['blocked'] != 1 else ''} blocked." if c["blocked"] else ""
    if state.phase == "done":
        return f"{title} is ready: {c['done']} of {total} tasks done.{blocked}"
    doing = ""
    if state.current:
        task = next((t for t in state.tasks if t.id == state.current), None)
        if task:
            doing = f" Now on {task.id}, {task.title}."
    paused = " It's paused; say resume or run it again to carry on." if state.message.startswith("Paused") else ""
    phase = "final checks" if state.phase == "integrate" else "building"
    return f"{title}: {phase}, {c['done']} of {total} tasks done.{doing}{blocked}{paused}"


def hud_line(folder: Path) -> str:
    items = projects(folder)
    if not items:
        return ""
    try:
        state = items[0].load()
    except (OSError, ValueError):
        return ""
    title = state.plan.get("title") or state.name
    c = state.counts()
    if state.phase in ("research", "plan"):
        return f"Studio: {title} · {state.phase}"
    where = "done" if state.phase == "done" else (state.current or state.phase)
    return f"Studio: {title} {c['done']}/{len(state.tasks)} · {where}"
