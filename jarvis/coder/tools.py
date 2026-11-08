"""What the coding agent may do. Files: only inside its project folder. Web: DuckDuckGo
search and reading pages (the user allowed search words and page requests to leave the PC,
7 Nov 2026; never file contents). References: the cloned repos in data/training/repos."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from jarvis.config import ROOT

REFERENCES = ROOT / "data" / "training" / "repos"
TEXT_EXT = {".py", ".js", ".ts", ".html", ".css", ".md", ".txt", ".json", ".c", ".h", ".cpp", ".java", ".rs",
            ".go", ".sh", ".toml", ".yml", ".yaml", ".rst"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JarvisCoder/1.0"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ToolProblem(Exception):
    """Told to the model so it can correct itself."""


@dataclass
class Workspace:
    root: Path
    confirm: Callable[[str], bool]  # asks the user in the terminal; tests answer for them
    written: list[str] = field(default_factory=list)
    # The studio's write rules: returns why a path may not be written now (e.g. the coder can't
    # edit the tests it has to pass), or None.
    refuse_write: Callable[[str], str | None] | None = None

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise ToolProblem(f"{rel} is outside the project folder; only files inside it can be used.")
        return p

    # ---------- files ----------

    def list_files(self, path: str = ".") -> str:
        base = self.path(path)
        if not base.is_dir():
            raise ToolProblem(f"{path} is not a folder.")
        items = []
        for p in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in p.relative_to(self.root.resolve()).parts):
                continue
            if p.is_file():
                items.append(f"{p.relative_to(self.root.resolve()).as_posix()} ({p.stat().st_size} bytes)")
            if len(items) >= 200:
                items.append("... (more)")
                break
        return "\n".join(items) or "(empty folder)"

    def read_file(self, path: str) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolProblem(f"{path} doesn't exist.")
        text = p.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= 12000 else text[:12000] + "\n... (cut at 12000 characters)"

    def write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        rel = p.relative_to(self.root.resolve()).as_posix()
        if self.refuse_write and (why := self.refuse_write(rel)):
            raise ToolProblem(why)
        if p.exists() and not self.confirm(f"Overwrite {path}?"):
            return f"The user said no: {path} was not changed."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.written.append(path)
        return f"Wrote {path} ({len(content)} characters)."

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        """Change one exact piece of an existing file. Small models fix things far more reliably this way than by
        rewriting whole files (seen 8 Nov 2026: given the exact cause three times, rewrites kept breaking or
        undoing other code)."""
        p = self.path(path)
        rel = p.relative_to(self.root.resolve()).as_posix()
        if self.refuse_write and (why := self.refuse_write(rel)):
            raise ToolProblem(why)
        if not p.is_file():
            raise ToolProblem(f"{path} doesn't exist; use write_file to create it.")
        text = p.read_text(encoding="utf-8")
        count = text.count(old) if old else 0
        if count == 0:
            near = [ln.strip() for ln in text.splitlines() if old.strip().splitlines() and
                    old.strip().splitlines()[0].strip()[:30] in ln][:3]
            hint = f" Lines that look similar: {near}" if near else " Read the file again and copy the text exactly."
            raise ToolProblem(f"The text to replace isn't in {path}.{hint}")
        if count > 1:
            raise ToolProblem(f"That text appears {count} times in {path}; include more surrounding lines so it's unique.")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        self.written.append(path)
        return f"Replaced {len(old.splitlines()) or 1} line(s) in {path}."

    def run_python(self, path: str, args: str = "") -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolProblem(f"{path} doesn't exist.")
        if not self.confirm(f"Run python {path} {args}".strip() + "?"):
            return "The user said no; it was not run."
        try:
            done = subprocess.run([sys.executable, str(p), *args.split()], cwd=self.root, capture_output=True,
                                  text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "Still running after 60 seconds, so it was stopped (games and servers keep running: that's fine)."
        out = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip()
        return f"Exit code {done.returncode}.\n{out[-4000:]}"

    # ---------- references and the web ----------

    def search_references(self, query: str, limit: int = 8) -> str:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        if not words or not REFERENCES.is_dir():
            return "No reference library."
        hits = []
        for p in REFERENCES.rglob("*"):
            if p.suffix not in TEXT_EXT or any(part in SKIP_DIRS for part in p.parts) or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            low = text.lower()
            score = sum(low.count(w) for w in words) + 5 * sum(w in p.name.lower() for w in words)
            if score and all(w in low or w in p.name.lower() for w in words[:3]):
                hits.append((score, p, text))
        hits.sort(key=lambda h: -h[0])
        out = []
        for _, p, text in hits[:limit]:
            i = max(0, text.lower().find(words[0]) - 300)
            out.append(f"--- {p.relative_to(REFERENCES).as_posix()}\n{text[i:i + 1200]}")
        return "\n\n".join(out) or "Nothing in the reference repos matched."

    def search_web(self, query: str) -> str:
        r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": UA},
                       timeout=20, follow_redirects=True)
        r.raise_for_status()
        results = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                             r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
            url = m.group(1)
            if "uddg=" in url:  # DuckDuckGo's redirect link -> the real address
                url = unquote(parse_qs(urlparse(url).query).get("uddg", [url])[0])
            title, snippet = (html.unescape(re.sub("<.*?>", "", g)).strip() for g in (m.group(2), m.group(3)))
            results.append(f"{title}\n{url}\n{snippet}")
            if len(results) >= 6:
                break
        return "\n\n".join(results) or "No results."

    def read_webpage(self, url: str) -> str:
        if not re.match(r"^https?://", url):
            raise ToolProblem("Only http(s) addresses can be read.")
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        text = page_text(r.text) if "html" in r.headers.get("content-type", "html") else r.text
        return text[:8000] + ("\n... (cut)" if len(text) > 8000 else "")


class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "svg", "noscript"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "li", "pre", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def page_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


SPECS = [
    {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
        "required": [k for k in props if k not in optional]}}}
    for name, desc, props, optional in [
        ("list_files", "List the files in the project (or a sub-folder).", {"path": "Folder, default '.'"}, {"path"}),
        ("read_file", "Read a file in the project.", {"path": "File path inside the project."}, set()),
        ("write_file", "Create a NEW file (or fully replace a tiny one) with its COMPLETE content.",
         {"path": "File path inside the project.", "content": "The whole file."}, set()),
        ("replace_in_file", "Change part of an EXISTING file: replace one exact, unique piece of text with new text. "
         "Prefer this for every change to an existing file.",
         {"path": "File path inside the project.", "old": "The exact current text (copied from read_file), unique in "
          "the file; include a few surrounding lines.", "new": "The text to put in its place."}, set()),
        ("run_python", "Run a Python file in the project to test it (the user is asked first).",
         {"path": "The .py file.", "args": "Command-line arguments, optional."}, {"args"}),
        ("search_references", "Search the trusted reference repos (Python algorithms, MDN web examples, pygame "
         "and turtle games, radare2/angr/Ghidra/Capstone reverse-engineering docs) for examples.",
         {"query": "A few keywords."}, set()),
        ("search_web", "Search the web (DuckDuckGo) for documentation or an error message.",
         {"query": "Search words. Never include the user's private data or file contents."}, set()),
        ("read_webpage", "Read a web page (documentation, a search result) as text.", {"url": "http(s) address."},
         set()),
    ]
]
