"""Fix misheard or misspelt names before the model sees them.

"open could code" -> "open Claude Code", "close spot if I" -> "close Spotify",
"open my jarvs project" -> "open my Jarvis project".

- Known names: apps in the Start Menu, the user's projects, and a few fixed ones
  (Claude Code, VS Code, YouTube...). Only the words right after a command word
  (open, close, launch, start, use, in...) are compared, so ordinary sentences
  are never rewritten.
- Sure (letters and sound both close) -> fixed silently. Fairly close -> Jarvis
  asks "Did you mean Spotify?"; a yes runs the fixed request, a no runs it as
  heard (user asked 8 Nov 2026: "first run it through me").
- Both answers are remembered (meta table): a confirmed fix is silent next time,
  a rejected one is never asked again.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SURE = 0.90
ASK = 0.78
LEARNED_KEY = "autocorrect.learned"

# Names Whisper keeps getting wrong in ways no similarity score catches.
FIXED = [
    (re.compile(r"\b(?:cloud|clod|clawed|claud|clyde|cloudy|could|clot|claw|clued)\s*(?:code|coat|cold|cod|coad)\b",
                re.I), "Claude Code"),
    (re.compile(r"\b(?:cloud|claud|could)\s+(?:cli|c\.l\.i\.?)", re.I), "Claude CLI"),
    (re.compile(r"\bshop\s?fi\b|\bshopfy\b", re.I), "Shopify"),
    (re.compile(r"\bv\.?\s?s\.?\s+(?:code|cold|coat|cod)\b|\bvs\s?code\b|\bvisual studio cold\b", re.I), "VS Code"),
    (re.compile(r"\bspot\s?if(?:y|i|\s+i)\b", re.I), "Spotify"),
    (re.compile(r"\byou\s+tube\b", re.I), "YouTube"),
    (re.compile(r"\bwhat'?s\s?app\b", re.I), "WhatsApp"),
    (re.compile(r"\bjar\s?vis\b|\bjarvice\b", re.I), "Jarvis"),
]
CORE_NAMES = ["Jarvis", "Claude Code", "VS Code", "Visual Studio Code", "Spotify", "YouTube", "WhatsApp", "Chrome",
              "Google Chrome", "Shopify", "Discord", "Microsoft Edge", "Notepad", "Calculator", "File Explorer",
              "Task Manager", "Windows Terminal", "PowerShell", "Instagram", "Gmail", "Netflix", "GitHub"]

# The words after these are where a name goes.
_VERB = re.compile(r"\b(open|close|quit|exit|launch|start|run|kill|show|switch to|go to|use|using|with|in|on|"
                   r"play|bring up|pull up|fire up|and)\s+", re.I)
_WORDS = re.compile(r"(?:the |my |a |an )?([\w'.+-]+(?:\s+[\w'.+-]+){0,2})", re.I)
_STOP = set("""for and please now then to on in at with from about my the a an it this that me up so but if
    project folder app application window file files website site page game code coding something everything
    music song songs video videos timer alarm alarms reminder screen volume tab tabs again""".split())
# Never "corrected": ordinary things people say after "open"/"start".
_PLAIN = {"coding", "music", "a game", "a website", "the music", "it", "this", "that", "something", "settings",
          "downloads", "documents", "desktop", "pictures", "browser", "terminal", "camera", "mail", "notes",
          "photos", "maps", "clock", "calendar", "store", "word", "excel"}


_SYSTEM = re.compile(r"\b(tools?|services|management|monitor|diagnostics?|viewer|configuration|information|"
                     r"manuals?|odbc|recorder|scheduler|preferences|utility|downloader|editor|keyboard|panel|"
                     r"firewall|cleanup|connection|command prompt|settings)\b", re.I)


@dataclass(frozen=True)
class Fix:
    heard: str      # as said, e.g. "spot if I"
    name: str       # e.g. "Spotify"
    score: float
    text: str       # the whole request with the fix made

    @property
    def question(self) -> str:
        return f"Did you mean {self.name}?"


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def sound(s: str) -> str:
    """A rough sound key: 'Claude' and 'could' both -> 'kld', 'Spotify' and 'spot if I' -> 'sptf'."""
    s = _letters(s)
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("sh", "s"), ("ch", "k"), ("th", "t"), ("wh", "w"),
                 ("gh", "g"), ("x", "ks")):
        s = s.replace(a, b)
    s = s.translate(str.maketrans("cqzdbvjy", "ksstpfgi"))
    if not s:
        return ""
    head, rest = s[0], re.sub(r"[aeiouwh]", "", s[1:])
    return re.sub(r"(.)\1+", r"\1", head + rest)


def similarity(heard: str, name: str) -> float:
    a, b = _letters(heard), _letters(name)
    if not a or not b:
        return 0.0
    letters = difflib.SequenceMatcher(None, a, b).ratio()
    sa, sb = sound(heard), sound(name)
    sounds = difflib.SequenceMatcher(None, sa, sb).ratio() if sa and sb else 0.0
    return (letters + sounds) / 2


def apply_fixed(text: str) -> str:
    for pattern, name in FIXED:
        text = pattern.sub(name, text)
    return text


def _clean_name(name: str) -> str:
    name = re.sub(r"\s*\([^)]*\)", "", name)              # "IDLE (Python 3.12)"
    name = re.sub(r"\s+\d+(\.\d+)*$", "", name).strip()   # "Blender 5.2", "GIMP 3.2.4"
    return name


def correct(text: str, names: list[str], learned: dict[str, str | None] | None = None) -> tuple[str, Fix | None]:
    """(text with sure fixes made, a fix to ask about or None)."""
    learned = learned or {}
    text = apply_fixed(text)
    known = {n.lower(): n for n in names}
    candidates = [n for n in names if len(_letters(n)) >= 4]
    sure: list[tuple[int, int, str]] = []   # (start, end, name) in `text`
    unsure: list[tuple[int, int, str, str, float]] = []
    seen_verb = False
    for verb in _VERB.finditer(text):
        if verb.group(1).lower() == "and" and not seen_verb:
            continue  # "and" only continues a list: "open Chrome and spotfy"
        seen_verb = True
        m = _WORDS.match(text, verb.end())
        if m is None:
            continue
        words = m.group(1).split()
        # stop at the first filler word: "open spotfy for me" -> "spotfy"
        for i, w in enumerate(words):
            if w.lower().strip(".,!?") in _STOP and i > 0:
                words = words[:i]
                break
        best: tuple[float, str, str] | None = None
        for k in range(len(words), 0, -1):
            heard = " ".join(words[:k]).strip(".,!?")
            low = heard.lower()
            if low in known or low in _PLAIN or low in _STOP:
                best = None
                break
            if low in learned:
                best = (1.0, heard, learned[low]) if learned[low] else None
                break
            for name in candidates:
                if abs(len(name.split()) - k) > 1:
                    continue
                score = similarity(heard, name)
                if best is None or score > best[0]:
                    best = (score, heard, name)
        if best is None or best[0] < ASK:
            continue
        score, heard, name = best
        start = text.find(heard, m.start(1))
        if start < 0:
            continue
        if score >= SURE:
            sure.append((start, start + len(heard), name))
        else:
            unsure.append((start, start + len(heard), name, heard, score))

    def apply(spans: list[tuple[int, int, str]]) -> str:
        out = text
        for start, end, name in sorted(spans, reverse=True):  # from the end, so earlier positions stay right
            out = out[:start] + name + out[end:]
        return out

    fixed = apply(sure)
    if not unsure:
        return fixed, None
    start, end, name, heard, score = unsure[0]
    return fixed, Fix(heard, name, score, apply(sure + [(start, end, name)]))


# ---------- known names ----------

_cache: dict[str, object] = {"at": 0.0, "names": []}
_lock = threading.Lock()


def vocabulary(max_age: float = 600) -> list[str]:
    """Start Menu apps + projects + core names, cached for 10 minutes (the Start Menu scan takes a moment)."""
    with _lock:
        if time.time() - float(_cache["at"]) < max_age and _cache["names"]:
            return list(_cache["names"])
    from jarvis.config import Settings
    from jarvis.pc import actions

    names = list(CORE_NAMES)
    try:
        names += [p.name for p in actions.discover_projects(Settings.from_env().projects)]
        names += [_clean_name(n) for n in actions.start_menu_apps()]
    except Exception:  # a broken shortcut must never stop a request
        pass
    unique = list(dict.fromkeys(n for n in names if n))
    with _lock:
        _cache.update(at=time.time(), names=unique)
    return unique


def hotwords(names: list[str] | None = None, limit: int = 400) -> str:
    """Names for Whisper to expect, most useful first, kept short (it's part of Whisper's prompt)."""
    names = names if names is not None else vocabulary()
    out, size = [], 0
    first = ["Jarvis", "Claude Code", "VS Code", "Spotify", "YouTube", "WhatsApp"]
    # then the user's projects and apps (what Whisper wouldn't guess), skipping everyday words and system tools
    rest = [n for n in names if n not in CORE_NAMES and n.lower() not in _PLAIN and not _SYSTEM.search(n)]
    for n in first + rest:
        if size + len(n) + 2 > limit:
            break
        out.append(n)
        size += len(n) + 2
    return ", ".join(out)


# ---------- remembering answers ----------

def learned(conn: sqlite3.Connection) -> dict[str, str | None]:
    from jarvis.calendar.db import get_meta

    try:
        return json.loads(get_meta(conn, LEARNED_KEY) or "{}")
    except (json.JSONDecodeError, sqlite3.Error):
        return {}


def remember(conn: sqlite3.Connection, heard: str, name: str | None) -> None:
    """name=None: the user said no, so never suggest it for these words again."""
    from jarvis.calendar.db import set_meta

    data = learned(conn)
    data[heard.lower()] = name
    set_meta(conn, LEARNED_KEY, json.dumps(data))


@dataclass
class _Waiting:
    fix: Fix
    original: str
    at: datetime


@dataclass
class Pending:
    """One question per session: 'Did you mean Spotify?' waits 2 minutes for the answer."""
    ttl: timedelta = timedelta(minutes=2)
    _by_session: dict[str, _Waiting] = field(default_factory=dict)

    def ask(self, session: str, fix: Fix, original: str, now: datetime) -> None:
        self._by_session[session] = _Waiting(fix, original, now)

    def take(self, session: str, now: datetime) -> _Waiting | None:
        waiting = self._by_session.pop(session, None)
        return waiting if waiting and now - waiting.at <= self.ttl else None


PENDING = Pending()
