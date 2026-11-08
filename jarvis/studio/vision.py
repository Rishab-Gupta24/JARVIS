"""Vision review: the vision model (qwen2.5vl) looks at the playtest's screenshots and compares them with the design
guide and the concept sketch's layout. The user chose this instead of training a model on design (8 Nov 2026), once
per playtest round. It swaps onto the GPU for a minute or two (Jarvis answers slowly meanwhile) with a short
keep-alive, so qwen3 comes back as soon as the review is done.

One screenshot per request: the 7B vision model stays focused on one picture. Problems become visual fix tasks.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from jarvis.studio import sketch

SCHEMA = {"type": "object", "properties": {"problems": {"type": "array", "items": {"type": "object", "properties": {
    "title": {"type": "string"}, "evidence": {"type": "string"},
    "acceptance": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "evidence", "acceptance"]}}},
    "required": ["problems"]}

PROMPT = """You review a screenshot of the '{scene}' screen of a {what} that was built from a design guide.
Design guide: art style "{style}"; palette {palette}; shapes: {shapes}; it must NOT look like: {avoid}.
Planned layout of this screen (0-100 grid, x,y top-left): {layout}
List only problems you can SEE in the screenshot, most important first, at most {limit}:
- placeholder look: plain rectangles or boxes where characters, images or designed elements should be;
- text too small or cut off;
- layout clearly different from the plan (missing health bars, missing sections, things off-screen);
- empty or broken-looking screen.
Do not comment on exact colours or hex values (they are checked separately).
Each problem: a short fix-task title, the evidence (what you see), and 1-2 acceptance criteria for the fix.
If the screen matches the design reasonably, return an empty list. Answer in JSON."""


_COLOUR = re.compile(r"\b(colou?rs?|palette|hex|#[0-9a-f]{2,6}|background colou?r|hue)\b", re.I)


def _about_colour(text: str) -> bool:
    """Colour judgements are left to colors.py (exact contrast maths) and the PALETTE constants: the 7B vision model
    can't read hex values off a screenshot reliably."""
    return bool(_COLOUR.search(text))


def review(shots: dict[str, str], design: dict, plan: dict, client, what: str = "game", limit: int = 2,
           max_shots: int = 4) -> list[dict]:
    palette = ", ".join(f"{c['name']} {c['hex']}" for c in design.get("palette", [])) or "not set"
    layouts = {s["scene"]: s for s in design.get("screens", [])}
    order = [s["scene"] for s in design.get("screens", [])] + list(plan.get("scenes", []))
    picked = sorted(shots, key=lambda s: order.index(s) if s in order else 99)[:max_shots]
    problems: list[dict] = []
    for scene in picked:
        path = Path(shots[scene])
        if not path.is_file():
            continue
        if scene in layouts:
            layout = sketch.describe({"screens": [layouts[scene]]}, 900)
        else:  # a website's desktop/phone shot shows several sections: give all their layouts
            layout = sketch.describe(design, 1200) if what == "website" else "(no sketch)"
        prompt = PROMPT.format(scene=scene, what=what, style=plan.get("art_style", ""), palette=palette,
                               shapes=design.get("shapes", ""), avoid="; ".join(design.get("avoid_look", [])),
                               layout=layout, limit=limit)
        message = {"role": "user", "content": prompt, "images": [base64.b64encode(path.read_bytes()).decode()]}
        out = client.json([message], SCHEMA)
        for pr in out.get("problems", [])[:limit]:
            if not isinstance(pr, dict) or _about_colour(f"{pr.get('title', '')} {pr.get('evidence', '')}"):
                continue  # seen live: it called the palette's own background and text colours "not from the palette"
            if pr.get("title", "").strip() and pr.get("acceptance"):
                problems.append({"title": f"{scene}: {pr['title'].strip()}"[:120], "scene": scene, "kind": "visual",
                                 "evidence": f"vision review: {pr.get('evidence', '')}"[:400],
                                 "acceptance": [a for a in pr["acceptance"] if str(a).strip()][:3]})
    return problems
