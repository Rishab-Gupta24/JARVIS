"""Write the project's documents from the plan, the design and the task board (Python formats them, so they're
tidy whatever the model said). The user's list (7 Nov 2026):

    PRD.md           what is being built, what it includes, the problem it solves, core features
    Architecture.md  system overview and flow, directory structure, tech stack, how each part connects
    Rules.md         strict requirements, prohibited practices, approved vs prohibited libraries
    memory.md        decisions, changes, bugs, project context (appended by the loop as it works)
    design.md        colour scheme, shapes, layout, motion, and what it must NOT look like
    tasks.md         what to build, step by step

plus reference.md (the research), README.md and project.html (the live page)."""

from __future__ import annotations

import html
from datetime import datetime

from jarvis.studio import graph, sketch
from jarvis.studio.project import Project, State

ICON = {"todo": "☐", "testing": "🧪", "coding": "⌨", "review": "🔎", "done": "✅", "blocked": "⛔", "skipped": "⏭"}
DOCS = ("PRD.md", "Architecture.md", "Rules.md", "memory.md", "design.md", "tasks.md")


def prd(state: State) -> str:
    p = state.plan
    out = [f"# {p.get('title', state.name)} — Product Requirements", "", f"> {p.get('pitch', '')}", "",
           f"Requested: “{state.request}”", ""]
    if state.reference:
        out += [f"Inspired by the *mechanics* of {state.reference} (studied from public descriptions, see "
                "reference.md). Name, characters, story and art are original.", ""]
    out += ["## What we're building", p.get("pitch", ""), "",
            "## The problem it solves", p.get("problem", "") or "-", "",
            "## Who it's for", p.get("audience", "") or "-", "",
            "## Core features"]
    for pr in ("must", "should"):
        items = [f for f in p.get("features", []) if f.get("priority", "must") == pr]
        if items:
            out += [f"### {'Must have (first playable version)' if pr == 'must' else 'Should have'}",
                    *[f"- **{f['id']} {f['name']}**: {f['description']}" for f in items], ""]
    out += ["## What it includes", f"- Scenes: {', '.join(p.get('scenes', []))}",
            *[f"- {c['name']}: {c['description']}" for c in p.get("characters", [])],
            f"- Art: {p.get('art_style', '')}", "",
            "## Not included in this version", *([f"- {x}" for x in p.get("out_of_scope", [])] or ["- (nothing listed)"]),
            "", "## Core loop", *[f"{i}. {s}" for i, s in enumerate(p.get("core_loop", []), 1)], "",
            "## Controls", "| Action | Keys |", "|---|---|",
            *[f"| {c['action']} | {c['keys']} |" for c in p.get("controls", [])], "",
            "## Done means", "It starts, every scene is reachable, the core loop plays start to finish, all tests and "
            "the automatic play-through pass, and the playtest finds nothing the PRD promises missing."]
    return "\n".join(out)


def _tree(root, layout: dict[str, str], depth: int = 3) -> list[str]:
    skip = {".git", "__pycache__", ".pytest_cache", "node_modules"}
    lines = [f"{root.name}/"]

    def walk(folder, prefix, level):
        items = sorted((p for p in folder.iterdir() if p.name not in skip), key=lambda p: (p.is_file(), p.name))
        for i, p in enumerate(items):
            last = i == len(items) - 1
            rel = p.relative_to(root).as_posix() + ("/" if p.is_dir() else "")
            note = layout.get(rel.rstrip("/")) or layout.get(rel)
            lines.append(f"{prefix}{'└── ' if last else '├── '}{p.name}{'/' if p.is_dir() else ''}"
                         + (f"   # {note}" if note else ""))
            if p.is_dir() and level < depth and p.name != ".studio":
                walk(p, prefix + ("    " if last else "│   "), level + 1)

    walk(root, "", 1)
    return lines


def architecture(project: Project, state: State, stack) -> str:
    p = state.plan
    planned = [m["name"] for m in p.get("modules", [])]
    planned_edges = [(m["name"], u, "") for m in p.get("modules", []) for u in m.get("uses", []) if u in planned]
    mods, edges = graph.module_graph(project.root, state.stack)
    scenes, transitions = graph.scene_graph(p)
    integrations = p.get("integrations", [])
    named = {i["name"].lower() for i in integrations}
    out = [f"# {p.get('title', state.name)} — Architecture", "",
           "## System overview", f"{p.get('pitch', '')} Built as a {state.stack} game: a fixed-step loop feeds input "
           "(as action names) to the current scene, the scene updates the game's logic modules, then draws.", "",
           "## Flow", *[f"{i}. {s}" for i, s in enumerate(p.get("data_flow", []), 1)], "",
           "### Scenes (how the game runs)", "```mermaid", graph.mermaid(scenes, transitions), "```", ""]
    if state.last_smoke.get("scenes"):
        out += [f"Last automatic play-through: {' → '.join(state.last_smoke['scenes'][:20])}", ""]
    out += ["## Project directory", "```", *_tree(project.root, stack.layout), "```", "",
            "## Tech stack", *[f"- {t}" for t in stack.tech], "",
            "## How the parts connect", "| Connection | Purpose | How |", "|---|---|---|",
            *[f"| {i['name']} | {i['purpose']} | {i['how']} |" for i in integrations]]
    for name, why in (("payment", "no purchases"), ("github", "local git only; nothing is pushed"),
                      ("llm models", "the game itself uses no AI models; the studio's agents built it")):
        if not any(name.split()[0] in n for n in named):
            out.append(f"| {name.title() if name != 'llm models' else 'LLM models'} | none | {why} |")
    out += ["", "## Modules (planned)", *[f"- **{m['name']}**: {m['purpose']}"
                                          + (f" (uses {', '.join(m['uses'])})" if m.get("uses") else "")
                                          for m in p.get("modules", [])], "",
            "```mermaid", graph.mermaid(planned, planned_edges), "```", "",
            "## Modules (actual code, read from the imports)", "```mermaid", graph.mermaid(mods, edges), "```", "",
            "## Code map (classes, functions and who calls whom)", "```mermaid",
            graph.code_map_mermaid(graph.code_map(project.root, state.stack)), "```", "",
            "## One frame", "```mermaid", graph.mermaid(*graph.frame_flow(graph.code_map(project.root, state.stack)),
                                                         direction="TD"), "```", "",
            "## How the studio builds it", "```mermaid", graph.mermaid(graph.LOOP_NODES, graph.LOOP_EDGES), "```"]
    return "\n".join(out)


def rules(state: State, stack) -> str:
    d = state.plan.get("design", {})
    return "\n".join([
        f"# {state.plan.get('title', state.name)} — Rules", "",
        "The rule book every agent works by. Strict: a change that breaks a rule is not done.", "",
        "## Strict requirements (do)", *[f"- {r}" for r in stack.must + list(d.get("must", []))], "",
        "## Prohibited practices (avoid)", *[f"- {r}" for r in stack.avoid + list(d.get("avoid", []))], "",
        "## Approved libraries", *[f"- {a}" for a in stack.approved], "",
        "## Prohibited libraries", *[f"- {a}" for a in stack.prohibited], "",
        "## Write less code (Ponytail ladder)",
        "1. Reuse what the project already has.", "2. Use the standard library or the engine's built-ins.",
        "3. Write a few clear lines inline.", "4. Only then add a new function or module, as small as it can be.",
        "", "## Originality",
        f"Mechanics may follow {state.reference or 'the genre'}; names, characters, story, levels, art, music and UI "
        "are original. Never download, unpack or copy another game's files."])


def design(state: State) -> str:
    d = state.plan.get("design", {})
    if not d:
        return f"# {state.plan.get('title', state.name)} — Design\n\nArt style: {state.plan.get('art_style', '')}"
    return "\n".join([
        f"# {state.plan.get('title', state.name)} — Design", "",
        f"Art style: {state.plan.get('art_style', '')}", "",
        "## Colour scheme", "| Colour | Hex | Used for |", "|---|---|---|",
        *[f"| {c['name']} | {c['hex']} | {c['use']} |" for c in d.get("palette", [])], "",
        "The palette is also in the game's config module as PALETTE; use those names, not new colours.", "",
        "## Typography", d.get("typography", ""), "", "## Shapes and characters", d.get("shapes", ""), "",
        "## Layout", d.get("layout", ""), "", "## Motion and feedback", d.get("motion", ""), "",
        "## It must NOT look like", *[f"- {x}" for x in d.get("avoid_look", [])], "",
        *(["## Contrast fixes", *[f"- {a}" for a in d.get("adjusted", [])], ""] if d.get("adjusted") else []),
        "## Screens (concept sketches, drawn before building)",
        *[f"![{s['scene']}](sketches/{s['scene']}.svg)" for s in d.get("screens", [])], "",
        "Layout on a 0-100 grid (x, y = top-left):", sketch.describe(d, 4000)])


def concept_section(state: State) -> str:
    screens = state.plan.get("design", {}).get("screens", [])
    if not screens:
        return ""
    e = html.escape
    shots = state.last_playtest.get("shots", {}) if state.last_playtest else {}
    cards = []
    for s in screens:
        built = (f'<img src="../.studio/playtest/{e(shots[s["scene"]])}" alt="built: {e(s["scene"])}">'
                 if s["scene"] in shots else '<div class="muted placeholder">not built yet</div>')
        cards.append(f'<div class="pair"><div><div class="muted">planned</div><img src="sketches/{e(s["scene"])}.svg" '
                     f'alt="concept: {e(s["scene"])}"></div><div><div class="muted">built</div>{built}</div>'
                     f'<div class="cap">{e(s["scene"])}</div></div>')
    return f'<h2>Concept vs built</h2><div class="concepts">{"".join(cards)}</div>'


def tasks(state: State) -> str:
    c = state.counts()
    out = [f"# {state.plan.get('title', state.name)} — Tasks", "",
           f"What to build, step by step. {c['done']}/{len(state.tasks)} done · {c['blocked']} blocked · "
           f"{c['skipped']} skipped. A task that's too big for one go is split into smaller steps (shown indented), "
           "which come first; the task itself then checks the whole.", ""]
    step = 0
    for t in state.tasks:
        indent = "    " * t.depth
        if t.depth == 0:
            step += 1
        label = f"Step {step}" if t.depth == 0 else "↳"
        out += [f"{indent}- {ICON.get(t.status, '·')} **{label}: {t.id} {t.title}** `{t.status}`"
                + (" (drawing)" if t.kind == "visual" else ""),
                f"{indent}  needs {', '.join(t.depends_on) or 'nothing'} · files: {', '.join(t.files) or '-'}",
                *[f"{indent}  - [{'x' if t.status == 'done' else ' '}] {a}" for a in t.acceptance]]
        if t.notes:
            out.append(f"{indent}  note: {t.notes[-1][:200]}")
    return "\n".join(out)


def readme(state: State, play: str) -> str:
    p = state.plan
    return "\n".join([f"# {p.get('title', state.name)}", "", p.get("pitch", ""), "", "## Play", "", "```",
                      play, "```", "", "## Controls", "",
                      *[f"- {c['action']}: {c['keys']}" for c in p.get("controls", [])], "",
                      "Made by Jarvis Studio. Documents in docs/: PRD, Architecture, Rules, memory, design, tasks, "
                      "and project.html (the live page)."])


PAGE_CSS = """
:root { --bg:#f7f7f9; --fg:#1d1d22; --muted:#6b6b76; --card:#fff; --line:#d9d9e0; --accent:#3b6ef5; --ok:#1f9d55;
        --bad:#d64545; --warn:#c98a00; }
@media (prefers-color-scheme: dark) { :root { --bg:#121216; --fg:#ececf1; --muted:#9a9aa6; --card:#1c1c22;
        --line:#33333d; --accent:#7aa2ff; --ok:#4cc38a; --bad:#ff7070; --warn:#e6b450; } }
* { box-sizing: border-box; } body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 60px; }
h1 { margin: 0 0 4px; font-size: 28px; } h2 { font-size: 18px; margin: 28px 0 10px; }
.muted { color: var(--muted); } .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
.bar { height: 10px; background: var(--line); border-radius: 6px; overflow: hidden; margin: 10px 0 4px; }
.bar > div { height: 100%; background: var(--ok); }
.board { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
.col h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 6px; }
.task { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--line); border-radius:8px;
        padding:8px 10px; margin-bottom:8px; font-size:13px; }
.task.done { border-left-color: var(--ok); } .task.blocked, .task.skipped { border-left-color: var(--bad); }
.task.coding, .task.testing, .task.review { border-left-color: var(--accent); }
.graph { width: 100%; height: auto; max-height: 420px; }
.graph .node rect { fill: var(--card); stroke: var(--line); stroke-width: 1.5; }
.graph .node.visited rect { fill: color-mix(in srgb, var(--accent) 18%, var(--card)); stroke: var(--accent); }
.graph text { fill: var(--fg); font: 13px system-ui, sans-serif; text-anchor: middle; }
.graph .elabel { fill: var(--muted); font-size: 11px; }
.graph .edge { fill: none; stroke: var(--muted); stroke-width: 1.3; } .graph .edge.walked { stroke: var(--accent); stroke-width: 3; }
.graph .arrow { fill: var(--muted); }
.graph .edge.missing { stroke: var(--bad); stroke-dasharray: 6 4; } .graph .edge.extra { stroke: var(--ok); stroke-dasharray: 3 3; }
.graph .node.st-done rect { stroke: var(--ok); fill: color-mix(in srgb, var(--ok) 15%, var(--card)); }
.graph .node.st-blocked rect, .graph .node.st-skipped rect { stroke: var(--bad); fill: color-mix(in srgb, var(--bad) 12%, var(--card)); }
.graph .node.st-coding rect, .graph .node.st-testing rect, .graph .node.st-review rect { stroke: var(--accent); stroke-width: 2.5; }
.concepts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; background: var(--card); border: 1px solid var(--line);
        border-radius: 12px; padding: 10px; } .pair img { width: 100%; border-radius: 6px; border: 1px solid var(--line); }
.pair .cap { grid-column: 1 / -1; font-weight: 600; } .placeholder { aspect-ratio: 16/9; display: grid; place-items: center;
        border: 1px dashed var(--line); border-radius: 6px; }
.codemap .mname { font: 600 13px system-ui, sans-serif; text-anchor: start; } .codemap .member { font: 11px ui-monospace, monospace; text-anchor: start; fill: var(--muted); }
pre { white-space: pre-wrap; background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px;
      font-size: 12px; max-height: 260px; overflow: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; } td { border-top: 1px solid var(--line); padding: 4px 6px; }
.ok { color: var(--ok); } .bad { color: var(--bad); }
"""


def playtest_section(state: State, scenes: list[str], transitions: list) -> str:
    pt = state.last_playtest
    if not pt:
        return ""
    e = html.escape
    shots = "".join(f'<figure style="margin:0"><img src="../.studio/playtest/{e(name)}" alt="{e(scene)}" '
                    f'style="width:100%;border-radius:8px;border:1px solid var(--line)"><figcaption class="muted">'
                    f'{e(scene)}</figcaption></figure>' for scene, name in pt.get("shots", {}).items())
    found = pt.get("edges", [])
    edges, edge_cls = graph.scene_edge_classes(state.plan, found)
    problems = "".join(f"<li>{e(p)}</li>" for p in pt.get("problems", [])) or "<li>None: it plays as the PRD says.</li>"
    return (f'<h2>Playtest (round {pt.get("round")})</h2><div class="card">'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px">{shots}</div>'
            f'<p><b>Scenes the automatic player could reach</b> (highlighted). Arrows: <span class="ok">found by '
            f'exploring</span>, <span class="bad">planned but never reached</span>, green dashed = found but not '
            f'planned.</p>{graph.svg(scenes, edges, pt.get("reachable"), "Reachable scenes", edge_classes=edge_cls)}'
            f'<p><b>Problems found</b> (each became a fix task):</p><ul>{problems}</ul></div>')


def engineering_section(project: Project, state: State) -> str:
    """The detailed views: code map (who calls whom), one frame, and the task graph."""
    cmap = graph.code_map(project.root, state.stack)
    fnodes, fedges = graph.frame_flow(cmap)
    tnodes, tedges, tcls = graph.task_graph(state)
    calls = len(cmap["calls"])
    frame = (graph.svg(fnodes, fedges, title="One frame") if len(fnodes) > 2 else
             '<p class="muted">No scene update methods found yet.</p>')
    return (f'<h2>Code map</h2><div class="card"><div class="muted">{len(cmap["modules"])} modules, {calls} calls '
            f'between functions, read from the code.</div>{graph.code_map_svg(cmap)}</div>'
            f'<h2>One frame: what runs, in order</h2><div class="card">{frame}</div>'
            f'<h2>Task graph</h2><div class="card"><div class="muted">Arrows point from a task to the ones that need '
            f'it; split steps are separate boxes.</div>{graph.svg(tnodes, tedges, title="Tasks", node_classes=tcls)}</div>')


def page(project: Project, state: State, play: str) -> str:
    e = html.escape
    c = state.counts()
    total = max(1, len(state.tasks))
    pct = round(100 * c["done"] / total)
    columns = [("To do", ("todo",)), ("In progress", ("testing", "coding", "review")), ("Done", ("done",)),
               ("Blocked / skipped", ("blocked", "skipped"))]
    board = []
    for label, sts in columns:
        items = [t for t in state.tasks if t.status in sts]
        cards = "".join(f'<div class="task {t.status}"><b>{e(t.id)}</b> {e(t.title)}'
                        f'<div class="muted">{e(t.status)}{" · " + e(t.notes[-1][:90]) if t.notes else ""}</div></div>'
                        for t in items)
        board.append(f'<div class="col"><h3>{label} ({len(items)})</h3>{cards}</div>')
    mods, edges = graph.module_graph(project.root, state.stack)
    scenes, transitions = graph.scene_graph(state.plan)
    smoke = state.last_smoke
    tests = state.last_tests
    timeline = "".join(f"<tr><td class='muted'>{e(ev['at'][11:19])}</td><td>{e(ev['event'])}</td>"
                       f"<td>{e(str(ev.get('task', '') or ev.get('detail', '') or ev.get('title', '')))[:120]}</td></tr>"
                       for ev in project.events()[-40:][::-1])
    refresh = '<meta http-equiv="refresh" content="20">' if state.phase != "done" else ""
    p = state.plan
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}<title>{e(p.get('title', state.name))}</title><style>{PAGE_CSS}</style></head>
<body><main>
<h1>{e(p.get('title', state.name))}</h1>
<div class="muted">{e(p.get('pitch', state.request))}</div>
<div class="card" style="margin-top:16px">
  <b>Phase:</b> {e(state.phase)} · <b>Stack:</b> {e(state.stack)} · <b>Tasks:</b> {c['done']}/{len(state.tasks)} done,
  {c['blocked']} blocked · <b>Helper calls:</b> {state.helper_calls}
  <div class="bar"><div style="width:{pct}%"></div></div>
  <div class="muted">{e(state.message)} · updated {e(state.updated_at[11:19])} UTC</div>
</div>
<h2>How to play</h2><pre>{e(play)}</pre>
{concept_section(state)}
<h2>Task board</h2><div class="board">{''.join(board)}</div>
<h2>How the game runs: scenes and the last automatic play-through</h2>
<div class="card">{graph.svg(scenes, transitions, smoke.get('scenes'), 'Scenes')}
<div class="muted">{e(' → '.join(smoke.get('scenes', [])[:20]) or 'not run yet')}</div></div>
{playtest_section(state, scenes, transitions)}
{engineering_section(project, state)}
<h2>Modules (from the real code)</h2><div class="card">{graph.svg(mods, edges, title='Modules')}</div>
<h2>How the studio builds it</h2><div class="card">{graph.svg(graph.LOOP_NODES, graph.LOOP_EDGES, title='Studio loop')}</div>
<h2>Last checks</h2>
<div class="card">Tests: <span class="{'ok' if tests.get('ok') else 'bad'}">{e(tests.get('summary', 'not run yet'))}</span><br>
Play-through: <span class="{'ok' if smoke.get('ok') else 'bad'}">{e(smoke.get('summary', 'not run yet'))}</span></div>
<h2>Timeline</h2><div class="card"><table>{timeline}</table></div>
<p class="muted">Made by Jarvis Studio · {datetime.now().strftime('%d %b %Y %H:%M')}</p>
</main></body></html>"""


def write_all(project: Project, state: State, play: str) -> None:
    from jarvis.studio import stacks

    if project.docs.is_dir():  # earlier names; Windows keeps a file's old capitalisation, so read the real names
        for path in project.docs.iterdir():
            if path.name in ("features.md", "architecture.md"):
                path.unlink()
    if state.plan:
        stack = stacks.get(state.stack)
        project.write_doc("PRD.md", prd(state))
        project.write_doc("Architecture.md", architecture(project, state, stack))
        project.write_doc("Rules.md", rules(state, stack))
        project.write_doc("design.md", design(state))
        if state.plan.get("design"):  # the concept sketches (and phone versions for websites)
            sketch.write_all(project.docs, state.plan["design"], phone_too=state.stack == "site")
        project.write_doc("tasks.md", tasks(state))
        if not (project.docs / "memory.md").is_file():
            project.write_doc("memory.md", Project.MEMORY_HEAD)
    project.write_doc("project.html", page(project, state, play))
