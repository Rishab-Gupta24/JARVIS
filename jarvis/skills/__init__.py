"""Skills: short know-how files (jarvis/skills/*.md) that the coding agents load when relevant.

Each file starts with a header:

    ---
    name: fighting-game
    roles: planner, tester, coder, reviewer
    keywords: fight, fighter, combat, punch, kick
    always: false
    ---

`select(text, role)` returns the skills for that role whose keywords appear in the text (or that are
marked `always`), best match first, within a size limit, ready to append to a system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).parent


@dataclass(frozen=True)
class Skill:
    name: str
    roles: frozenset[str]
    keywords: tuple[str, ...]
    always: bool
    body: str


def parse(text: str) -> Skill:
    m = re.match(r"---\n(.*?)\n---\n(.*)", text.replace("\r\n", "\n"), re.S)
    if not m:
        raise ValueError("a skill file must start with a --- header ---")
    head = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    head = {k.strip(): v.strip() for k, v in head.items()}
    split = lambda key: [w.strip().lower() for w in head.get(key, "").split(",") if w.strip()]  # noqa: E731
    return Skill(head["name"], frozenset(split("roles")), tuple(split("keywords")),
                 head.get("always", "false").lower() == "true", m.group(2).strip())


@lru_cache(maxsize=1)
def load_all(folder: Path = HERE) -> tuple[Skill, ...]:
    return tuple(parse(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.md")))


def score(skill: Skill, text: str) -> int:
    low = text.lower()
    # prefix match ("fight" counts "fighting"), but short keywords whole-word only ("ai" isn't "air")
    return sum(len(re.findall(rf"\b{re.escape(k)}\b" if len(k) <= 3 else rf"\b{re.escape(k)}", low))
               for k in skill.keywords)


def pick(text: str, role: str, skills: tuple[Skill, ...] | None = None) -> list[Skill]:
    skills = load_all() if skills is None else skills
    scored = [(score(s, text), s) for s in skills if role in s.roles or "all" in s.roles]
    chosen = [(n, s) for n, s in scored if n > 0 or s.always]
    return [s for n, s in sorted(chosen, key=lambda x: (-x[1].always, -x[0], x[1].name))]


def select(text: str, role: str, limit: int = 4000, skills: tuple[Skill, ...] | None = None) -> str:
    # 4000 characters (~1000 tokens): with rules, memory and conventions the coder's prompt must stay well
    # inside the context window (see core/llm.default_num_ctx)
    out, used = [], 0
    for s in pick(text, role, skills):
        block = f"\n\n## Skill: {s.name}\n{s.body}"
        if used + len(block) > limit:
            continue
        out.append(block)
        used += len(block)
    return ("\n\nUse these skills where they apply:" + "".join(out)) if out else ""
