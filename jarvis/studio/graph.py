"""Project graphs: which module uses which (read from the real code), the game's scenes and the path the
last smoke run took through them, and the studio's own loop. Output as Mermaid text (for architecture.md)
and as SVG drawn here with a simple layered layout (no packages, no internet: the page works offline)."""

from __future__ import annotations

import ast
import html
import re
from pathlib import Path

Edge = tuple[str, str, str]  # from, to, label
SKIP = {".git", ".studio", "__pycache__", "node_modules", "tests", "docs", ".pytest_cache"}


def module_graph(root: Path, stack: str) -> tuple[list[str], list[Edge]]:
    return _js_modules(root) if stack == "web" else _py_modules(root)


def _py_modules(root: Path) -> tuple[list[str], list[Edge]]:
    files = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if set(rel.parts) & SKIP or p.name == "__init__.py":
            continue
        files[".".join(rel.with_suffix("").parts)] = p
    edges = []
    for mod, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets = [f"{node.module}.{a.name}" for a in node.names] + [node.module]
            for t in targets:
                if t in files and t != mod and (mod, t, "") not in edges:
                    edges.append((mod, t, ""))
                    break
    return list(files), edges


def _js_modules(root: Path) -> tuple[list[str], list[Edge]]:
    files = {}
    for p in sorted(list(root.rglob("*.js")) + list(root.rglob("*.mjs"))):
        rel = p.relative_to(root)
        if not set(rel.parts) & SKIP:
            files[rel.with_suffix("").as_posix()] = p
    edges = []
    for mod, path in files.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        for spec in re.findall(r"""(?:import|from)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""", text):
            target = (path.parent / spec).resolve()
            try:
                name = target.relative_to(root.resolve()).with_suffix("").as_posix()
            except ValueError:
                continue
            if name in files and name != mod and (mod, name, "") not in edges:
                edges.append((mod, name, ""))
    return list(files), edges


def scene_graph(plan: dict) -> tuple[list[str], list[Edge]]:
    nodes = [s.lower() for s in plan.get("scenes", [])]
    edges = [(t["from"].lower(), t["to"].lower(), t.get("trigger", "")) for t in plan.get("transitions", [])]
    for a, b, _ in edges:
        for n in (a, b):
            if n not in nodes:
                nodes.append(n)
    return nodes, edges


LOOP_NODES = ["research", "plan", "tests first", "code", "run tests + smoke", "review", "done", "ask Claude Code",
              "blocked", "playtest"]
LOOP_EDGES = [("research", "plan", ""), ("plan", "tests first", "each task"), ("tests first", "code", ""),
              ("code", "run tests + smoke", ""), ("run tests + smoke", "code", "fail: fix"),
              ("run tests + smoke", "review", "pass"), ("review", "code", "changes"), ("review", "done", "ok"),
              ("run tests + smoke", "ask Claude Code", "stuck"), ("ask Claude Code", "run tests + smoke", ""),
              ("ask Claude Code", "blocked", "still failing"), ("done", "playtest", "all tasks done"),
              ("playtest", "tests first", "fix tasks")]


def _id(name: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def mermaid(nodes: list[str], edges: list[Edge], direction: str = "LR") -> str:
    lines = [f"flowchart {direction}"]
    for n in nodes:
        lines.append(f'    {_id(n)}["{n}"]')
    for a, b, label in edges:
        lab = f'|"{label.replace(chr(34), "")[:40]}"|' if label else ""
        lines.append(f"    {_id(a)} -->{lab} {_id(b)}")
    return "\n".join(lines)


def layers(nodes: list[str], edges: list[Edge]) -> dict[str, int]:
    """Column per node: longest path from a start node, ignoring edges that go back (cycles)."""
    out = {n: [] for n in nodes}
    for a, b, _ in edges:
        if a in out and b in out and a != b:
            out[a].append(b)
    back, state = set(), {}

    def dfs(n):
        state[n] = 1
        for m in out[n]:
            if state.get(m) == 1:
                back.add((n, m))
            elif m not in state:
                dfs(m)
        state[n] = 2

    for n in nodes:
        if n not in state:
            dfs(n)
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for a in nodes:
            for b in out[a]:
                if (a, b) not in back and layer[b] < layer[a] + 1:
                    layer[b] = layer[a] + 1
                    changed = True
        if not changed:
            break
    return layer


def svg(nodes: list[str], edges: list[Edge], visited: list[str] | None = None, title: str = "",
        node_classes: dict[str, str] | None = None, edge_classes: dict[tuple[str, str], str] | None = None) -> str:
    """A left-to-right diagram. Nodes in `visited` (e.g. the smoke run's scene path) are highlighted, and
    edges between consecutive visited nodes drawn bold. node_classes / edge_classes add CSS classes (task
    status colours; scene transitions the playtest found or couldn't reach)."""
    node_classes, edge_classes = node_classes or {}, edge_classes or {}
    if not nodes:
        return '<p class="muted">Nothing to draw yet.</p>'
    visited = visited or []
    walked = set(zip(visited, visited[1:]))
    layer = layers(nodes, edges)
    cols: dict[int, list[str]] = {}
    for n in nodes:
        cols.setdefault(layer[n], []).append(n)
    w = {n: max(70, 8 * len(n) + 24) for n in nodes}
    h, gap_y, gap_x, pad = 32, 18, 70, 20
    col_w = {c: max(w[n] for n in ns) for c, ns in cols.items()}
    x0, pos = pad, {}
    for c in sorted(cols):
        for i, n in enumerate(cols[c]):
            pos[n] = (x0 + (col_w[c] - w[n]) / 2, pad + i * (h + gap_y))
        x0 += col_w[c] + gap_x
    width = x0 - gap_x + pad
    height = pad * 2 + max(len(ns) for ns in cols.values()) * (h + gap_y) - gap_y + 30
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" class="graph" role="img" '
             f'aria-label="{html.escape(title)}">',
             '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrow"/></marker></defs>']
    for a, b, label in edges:
        if a not in pos or b not in pos:
            continue
        (ax, ay), (bx, by) = pos[a], pos[b]
        cls = "edge " + edge_classes.get((a, b), "walked" if (a, b) in walked else "")
        if a == b:
            d = f"M{ax + w[a] - 10},{ay} C{ax + w[a] + 30},{ay - 30} {ax + w[a] + 30},{ay + h + 30} {ax + w[a] - 10},{ay + h}"
            mid = (ax + w[a] + 26, ay + h / 2)
        elif layer[b] > layer[a]:
            sx, sy, ex, ey = ax + w[a], ay + h / 2, bx, by + h / 2
            d = f"M{sx},{sy} C{sx + 35},{sy} {ex - 35},{ey} {ex},{ey}"
            mid = ((sx + ex) / 2, (sy + ey) / 2 - 4)
        else:  # going back: arc below the boxes
            sx, sy, ex, ey = ax + w[a] / 2, ay + h, bx + w[b] / 2, by + h
            low = max(sy, ey) + 26 + 6 * abs(layer[a] - layer[b])
            d = f"M{sx},{sy} C{sx},{low} {ex},{low} {ex},{ey}"
            mid = ((sx + ex) / 2, low - 6)
        parts.append(f'<path d="{d}" class="{cls}" marker-end="url(#arr)"/>')
        if label:
            parts.append(f'<text x="{mid[0]:.0f}" y="{mid[1]:.0f}" class="elabel">{html.escape(label[:28])}</text>')
    for n in nodes:
        x, y = pos[n]
        cls = "node " + node_classes.get(n, "visited" if n in visited else "")
        parts.append(f'<g class="{cls}"><rect x="{x:.0f}" y="{y:.0f}" width="{w[n]}" height="{h}" rx="8"/>'
                     f'<text x="{x + w[n] / 2:.0f}" y="{y + h / 2 + 5:.0f}">{html.escape(n)}</text></g>')
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------- the detailed views (user asked 8 Nov 2026)

def code_map(root: Path, stack: str) -> dict:
    """Every module's classes (with methods) and functions, and which function calls which, read from the code:
    {"modules": {name: {"classes": {cls: [methods]}, "functions": [...]}}, "calls": [(mod, owner, mod2, target)]}.
    owner/target are "func" or "Class.method"."""
    return _js_code_map(root) if stack in ("web", "site") else _py_code_map(root)


def _py_code_map(root: Path) -> dict:
    modules, trees = {}, {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if set(rel.parts) & SKIP:
            continue
        parts = rel.with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if not name:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        trees[name] = tree
        modules[name] = {
            "classes": {n.name: [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
                        for n in tree.body if isinstance(n, ast.ClassDef)},
            "functions": [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]}
    calls = set()
    for mod, tree in trees.items():
        aliases: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    sub = f"{node.module}.{a.name}"
                    aliases[a.asname or a.name] = (sub, None) if sub in modules else (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in modules:
                        aliases[a.asname or a.name.split(".")[-1]] = (a.name, None)
        info = modules[mod]
        owners = [(f.name, f, None) for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        owners += [(f"{c.name}.{m.name}", m, c.name) for c in tree.body if isinstance(c, ast.ClassDef)
                   for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for owner, fn, cls in owners:
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                target, f = None, call.func
                if isinstance(f, ast.Name):
                    if f.id in info["functions"] or f.id in info["classes"]:
                        target = (mod, f.id)
                    elif f.id in aliases and aliases[f.id][1]:
                        target = aliases[f.id]
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    v = f.value.id
                    if v == "self" and cls and f.attr in info["classes"].get(cls, []):
                        target = (mod, f"{cls}.{f.attr}")
                    elif v in aliases:
                        m, sym = aliases[v]
                        target = (m, f.attr) if sym is None else (m, f"{sym}.{f.attr}")
                    elif v in info["classes"]:
                        target = (mod, f"{v}.{f.attr}")
                if target and target[0] in modules and (target[0], target[1]) != (mod, owner):
                    calls.add((mod, owner, target[0], target[1]))
    return {"modules": modules, "calls": sorted(calls)}


def _js_code_map(root: Path) -> dict:
    modules, texts = {}, {}
    for p in sorted(list(root.rglob("*.js")) + list(root.rglob("*.mjs"))):
        rel = p.relative_to(root)
        if set(rel.parts) & SKIP:
            continue
        name = rel.with_suffix("").as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        texts[name] = (p, text)
        classes = {}
        for m in re.finditer(r"class\s+(\w+)[^{]*\{", text):
            body = text[m.end(): m.end() + 6000]
            classes[m.group(1)] = [x for x in re.findall(r"^\s{2}(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", body, re.M)
                                   if x not in ("if", "for", "while", "switch", "catch")][:20]
        modules[name] = {"classes": classes,
                         "functions": re.findall(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", text, re.M)}
    calls = set()
    for name, (path, text) in texts.items():
        for names, spec in re.findall(r"""import\s*\{([^}]*)\}\s*from\s*['"](\.{1,2}/[^'"]+)['"]""", text):
            try:
                target = (path.parent / spec).resolve().relative_to(root.resolve()).with_suffix("").as_posix()
            except ValueError:
                continue
            if target not in modules:
                continue
            for imported in (n.split(" as ")[-1].strip() for n in names.split(",") if n.strip()):
                for m in re.finditer(rf"\b{re.escape(imported)}(?:\.(\w+))?\s*\(", text):
                    callee = f"{imported}.{m.group(1)}" if m.group(1) else imported
                    calls.add((name, "(module)", target, callee))
    return {"modules": modules, "calls": sorted(calls)}


def code_map_svg(cmap: dict, title: str = "Code map") -> str:
    """Module boxes listing their classes, methods and functions; arrows labelled with how many calls go between."""
    mods = list(cmap["modules"])
    if not mods:
        return '<p class="muted">No code yet.</p>'
    counts: dict[tuple[str, str], int] = {}
    for a, _, b, _ in cmap["calls"]:
        if a != b:
            counts[(a, b)] = counts.get((a, b), 0) + 1
    edges = [(a, b, f"{n} call{'s' if n > 1 else ''}") for (a, b), n in counts.items()]
    rows = {}
    for m in mods:
        info = cmap["modules"][m]
        lines = [f"class {c}" + (f": {', '.join(ms[:6])}" + ("…" if len(ms) > 6 else "") if ms else "")
                 for c, ms in info["classes"].items()] + [f"{f}()" for f in info["functions"]]
        rows[m] = lines[:12] + ([f"+{len(lines) - 12} more"] if len(lines) > 12 else [])
    layer = layers(mods, edges)
    cols: dict[int, list[str]] = {}
    for m in mods:
        cols.setdefault(layer[m], []).append(m)
    w = {m: min(300, max(120, 7 * max([len(m)] + [len(r) for r in rows[m]]) + 20)) for m in mods}
    h = {m: 30 + 15 * max(1, len(rows[m])) for m in mods}
    pad, gap_x, gap_y, pos, x0 = 20, 80, 20, {}, 20
    for c in sorted(cols):
        y = pad
        for m in cols[c]:
            pos[m] = (x0, y)
            y += h[m] + gap_y
        x0 += max(w[m] for m in cols[c]) + gap_x
    width = x0 - gap_x + pad
    height = max(pos[m][1] + h[m] for m in mods) + pad + 20
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" class="graph codemap" '
           f'role="img" aria-label="{html.escape(title)}"><defs><marker id="arr2" viewBox="0 0 10 10" refX="9" '
           'refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
           'class="arrow"/></marker></defs>']
    for a, b, label in edges:
        (ax, ay), (bx, by) = pos[a], pos[b]
        sx, sy, ex, ey = ax + w[a], ay + 15, bx, by + 15
        if layer[b] <= layer[a]:  # back or same column: loop around below
            sx, sy, ex, ey = ax + w[a] / 2, ay + h[a], bx + w[b] / 2, by + h[b]
            low = max(sy, ey) + 24
            d = f"M{sx},{sy} C{sx},{low} {ex},{low} {ex},{ey}"
            mid = ((sx + ex) / 2, low - 4)
        else:
            d = f"M{sx},{sy} C{sx + 40},{sy} {ex - 40},{ey} {ex},{ey}"
            mid = ((sx + ex) / 2, (sy + ey) / 2 - 4)
        out.append(f'<path d="{d}" class="edge" marker-end="url(#arr2)"/>'
                   f'<text x="{mid[0]:.0f}" y="{mid[1]:.0f}" class="elabel">{html.escape(label)}</text>')
    for m in mods:
        x, y = pos[m]
        out.append(f'<g class="node module"><rect x="{x}" y="{y}" width="{w[m]}" height="{h[m]}" rx="8"/>'
                   f'<text x="{x + 10}" y="{y + 19}" class="mname">{html.escape(m)}</text>')
        for i, r in enumerate(rows[m]):
            out.append(f'<text x="{x + 12}" y="{y + 36 + 15 * i}" class="member">{html.escape(r[:44])}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def code_map_mermaid(cmap: dict) -> str:
    """classDiagram: each class with its methods, each module's free functions as a box, and calls between them."""
    ident = lambda s: re.sub(r"\W", "_", s)  # noqa: E731
    lines = ["classDiagram"]
    for mod, info in cmap["modules"].items():
        for cls, methods in info["classes"].items():
            lines.append(f"    class {ident(cls)} {{")
            lines += [f"        +{m}()" for m in methods[:12]] + ["    }"]
        if info["functions"]:
            lines.append(f"    class {ident(mod)} {{")
            lines += ["        <<module>>"] + [f"        +{f}()" for f in info["functions"][:12]] + ["    }"]
    def box(mod: str, name: str) -> str:  # a class's own box, else the module's functions box
        head = name.split(".")[0]
        return ident(head) if head in cmap["modules"].get(mod, {}).get("classes", {}) else ident(mod)

    seen = set()
    for a, owner, b, target in cmap["calls"]:
        src, dst = box(a, owner), box(b, target)
        if src != dst and (src, dst) not in seen:
            seen.add((src, dst))
            lines.append(f"    {src} ..> {dst} : calls")
    return "\n".join(lines)


def frame_flow(cmap: dict, depth: int = 2) -> tuple[list[str], list[Edge]]:
    """What one frame does: the main loop steps the game, the current scene updates and calls into the logic
    (followed `depth` calls deep), then draws. Built from the code map's calls."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for a, owner, b, target in cmap["calls"]:
        out.setdefault((a, owner), []).append((b, target))
    nodes, edges = ["main loop", "Game.step"], [("main loop", "Game.step", "60 times a second")]
    starts = [(m, f"{c}.update") for m, info in cmap["modules"].items() if "scene" in m.lower()
              for c, ms in info["classes"].items() if "update" in ms]
    frontier = []
    for m, owner in starts:
        nodes.append(owner)
        edges.append(("Game.step", owner, "current scene"))
        frontier.append(((m, owner), 0))
    seen = set(starts)
    while frontier:
        (m, owner), d = frontier.pop(0)
        for b, target in out.get((m, owner), [])[:6]:
            if target not in nodes:
                nodes.append(target)
            if (owner, target, "") not in edges:
                edges.append((owner, target, ""))
            if d + 1 < depth and (b, target) not in seen:
                seen.add((b, target))
                frontier.append(((b, target), d + 1))
    if starts:
        nodes.append("draw")
        edges.append(("main loop", "draw", "then"))
    return nodes, edges


def task_graph(state) -> tuple[list[str], list[Edge], dict[str, str]]:
    """All tasks and split steps with their dependencies, coloured by status."""
    label = {t.id: f"{t.id} {t.title[:18]}" for t in state.tasks}
    nodes = [label[t.id] for t in state.tasks]
    edges = [(label[d], label[t.id], "") for t in state.tasks for d in t.depends_on if d in label]
    classes = {label[t.id]: f"st-{t.status}" for t in state.tasks}
    return nodes, edges, classes


def scene_edge_classes(plan: dict, found: list) -> tuple[list[Edge], dict[tuple[str, str], str]]:
    """Planned transitions plus what the playtest found: found = walked, planned but never reached = missing,
    found but not planned = extra (drawn too)."""
    planned = {(t["from"].lower(), t["to"].lower()): t.get("trigger", "") for t in plan.get("transitions", [])}
    seen = {(a, b) for a, b, *_ in found}
    edges = [(a, b, trig) for (a, b), trig in planned.items()]
    classes = {(a, b): ("walked" if (a, b) in seen else "missing") for (a, b) in planned} if found else {}
    for a, b, *rest in found:
        if (a, b) not in planned:
            edges.append((a, b, rest[0] if rest else ""))
            classes[(a, b)] = "extra"
    return edges, classes
