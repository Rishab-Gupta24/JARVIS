"""Turn trusted GitHub repos into instruction/answer pairs, one dataset per skill.

    python -m jarvis.training.datasets          # writes data/training/sets/<skill>.jsonl (+ .eval.jsonl)

Each example is {"instruction", "response", "source"}. Only real code and docs from the
repos go in; nothing is generated, so the model learns from the repos themselves. Long
files are skipped rather than cut, so every answer is complete. HumanEval is the benchmark
and is never used for training.
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
import re
import textwrap
from pathlib import Path

from jarvis.config import ROOT

REPOS = ROOT / "data" / "training" / "repos"
SETS = ROOT / "data" / "training" / "sets"
MAX_CHARS = 3500  # ~1000 tokens: fits the training sequence length with the prompt
EVAL_SHARE = 0.05


def _ex(instruction: str, response: str, source: Path) -> dict | None:
    instruction, response = instruction.strip(), response.strip()
    if not instruction or not response or len(instruction) + len(response) > MAX_CHARS:
        return None
    return {"instruction": instruction, "response": response, "source": str(source.relative_to(REPOS))}


def _first_paragraph(doc: str) -> str:
    doc = textwrap.dedent(doc).strip()
    para = re.split(r"\n\s*\n|\n\s*>>>|\n\s*(?:Args|Parameters|Returns|Examples?|Raises):", doc)[0]
    return " ".join(para.split())


def _code(lang: str, text: str) -> str:
    return f"```{lang}\n{text.strip()}\n```"


# ---------- Python and games: functions and whole files with docstrings ----------

def python_functions(folder: Path, skill_hint: str = "") -> list[dict]:
    out = []
    for path in sorted(folder.rglob("*.py")):
        if any(p in path.parts for p in ("test", "tests", ".git")) or path.name.startswith("test_"):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        lines = src.splitlines()
        imports = _module_imports(tree, lines)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
                continue
            doc = ast.get_docstring(node)
            if not doc or len(_first_paragraph(doc)) < 25:
                continue
            body = "\n".join(lines[node.lineno - 1 - len(node.decorator_list):node.end_lineno])
            if body.count("\n") > 60:
                continue
            # The file's imports that this function uses go into the answer. Left out, a third of the
            # examples taught "call floor() / combinations() without importing them" (7 Nov 2026).
            used = _names_used(node)
            needed = [text for names, text in imports if names is None or names & used]
            if needed:
                body = "\n".join(needed) + "\n\n\n" + textwrap.dedent(body)
            args = ", ".join(a.arg for a in node.args.args)
            if _completion_turn(path, node.name):
                # The stub (signature + docstring) is given; the answer is the whole function.
                # Without these, training only on "write it from a one-line description" taught the
                # model to reproduce docstrings: given a stub it echoed it and stopped (7 Nov 2026).
                stub = "\n".join(lines[node.lineno - 1 - len(node.decorator_list):node.body[0].end_lineno])
                ask = f"Finish the implementation of this Python function:\n\n{_code('python', textwrap.dedent(stub))}"
            else:
                ask = f"Write a Python function `{node.name}({args})`{skill_hint}: {_first_paragraph(doc)}"
            if ex := _ex(ask, _code("python", textwrap.dedent(body)), path):
                out.append(ex)
    return out


def _module_imports(tree: ast.Module, lines: list[str]) -> list[tuple[set[str] | None, str]]:
    """Top-level import statements: (names they bind, or None for 'import *'; their source text)."""
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            continue
        text = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if any(a.name == "*" for a in node.names):
            out.append((None, text))
        else:
            out.append(({(a.asname or a.name).split(".")[0] for a in node.names}, text))
    return out


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _completion_turn(path: Path, name: str) -> bool:
    """Half the functions (fixed by a hash, so rebuilding gives the same split) become stub completions."""
    return hashlib.sha1(f"{path.name}:{name}".encode()).digest()[0] % 2 == 0


def python_files(folder: Path, framing: str, pattern: str = "*.py") -> list[dict]:
    """A whole program from its module docstring: 'Snake, classic arcade game.' -> snake.py."""
    out = []
    for path in sorted(folder.rglob(pattern)):
        if ".git" in path.parts or path.name.startswith(("test_", "__")):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            doc = ast.get_docstring(ast.parse(src))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        if not doc or len(_first_paragraph(doc)) < 12:
            continue
        if ex := _ex(f"{framing}: {_first_paragraph(doc)}", _code("python", src), path):
            out.append(ex)
    return out


# ---------- web: self-contained example pages ----------

def web_pages(folder: Path) -> list[dict]:
    out = []
    for path in sorted(folder.rglob("*.html")):
        if ".git" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        title = re.search(r"<title>(.*?)</title>", src, re.S | re.I)
        if not title or len(title.group(1).strip()) < 4:
            continue
        where = " / ".join(p.replace("-", " ") for p in path.relative_to(folder).parent.parts[-2:])
        ask = f"Write a complete HTML page (with any CSS and JavaScript inline or linked) for: {' '.join(title.group(1).split())}"
        if where:
            ask += f" (topic: {where})"
        if ex := _ex(ask, _code("html", src), path):
            out.append(ex)
    for path in sorted(folder.rglob("*.js")):
        if ".git" in path.parts or "node_modules" in path.parts or ".min." in path.name:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        head = re.match(r"\s*(?://[^\n]*\n\s*)+|\s*/\*(.*?)\*/", src, re.S)
        if not head:
            continue
        comment = " ".join(re.sub(r"^\s*(//|/\*|\*/|\*)", "", line).strip() for line in head.group(0).splitlines())
        if len(comment.strip()) < 25 or "license" in comment.lower():
            continue
        if ex := _ex(f"Write JavaScript that does this: {comment.strip()}", _code("javascript", src), path):
            out.append(ex)
    return out


# ---------- reverse engineering: docs sections with their code ----------

def markdown_sections(folder: Path, tool: str) -> list[dict]:
    """'## Searching for strings' + its text and code -> 'How do I search for strings in radare2?'"""
    out = []
    for path in sorted(folder.rglob("*.md")):
        if ".git" in path.parts or path.name.lower() in ("license.md", "contributing.md", "changelog.md"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        parts = re.split(r"^(#{1,4} .+)$", text, flags=re.M)
        for heading, body in zip(parts[1::2], parts[2::2]):
            title = heading.lstrip("#").strip().strip("`")
            body = body.strip()
            if len(title) < 4 or len(body) < 120 or len(re.findall(r"\w+", body)) < 25:
                continue
            ask = f"In {tool}: {title}. Explain how, with an example."
            if ex := _ex(ask, body, path):
                out.append(ex)
    return out


def python_examples_with_readme(folder: Path, framing: str) -> list[dict]:
    """angr's CTF solutions: the README next to a solve script describes the challenge."""
    out = []
    for script in sorted(folder.rglob("solve*.py")):
        readme = next((p for p in script.parent.glob("README*")), None)
        if readme is None:
            continue
        desc = " ".join(readme.read_text(encoding="utf-8", errors="ignore").split())[:600]
        src = script.read_text(encoding="utf-8", errors="ignore")
        if ex := _ex(f"{framing}: {desc}", _code("python", src), script):
            out.append(ex)
    return out


# ---------- putting the sets together ----------

def build() -> dict[str, list[dict]]:
    r = REPOS
    sets = {
        "python": python_functions(r / "TheAlgorithms_Python")
        + python_files(r / "TheAlgorithms_Python", "Write a Python program"),
        "games": python_files(r / "grantjenks_free-python-games" / "src" / "freegames",
                              "Write a Python game using turtle graphics and the freegames helpers")
        + python_files(r / "pygame_pygame" / "examples", "Write a pygame example program")
        + python_functions(r / "grantjenks_free-python-games" / "src" / "freegames", " for a turtle game"),
        "web": web_pages(r / "mdn_learning-area") + web_pages(r / "mdn_dom-examples"),
        "reverse": markdown_sections(r / "radareorg_radare2-book", "radare2")
        + markdown_sections(r / "angr_angr-doc", "angr (Python binary analysis)")
        + markdown_sections(r / "HackOvert_GhidraSnippets", "Ghidra's Python scripting API")
        + python_examples_with_readme(r / "angr_angr-doc" / "examples",
                                      "Write an angr script that solves this reverse-engineering challenge")
        + python_files(r / "capstone-engine_capstone" / "bindings" / "python",
                       "Write a Python script using the Capstone disassembler"),
    }
    for name, items in sets.items():  # drop exact duplicates (same instruction and answer)
        seen, unique = set(), []
        for ex in items:
            key = hashlib.sha1((ex["instruction"] + ex["response"]).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                unique.append(ex)
        sets[name] = unique
    return sets


def write(sets: dict[str, list[dict]], out: Path = SETS, seed: int = 7) -> dict[str, tuple[int, int]]:
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, items in sets.items():
        items = list(items)
        random.Random(seed).shuffle(items)
        n_eval = max(1, int(len(items) * EVAL_SHARE)) if items else 0
        for suffix, chunk in ((".eval.jsonl", items[:n_eval]), (".jsonl", items[n_eval:])):
            with open(out / f"{name}{suffix}", "w", encoding="utf-8") as f:
                for ex in chunk:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        counts[name] = (len(items) - n_eval, n_eval)
    return counts


if __name__ == "__main__":
    for name, (train, held) in write(build()).items():
        print(f"{name:8s} {train:5d} training examples, {held:4d} held out")
