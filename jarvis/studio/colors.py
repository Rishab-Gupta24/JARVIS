"""Colour checks for the design guide: WCAG contrast, fixed in Python before anything is built.

Text needs 4.5:1 against the background, characters and important shapes 3:1 (WCAG AA). The designer model
picks pretty palettes but often too little contrast; a colour that falls short is moved towards white or black
until it passes, and the change is reported so it goes into memory.md.
"""

from __future__ import annotations

import re

TEXT_WORDS = ("text", "ui", "label", "font", "title", "heading", "body")
FIGURE_WORDS = ("player", "opponent", "enemy", "hero", "character", "fighter", "accent", "button", "primary",
                "danger", "health")


def rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(c: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in c)


def luminance(hex_: str) -> float:
    def ch(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(hex_)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _towards(hex_: str, target: tuple[int, int, int], t: float) -> str:
    c = rgb(hex_)
    return to_hex(tuple(c[i] + (target[i] - c[i]) * t for i in range(3)))


def push(hex_: str, against: str, need: float) -> str:
    """The nearest colour (same hue, lighter or darker) with at least `need` contrast against `against`."""
    if contrast(hex_, against) >= need:
        return hex_
    target = (255, 255, 255) if luminance(against) < 0.4 else (0, 0, 0)
    for step in range(1, 21):
        c = _towards(hex_, target, step / 20)
        if contrast(c, against) >= need:
            return c
    return to_hex(target)


def _role(name: str, use: str, words: tuple[str, ...]) -> bool:
    text = f"{name} {use}".lower()
    return any(re.search(rf"\b{w}", text) for w in words)


def background_of(palette: list[dict]) -> dict:
    for c in palette:
        if _role(c["name"], c.get("use", ""), ("background", "bg", "sky", "backdrop")):
            return c
    return palette[0]


def fix_contrast(palette: list[dict]) -> tuple[list[dict], list[str]]:
    if not palette:
        return palette, []
    bg = background_of(palette)["hex"]
    out, notes = [], []
    for c in palette:
        c = dict(c)
        if c["hex"] != bg:
            need = 4.5 if _role(c["name"], c.get("use", ""), TEXT_WORDS) else (
                3.0 if _role(c["name"], c.get("use", ""), FIGURE_WORDS) else 0)
            fixed = push(c["hex"], bg, need) if need else c["hex"]
            if fixed.lower() != c["hex"].lower():
                notes.append(f"{c['name']} {c['hex']} → {fixed} (contrast {contrast(c['hex'], bg):.1f}:1 → "
                             f"{contrast(fixed, bg):.1f}:1 against the background)")
                c["hex"] = fixed
        out.append(c)
    return out, notes
