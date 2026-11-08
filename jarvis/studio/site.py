"""The `site` stack: animated websites (user asked 8 Nov 2026). Plain HTML, CSS and ES modules, no packages.

- Skeleton: semantic index.html (header/nav, hero section, footer), css/styles.css (palette as CSS variables, type
  scale, scroll-reveal transitions, a reduced-motion rule), js/main.js (IntersectionObserver reveals, phone menu),
  tests/site.test.mjs (node --test: structure, alt text, anchors, no external resources, motion + reduced motion,
  and the reveal logic with a fake observer).
- Automatic check: .studio/site_check.html loads the page into a same-origin iframe at 1280x800 and 390x844, catches
  every script error, scrolls through to trigger reveals, and reports sections, animations, sideways overflow on
  phones, missing alt text, broken anchors and external resources.
- Screenshots: headless Edge, full page at desktop and phone widths (for the playtest and the vision review).
"""

from __future__ import annotations

import functools
import html as _html
import http.server
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

from jarvis.studio.stacks import (SMOKE_TIMEOUT, TEST_TIMEOUT, SmokeResult, Stack, TestResult, _browser, _esc, _Quiet,
                                  tap_failures,
                                  _run)

INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="css/styles.css">
<script type="module" src="js/main.js"></script>
</head>
<body>
<header class="site-header">
  <a class="brand" href="#hero">{title}</a>
  <button class="nav-toggle" aria-expanded="false" aria-controls="nav">Menu</button>
  <nav id="nav"><a href="#hero">Home</a></nav>
</header>
<main>
  <section id="hero" class="hero">
    <h1 class="reveal">{title}</h1>
    <p class="reveal">A short line about what this is.</p>
  </section>
</main>
<footer class="site-footer"><p>&copy; <span data-year></span> {title}</p></footer>
</body>
</html>
"""

CSS = """/* Made by Jarvis Studio. Every colour comes from the palette variables; the palette block at the end
   (from design.md) maps the base names (--bg, --text, --accent...) to the design's colours. */
:root {
  --bg: #0f1115; --surface: #171a21; --text: #eef0f4; --muted: #a7adbb; --accent: #6c8cff;
  --space: clamp(48px, 8vw, 120px); --radius: 14px; --max: 1160px; --ease: cubic-bezier(.2, .7, .2, 1);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text); font: 17px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
img { max-width: 100%; display: block; }
a { color: var(--accent); }
.site-header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 14px 20px; background: color-mix(in srgb, var(--bg) 85%, transparent); backdrop-filter: blur(8px); }
.brand { color: var(--text); font-weight: 700; text-decoration: none; }
nav { display: flex; gap: 20px; }
nav a { color: var(--muted); text-decoration: none; }
nav a:hover, nav a:focus-visible { color: var(--text); }
.nav-toggle { display: none; }
section { padding: var(--space) 20px; max-width: var(--max); margin: 0 auto; }
.hero { min-height: 80vh; display: grid; align-content: center; gap: 16px; }
h1 { font-size: clamp(2.2rem, 6vw, 4.5rem); line-height: 1.05; margin: 0; }
.reveal { opacity: 0; transform: translateY(24px); transition: opacity .7s var(--ease), transform .7s var(--ease); }
.reveal.is-visible { opacity: 1; transform: none; }
.reveal:nth-child(2) { transition-delay: 80ms; }
.reveal:nth-child(3) { transition-delay: 160ms; }
@keyframes rise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
.site-footer { padding: 32px 20px; color: var(--muted); text-align: center; }
@media (max-width: 640px) {
  .nav-toggle { display: inline-block; }
  nav { display: none; }
  nav.open { display: flex; flex-direction: column; position: absolute; top: 56px; right: 16px; padding: 12px 16px;
    background: var(--surface); border-radius: var(--radius); }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  .reveal { opacity: 1; transform: none; transition: none; }
  *, *::before, *::after { animation: none !important; }
}
"""

MAIN_JS = """// Made by Jarvis Studio: scroll reveals, the phone menu and the footer year. Plain ES module, no libraries.
// Functions take their dependencies as arguments so node --test can check them without a browser.

export function reveal(root = globalThis.document, Observer = globalThis.IntersectionObserver) {
  const items = [...root.querySelectorAll(".reveal")];
  if (!Observer) {
    items.forEach((el) => el.classList.add("is-visible"));
    return items.length;
  }
  const io = new Observer((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        io.unobserve(entry.target);
      }
    }
  }, { threshold: 0.15 });
  items.forEach((el) => io.observe(el));
  return items.length;
}

export function navToggle(root = globalThis.document) {
  const button = root.querySelector(".nav-toggle");
  const nav = root.querySelector("nav");
  if (!button || !nav) return false;
  button.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
  });
  return true;
}

if (globalThis.document) {
  reveal();
  navToggle();
  const year = document.querySelector("[data-year]");
  if (year) year.textContent = String(new Date().getFullYear());
}
"""

SITE_TEST = """// The skeleton's own checks (made by Jarvis Studio). Run: node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { reveal } from "../js/main.js";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const css = readFileSync(new URL("../css/styles.css", import.meta.url), "utf8");

test("page has header, main and footer", () => {
  for (const tag of ["<header", "<main", "<footer"]) assert.ok(html.includes(tag), tag);
});

test("every image has alt text", () => {
  for (const img of html.match(/<img\\b[^>]*>/gi) || []) assert.match(img, /\\balt=/, img);
});

test("nav links point to sections that exist", () => {
  const ids = new Set([...html.matchAll(/\\bid="([^"]+)"/g)].map((m) => m[1]));
  for (const m of html.matchAll(/href="#([^"]+)"/g)) assert.ok(ids.has(m[1]), `#${m[1]}`);
});

test("no external resources (CDNs, web fonts)", () => {
  assert.doesNotMatch(html, /(src|href)=["'](https?:)?\\/\\//);
  assert.doesNotMatch(css, /url\\(["']?(https?:)?\\/\\//);
});

test("motion is defined and respects reduced motion", () => {
  assert.match(css, /@keyframes|transition/);
  assert.match(css, /prefers-reduced-motion/);
});

test("reveal shows items as they scroll into view", () => {
  const el = { classList: new Set(), };
  el.classList.add = el.classList.add.bind(el.classList);
  const root = { querySelectorAll: () => [el] };
  class FakeObserver {
    constructor(cb) { this.cb = cb; }
    observe(target) { this.cb([{ isIntersecting: true, target }]); }
    unobserve() {}
  }
  assert.equal(reveal(root, FakeObserver), 1);
  assert.ok(el.classList.has("is-visible"));
});
"""

CHECK = """<!doctype html>
<html><head><meta charset="utf-8"><title>page check</title><style>body{margin:0}iframe{border:0;display:block}</style></head>
<body><pre id="result">pending</pre>
<script>
// Jarvis Studio page check (written by the studio; safe to delete).
const out = (d) => { document.getElementById("result").textContent = JSON.stringify(d); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const base = new URL("../", location.href).href;
const hook = `<base href="${base}"><script>window.__errors=[];addEventListener("error",e=>__errors.push(String(e.message||e)));` +
  `addEventListener("unhandledrejection",e=>__errors.push("promise: "+String(e.reason)));` +
  `const __ce=console.error;console.error=(...a)=>{__errors.push(a.join(" "));__ce(...a)};<\\/script>`;
async function viewport(name, w, h, page) {
  const f = document.createElement("iframe");
  f.width = w; f.height = h;
  const loaded = new Promise((r) => f.addEventListener("load", r, { once: true }));
  f.srcdoc = page.replace(/<head[^>]*>/i, (m) => m + hook);
  document.body.appendChild(f);
  await loaded;
  await wait(400);
  const win = f.contentWindow, doc = f.contentDocument;
  const total = doc.documentElement.scrollHeight;
  for (let y = 0; y <= total; y += Math.max(200, h / 2)) { win.scrollTo(0, y); await wait(120); }
  win.scrollTo(0, total); await wait(900);
  let keyframes = 0, reduced = false;
  for (const sheet of doc.styleSheets) {
    let rules = []; try { rules = sheet.cssRules; } catch (e) { continue; }
    const walk = (list) => { for (const r of list) {
      if (r.type === 7) keyframes++;
      if (r.conditionText && r.conditionText.includes("prefers-reduced-motion")) reduced = true;
      if (r.cssRules) walk(r.cssRules);
    } };
    walk(rules);
  }
  const els = [...doc.querySelectorAll("body *")].slice(0, 600);
  const transitions = els.filter((e) => { const s = win.getComputedStyle(e); return s.transitionDuration && s.transitionDuration.split(",").some((d) => parseFloat(d) > 0); }).length;
  const animated = els.filter((e) => { const s = win.getComputedStyle(e); return s.animationName && s.animationName !== "none"; }).length;
  const ids = [...doc.querySelectorAll("[id]")].map((e) => e.id);
  const result = {
    name, width: w, height: total, errors: win.__errors || [],
    sections: [...doc.querySelectorAll("section[id], header[id], footer[id]")].map((e) => e.id),
    ids, keyframes, transitions, animated, reduced_motion: reduced,
    reveal_total: doc.querySelectorAll(".reveal").length, reveal_shown: doc.querySelectorAll(".reveal.is-visible").length,
    overflow: doc.documentElement.scrollWidth > w + 2, scroll_width: doc.documentElement.scrollWidth,
    images_without_alt: [...doc.images].filter((i) => !i.hasAttribute("alt")).length,
    broken_anchors: [...doc.querySelectorAll('a[href^="#"]')].map((a) => a.getAttribute("href")).filter((h) => h.length > 1 && !ids.includes(h.slice(1))),
    external: [...doc.querySelectorAll("[src],[href]")].filter((e) => e.tagName !== "BASE").map((e) => e.getAttribute("src") || e.getAttribute("href")).filter((u) => /^(https?:)?\\/\\//.test(u)),
  };
  f.remove();
  return result;
}
(async () => {
  try {
    const page = await (await fetch(base + "index.html")).text();
    const views = [await viewport("desktop", 1280, 800, page), await viewport("phone", 390, 844, page)];
    out({ ok: views.every((v) => v.errors.length === 0), viewports: views });
  } catch (err) {
    out({ ok: false, error: String(err && err.stack || err) });
  }
})();
</script></body></html>
"""

SITE_PLANNING = """- 8 to 18 tasks, ids "T1", "T2", ... in build order. The skeleton (index.html with header/nav, a hero section and a
  footer, css/styles.css with palette variables and scroll reveals, js/main.js) already exists: build on it.
  Order: content and structure of each section, then the layout per breakpoint, then the animations (hero entrance,
  scroll reveals with stagger, hover effects, one signature effect like an SVG line drawing or a canvas particle
  hero), then polish (focus styles, reduced motion, final spacing).
- "scenes" are the page's sections in order, lowercase ids used as the sections' id attributes (e.g. hero, about,
  work, contact). "transitions" are the navigation: from the section a link is in (usually "hero" for the nav) to
  the section it scrolls to, trigger "nav link" or "button". characters can be an empty list. "controls" are the
  interactions (scroll, click nav, hover cards, open the phone menu).
- Acceptance criteria are checks a test can make by reading the files with node:fs or by calling js functions with
  fakes: "index.html has <section id=\\"work\\"> with at least 3 .card elements", "styles.css defines @keyframes
  hero-in and uses it on .hero h1", "reveal() marks an intersecting element is-visible"."""


class SiteStack(Stack):
    name = "site"
    what = "website"
    planning = SITE_PLANNING
    config_file = "css/styles.css"
    TITLE_LINES = {"index.html": (r"<title>.*?</title>", lambda t: f"<title>{_esc(t)}</title>")}
    conventions = """Technology: an animated website in plain HTML, CSS and JavaScript ES modules: no npm packages, no
frameworks, no CDN scripts or web fonts, no build step. Already in the project (made by the studio, working):
- index.html: header (brand + nav + phone menu button), <main> with <section id="hero">, footer. Add one <section
  id="..."> per planned section inside <main>, and a nav link for each.
- css/styles.css: palette variables (--bg, --surface, --text, --muted, --accent, plus every design colour as
  --<name>), spacing and type scale, .reveal / .reveal.is-visible scroll transitions, a phone breakpoint, and the
  prefers-reduced-motion rule. Put all styles here, using the variables (no new hex colours, no inline styles).
- js/main.js: reveal() (IntersectionObserver adds is-visible) and navToggle(), both taking their dependencies as
  arguments so tests can pass fakes. Add new behaviour as exported functions the same way.
Tests: tests/<task id>.test.mjs with node:test and node:assert/strict; read files with node:fs (readFileSync with
new URL("../index.html", import.meta.url)) to check structure and CSS, and import functions from "../js/main.js" to
check behaviour with fakes. Use run_tests for all tests and run_smoke for the page check (desktop + phone)."""
    tech = ["HTML5 (semantic sections)", "CSS3: custom properties, grid/flex, transitions, @keyframes, clamp()",
            "JavaScript ES modules (IntersectionObserver, requestAnimationFrame, canvas/SVG when needed)",
            "node 24 (node --test, static and logic checks)", "headless Edge (page check at 1280 and 390 px, screenshots)",
            "git (one commit per finished task)"]
    approved = ["browser built-ins (DOM, IntersectionObserver, requestAnimationFrame, canvas, SVG, Web Animations)",
                "CSS features (custom properties, grid, flex, transitions, @keyframes, clamp)",
                "node:test, node:assert and node:fs (tests only)"]
    prohibited = ["npm packages and frameworks (React, Vue, GSAP, Tailwind, Bootstrap, jQuery...)", "CDN scripts",
                  "web fonts from other sites", "network calls", "images from other sites (draw with CSS/SVG/canvas)"]
    must = ["Every planned section is a <section id=\"...\"> inside <main>, with a nav link to it.",
            "All colours come from the palette variables in css/styles.css; no hex values elsewhere.",
            "Animate only transform and opacity; every animation has a prefers-reduced-motion fallback.",
            "Nothing is wider than a 390 px phone screen (no sideways scrolling).",
            "Every image has alt text; links and buttons have visible focus styles.",
            "JavaScript logic lives in exported functions that take their dependencies, so tests can use fakes.",
            "Each task's tests in tests/<task id>.test.mjs; all tests and the page check must pass."]
    avoid = ["Editing tests to make them pass.", "Scroll event handlers for animations (use IntersectionObserver).",
             "Inline styles and !important (except in the reduced-motion rule).", "Placeholders like 'Lorem ipsum' in "
             "the finished page.", "Copying text, logos or images from an existing website (Ponytail and originality)."]
    layout = {"index.html": "the page: header/nav, one section per planned section, footer",
              "css/styles.css": "palette variables, layout, animations, breakpoints, reduced motion",
              "js/main.js": "reveal(), navToggle() and other behaviour as exported functions",
              "tests/": "node:test files, one per task", "docs/": "PRD, Architecture, Rules, memory, design, tasks, "
              "project.html", ".studio/": "studio state, page check, screenshots"}

    def skeleton(self, title: str) -> dict[str, str]:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "site"
        return {"package.json": json.dumps({"name": slug, "private": True, "type": "module",
                                            "scripts": {"test": "node --test"}}, indent=2) + "\n",
                "index.html": INDEX.format(title=_esc(title)), "css/styles.css": CSS, "js/main.js": MAIN_JS,
                "tests/site.test.mjs": SITE_TEST, ".gitignore": "node_modules/\n.studio/playtest/\n"}

    def set_title(self, root: Path, title: str) -> None:
        index = root / "index.html"
        if index.is_file():
            text = index.read_text(encoding="utf-8").replace("Untitled Game", _esc(title)).replace("Untitled Site",
                                                                                                    _esc(title))
            index.write_text(re.sub(r"<title>.*?</title>", f"<title>{_esc(title)}</title>", text, count=1),
                             encoding="utf-8")

    def palette_code(self, palette: list[dict]) -> str:
        from jarvis.studio.colors import TEXT_WORDS, _role, background_of

        var = lambda n: "--" + re.sub(r"[^a-z0-9]+", "-", n.strip().lower()).strip("-")  # noqa: E731
        lines = [f"  {var(c['name'])}: {c['hex']};" for c in palette]
        bg = background_of(palette)
        text = next((c for c in palette if c is not bg and _role(c["name"], c.get("use", ""), TEXT_WORDS)), None)
        accent = next((c for c in palette if c is not bg and c is not text), None)
        aliases = [f"  --bg: var({var(bg['name'])});"]
        if text:
            aliases.append(f"  --text: var({var(text['name'])});")
        if accent:
            aliases.append(f"  --accent: var({var(accent['name'])});")
        return "/* palette (design.md) */\n:root {\n" + "\n".join(lines + aliases) + "\n}\n/* end palette */\n"

    def test_file(self, task_id: str) -> str:
        return f"tests/{task_id.lower()}.test.mjs"

    def available(self) -> str | None:
        if not shutil.which("node"):
            return "node isn't installed"
        if not _browser():
            return "no Edge or Chrome for the page check"
        return None

    def run_tests(self, root: Path) -> TestResult:
        node = shutil.which("node")
        if not node:
            return TestResult(False, output="node isn't installed")
        code, out = _run([node, "--test", "--test-reporter=tap"], root, TEST_TIMEOUT)
        passed = sum(int(n) for n in re.findall(r"^# pass (\d+)", out, re.M))
        failed = sum(int(n) for n in re.findall(r"^# fail (\d+)", out, re.M))
        return TestResult(code == 0 and passed > 0, passed, failed, out.strip()[-6000:], tap_failures(out))

    def _serve(self, root: Path):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def check(self, root: Path) -> dict:
        """The page check at desktop and phone sizes (see CHECK)."""
        browser = _browser()
        if not browser:
            return {"ok": False, "error": "no Edge or Chrome for the page check"}
        (root / ".studio").mkdir(exist_ok=True)
        (root / ".studio" / "site_check.html").write_text(CHECK, encoding="utf-8")
        server = self._serve(root)
        try:
            with tempfile.TemporaryDirectory() as profile:
                url = f"http://127.0.0.1:{server.server_address[1]}/.studio/site_check.html"
                code, out = _run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                                  "--no-default-browser-check", "--disable-extensions", f"--user-data-dir={profile}",
                                  "--virtual-time-budget=30000", "--dump-dom", url], root, SMOKE_TIMEOUT)
        finally:
            server.shutdown()
            server.server_close()
        m = re.search(r'<pre id="result">(.*?)</pre>', out, re.S)
        if not m:
            return {"ok": False, "error": f"the page check didn't report (exit {code}): {out[-1500:]}"}
        try:
            return json.loads(_html.unescape(m.group(1)))
        except json.JSONDecodeError:
            return {"ok": False, "error": f"the page check reported: {m.group(1)[:1500]}"}

    def smoke(self, root: Path, frames: int = 0) -> SmokeResult:
        data = self.check(root)
        if "viewports" not in data:
            return SmokeResult(False, error=data.get("error", "page check failed"))
        errors = [e for v in data["viewports"] for e in v.get("errors", [])]
        sections = data["viewports"][0].get("sections", [])
        if errors:
            return SmokeResult(False, sections, 0, "Script errors on the page: " + " | ".join(errors[:5]))
        return SmokeResult(True, sections, 0)

    def screenshots(self, root: Path, out: Path) -> dict[str, str]:
        """Full-page screenshots at desktop and phone width (after scroll reveals had time to run)."""
        browser = _browser()
        if not browser:
            return {}
        out.mkdir(parents=True, exist_ok=True)
        server = self._serve(root)
        shots = {}
        try:
            # what a visitor sees first at each size, plus one tall overview (vh-sized sections grow with the
            # window, so a tall window can't stand in for the first screen)
            for name, size in (("desktop", "1280,800"), ("phone", "390,844"), ("overview", "1280,2400")):
                path = out / f"{name}.png"
                path.unlink(missing_ok=True)
                with tempfile.TemporaryDirectory() as profile:
                    _run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                          f"--user-data-dir={profile}", f"--window-size={size}", "--virtual-time-budget=5000",
                          f"--screenshot={path}", f"http://127.0.0.1:{server.server_address[1]}/index.html"],
                         root, SMOKE_TIMEOUT)
                if path.is_file():
                    shots[name] = str(path)
        finally:
            server.shutdown()
            server.server_close()
        return shots

    def play_command(self, root: Path) -> str:
        from jarvis.config import Settings

        local = f'cd "{root}"; python -m http.server 8000 --bind 127.0.0.1   then open http://127.0.0.1:8000'
        public = Settings.from_env().public_url
        return f"On your iPhone (Tailscale on): {public}/sites/{root.name}/\nOn the PC: {local}" if public else local
