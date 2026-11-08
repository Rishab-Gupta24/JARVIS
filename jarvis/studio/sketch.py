"""Concept sketches: a rough, hand-drawn-looking wireframe of every screen, drawn BEFORE the game or site is built,
from the designer's layout (design.md "screens"). The user asked for the studio to "sketch a rough idea of what it
wants to build" (8 Nov 2026). project.html shows each sketch next to the playtest's screenshot of what was built;
drawing tasks and the vision review get the same layout.

Pure SVG made here: seeded jitter so lines look drawn by hand, stick-figure characters, palette tints.
"""

from __future__ import annotations

import hashlib
import html
import random
from pathlib import Path

FONT = "'Segoe Print','Comic Sans MS','Bradley Hand',cursive"


def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16))


def _rough_rect(x, y, w, h, r: random.Random, j: float = 1.6) -> str:
    """Two slightly different passes around the box, like a pencil going over a line twice."""
    paths = []
    for _ in range(2):
        pts = [(x + r.uniform(-j, j), y + r.uniform(-j, j)), (x + w + r.uniform(-j, j), y + r.uniform(-j, j)),
               (x + w + r.uniform(-j, j), y + h + r.uniform(-j, j)), (x + r.uniform(-j, j), y + h + r.uniform(-j, j))]
        d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f} " + " ".join(f"L{px:.1f},{py:.1f}" for px, py in pts[1:]) + " Z"
        paths.append(d)
    return " ".join(paths)


def _stick(x, y, w, h, colour, r: random.Random) -> str:
    cx, head = x + w / 2, min(w, h) * 0.18
    top, hip, foot = y + head * 2, y + h * 0.62, y + h
    j = lambda: r.uniform(-1.2, 1.2)  # noqa: E731
    lines = [(cx, top, cx + j(), hip), (cx, top + h * 0.12, x + w * 0.1 + j(), top + h * 0.3),
             (cx, top + h * 0.12, x + w * 0.9 + j(), top + h * 0.22), (cx, hip, x + w * 0.2 + j(), foot),
             (cx, hip, x + w * 0.8 + j(), foot)]
    out = [f'<circle cx="{cx:.1f}" cy="{y + head:.1f}" r="{head:.1f}" fill="none" stroke="{colour}" stroke-width="2.4"/>']
    out += [f'<line x1="{a:.1f}" y1="{b:.1f}" x2="{c:.1f}" y2="{d:.1f}" stroke="{colour}" stroke-width="2.4" '
            'stroke-linecap="round"/>' for a, b, c, d in lines]
    return "".join(out)


def _ink(bg: str) -> str:
    from jarvis.studio.colors import luminance

    return "#f4f1ea" if luminance(bg) < 0.4 else "#2a2a2a"


def render(screen: dict, palette: list[dict], width: int = 480, height: int = 270, title: bool = True) -> str:
    from jarvis.studio.colors import background_of

    colours = {c["name"]: c["hex"] for c in palette}
    bg = background_of(palette)["hex"] if palette else "#20232a"
    ink = _ink(bg)
    sx, sy = width / 100, height / 100
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
           f'class="sketch" role="img" aria-label="Concept sketch: {html.escape(screen["scene"])}">',
           f'<rect width="{width}" height="{height}" fill="{bg}"/>']
    for i, el in enumerate(screen["elements"]):
        r = _rng(screen["scene"], i, el["label"])
        x, y, w, h = el["x"] * sx, el["y"] * sy, el["w"] * sx, el["h"] * sy
        col = colours.get(el["color"], ink)
        label = html.escape(el["label"])
        size = max(9, min(18, h * 0.45, w / max(4, len(el["label"])) * 1.7))
        kind = el["kind"]
        if kind == "character":
            out.append(_stick(x, y, w, h, col, r))
            out.append(f'<text x="{x + w / 2:.1f}" y="{y + h + 12:.1f}" fill="{ink}" font-size="10" font-family="{FONT}" '
                       f'text-anchor="middle">{label}</text>')
            continue
        if kind == "text":
            out.append(f'<text x="{x + w / 2:.1f}" y="{y + h * 0.7:.1f}" fill="{col}" font-size="{size:.0f}" '
                       f'font-family="{FONT}" text-anchor="middle">{label}</text>')
            continue
        if kind == "shape" or kind == "icon":
            rx, ry = (min(w, h) / 2,) * 2 if kind == "icon" else (w / 2, h / 2)
            out.append(f'<ellipse cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" fill="{col}" '
                       f'fill-opacity="0.35" stroke="{col}" stroke-width="2"/>')
            if kind == "icon" and label:
                out.append(f'<text x="{x + w / 2:.1f}" y="{y + h + 11:.1f}" fill="{ink}" font-size="9" '
                           f'font-family="{FONT}" text-anchor="middle">{label}</text>')
            continue
        fill_opacity = {"panel": 0.12, "button": 0.45, "bar": 0.0, "image": 0.18}.get(kind, 0.15)
        dash = ' stroke-dasharray="5 4"' if kind == "panel" else ""
        out.append(f'<path d="{_rough_rect(x, y, w, h, r)}" fill="{col}" fill-opacity="{fill_opacity}" stroke="{col}" '
                   f'stroke-width="1.8"{dash}/>')
        if kind == "bar":
            out.append(f'<rect x="{x + 2:.1f}" y="{y + 2:.1f}" width="{max(0, w * 0.65 - 4):.1f}" height="{max(0, h - 4):.1f}" '
                       f'fill="{col}" fill-opacity="0.75"/>')
        if kind == "image":
            out.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + w:.1f}" y2="{y + h:.1f}" stroke="{col}" stroke-opacity="0.6"/>'
                       f'<line x1="{x + w:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y + h:.1f}" stroke="{col}" stroke-opacity="0.6"/>')
        if label:
            ty = y + h / 2 + size * 0.35 if kind in ("button", "image") else (y - 4 if kind == "bar" else y + size + 3)
            anchor, tx = ("middle", x + w / 2) if kind in ("button", "image") else ("start", x + 4)
            out.append(f'<text x="{tx:.1f}" y="{ty:.1f}" fill="{ink}" font-size="{min(size, 13):.0f}" font-family="{FONT}" '
                       f'text-anchor="{anchor}">{label}</text>')
    if title:
        out.append(f'<text x="8" y="{height - 8}" fill="{ink}" fill-opacity="0.55" font-size="11" '
                   f'font-family="{FONT}">concept: {html.escape(screen["scene"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def stacked(screen: dict) -> dict:
    """The phone version of a website section: elements one under another, full width."""
    els = sorted(screen["elements"], key=lambda e: (round(e["y"] / 5), e["x"]))
    y, out = 3.0, []
    for e in els:
        h = max(4.0, min(30.0, e["h"] * 0.6))
        out.append({**e, "x": 6.0, "w": 88.0, "y": y, "h": h})
        y += h + 2.5
        if y > 96:
            break
    return {"scene": screen["scene"], "elements": out}


def write_all(docs: Path, design: dict, phone_too: bool = False) -> list[str]:
    """docs/sketches/<scene>.svg (and <scene>-phone.svg for websites). Returns the written names."""
    screens, palette = design.get("screens", []), design.get("palette", [])
    if not screens:
        return []
    folder = docs / "sketches"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for sc in screens:
        name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in sc["scene"])
        (folder / f"{name}.svg").write_text(render(sc, palette), encoding="utf-8")
        written.append(f"{name}.svg")
        if phone_too:
            (folder / f"{name}-phone.svg").write_text(render(stacked(sc), palette, 220, 400), encoding="utf-8")
            written.append(f"{name}-phone.svg")
    return written


def describe(design: dict, chars: int = 1500) -> str:
    """The layouts as text, for drawing tasks and the vision review."""
    lines = []
    for sc in design.get("screens", []):
        items = "; ".join(f"{e['kind']} '{e['label']}' at ({e['x']:.0f},{e['y']:.0f}) {e['w']:.0f}x{e['h']:.0f} "
                          f"{e['color']}" for e in sc["elements"])
        lines.append(f"- {sc['scene']}: {items}")
    return "\n".join(lines)[:chars]
