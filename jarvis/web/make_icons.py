"""Draw the Home Screen icons: `.venv\\Scripts\\python jarvis\\web\\make_icons.py`."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent


def icon(size: int) -> Image.Image:
    s = size * 4  # draw large, shrink for smooth edges
    img = Image.new("RGB", (s, s), (11, 18, 32))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    c, r = s / 2, s * 0.30
    g.ellipse((c - r, c - r, c + r, c + r), outline=(56, 189, 248, 255), width=int(s * 0.07))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.035))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)
    d.ellipse((c - r, c - r, c + r, c + r), outline=(186, 230, 253), width=int(s * 0.035))
    dot = s * 0.07
    d.ellipse((c - dot, c - dot, c + dot, c + dot), fill=(224, 242, 254))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size in (180, 192, 512):
        icon(size).save(HERE / f"icon-{size}.png", optimize=True)
    icon(256).save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    print("icons written to", HERE)
