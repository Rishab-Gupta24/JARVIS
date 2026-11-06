"""File in, structured data out: read → classify → structure → store → confirm."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from jarvis.core.dates import WEEKDAYS, monday_of, resolve_date, spoken_date
from jarvis.core.llm import JsonModel, LLMError, Transcriber
from jarvis.documents import menu, readers, store
from jarvis.documents.menu import MenuRow, WeekInfo
from jarvis.documents.readers import UnreadableFile

KINDS = ("mess_menu", "timetable", "exam_schedule", "other")
MAX_BYTES = 20 * 1024 * 1024


@dataclass
class IngestResult:
    document: store.Document
    reply: str
    meals: int = 0
    duplicate: bool = False


# ---------- what kind of file is it ----------

_EXAM = re.compile(r"\b(exams?|examination|date ?sheet|mid-?sem|end-?sem|mid-?terms?|finals)\b")
_CLASSES = re.compile(r"\b(lectures?|tutorials?|periods?|labs?|practicals?|class(es)?|room)\b")


def guess_kind(text: str) -> str | None:
    """Confident guesses only; None means ask the model."""
    t = text.lower()
    meals = {m for m in ("breakfast", "lunch", "dinner", "snacks") if m in t}
    days = {WEEKDAYS[w] for w in re.findall(r"[a-z]+", t) if w in WEEKDAYS}
    if len(meals) >= 2 and len(days) >= 3:
        return "mess_menu"
    if _EXAM.search(t) and len(re.findall(menu._PIECE, t, re.I)) >= 2:
        return "exam_schedule"
    if len(days) >= 3 and (len(re.findall(r"\b\d{1,2}[:.]\d{2}\b", t)) >= 3 or _CLASSES.search(t)):
        return "timetable"
    return None


def classify_with_model(text: str, llm: JsonModel) -> str:
    answer = llm.json(
        [{"role": "user", "content": (
            "What kind of document is this? Answer mess_menu (a hostel or canteen food menu), timetable "
            "(a class or lecture timetable), exam_schedule (exam dates), or other.\n\n" + text[:4000]
        )}],
        {"type": "object", "properties": {"kind": {"type": "string", "enum": list(KINDS)}}, "required": ["kind"]},
    )
    return answer.get("kind") if answer.get("kind") in KINDS else "other"


# ---------- which week ----------

def week_from_words(week: str | None, repeats: bool | None, today: date) -> WeekInfo:
    """The user's own answer: 'next week', 'this week', '9 nov', and/or 'it repeats'."""
    if week:
        start = monday_of(resolve_date(week, today))
        return WeekInfo(start, None if repeats else start + timedelta(days=6), bool(repeats))
    if repeats:
        return WeekInfo(monday_of(today), None, True)
    return WeekInfo()


def _settle_week(rows: list[MenuRow], text: str, today: date, week: str | None,
                 repeats: bool | None) -> tuple[WeekInfo, list[MenuRow]]:
    header_dates = [r.date for r in rows if r.date]
    if week or repeats:
        info = week_from_words(week, repeats, today)
    elif header_dates:
        info = WeekInfo(min(header_dates), max(header_dates), False)
    else:
        info = menu.detect_week(text, today)
        if info.repeats_weekly and info.valid_from is None:
            info = WeekInfo(monday_of(today), None, True)  # a rotating menu applies from this week on
    if info.repeats_weekly:
        rows = [replace(r, date=None) for r in rows]
    elif info.valid_from and info.valid_to:
        rows = menu.date_rows(rows, info.valid_from, info.valid_to)
    return info, rows


def _span(first: date, last: date) -> str:
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day} to {last.day} {last:%B}"
    return f"{first.day} {first:%B} to {last.day} {last:%B}"


def describe_menu(doc: store.Document, meals: int, today: date) -> str:
    if doc.status == "needs_week":
        return f"Got a mess menu with {meals} meals. Which week is it for, or does it repeat every week?"
    if doc.repeats_weekly:
        return f"Got the mess menu, {meals} meals. I'll use it every week from {spoken_date(doc.valid_from, today)}."
    return f"Got the mess menu for {_span(doc.valid_from, doc.valid_to)}, {meals} meals."


# ---------- the pipeline ----------

def _safe_name(filename: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._") or "file"
    return name[-80:]


def ingest(
    conn: sqlite3.Connection,
    data: bytes,
    filename: str,
    now: datetime,
    files_dir: str | Path,
    *,
    source: str = "upload",
    vision: Transcriber | None = None,
    llm: JsonModel | None = None,
    week: str | None = None,
    repeats_weekly: bool | None = None,
) -> IngestResult:
    """Store a file and whatever Jarvis can structure from it.

    Raises UnreadableFile (bad or unsupported file), DateParseError (a `week`
    phrase that isn't a date) or LLMError (a photo, but the vision model is down).
    """
    if len(data) > MAX_BYTES:
        raise UnreadableFile("That file is over 20 MB.")
    today = now.date()
    if week:
        resolve_date(week, today)  # a bad week phrase fails before anything is stored
    sha = hashlib.sha256(data).hexdigest()
    if existing := store.by_sha256(conn, sha):
        return IngestResult(existing, f"I already have that file ({existing.filename}).", duplicate=True)

    extracted = readers.read_bytes(data, filename, vision)
    rows = menu.parse_tables(extracted.tables, today)
    kind = "mess_menu" if rows else guess_kind(extracted.text)
    if kind is None:
        kind = classify_with_model(extracted.text, llm) if llm else "other"
    unreadable_menu = False
    if kind == "mess_menu" and not rows:
        rows = menu.extract_with_model(extracted.text, llm) if llm else []
        if not rows:
            kind, unreadable_menu = "other", True

    info, rows = _settle_week(rows, extracted.text, today, week, repeats_weekly) if kind == "mess_menu" \
        else (WeekInfo(), [])

    folder = Path(files_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{sha[:12]}_{_safe_name(filename)}"
    stored.write_bytes(data)

    doc = store.insert(conn, kind=kind, filename=filename, stored_path=str(stored), sha256=sha,
                       source=source, now=now, week=info, raw_text=extracted.text, rows=rows)
    if kind == "mess_menu":
        reply = describe_menu(doc, len(rows), today)
    elif unreadable_menu:
        reply = (f"{filename} looks like a mess menu, but I couldn't read its days and meals, "
                 "so I've saved it as a document you can ask me about.")
    elif kind == "timetable":
        reply = f"Saved {filename} as your class timetable. You can ask me what's in it."
    elif kind == "exam_schedule":
        reply = f"Saved {filename} as your exam schedule. You can ask me about it."
    else:
        reply = f"Saved {filename}. You can ask me about it."
    return IngestResult(doc, reply, meals=len(rows))


# ---------- the inbox folder ----------

_PARTIAL = (".tmp", ".part", ".crdownload", ".download")


def scan_inbox(
    conn: sqlite3.Connection, inbox: str | Path, files_dir: str | Path, now: datetime, *,
    vision: Transcriber | None = None, llm: JsonModel | None = None, settle_seconds: float = 2.0,
    log: Callable[[str], None] = print,
) -> list[IngestResult]:
    """Ingest every finished file in the inbox. Done files are removed (the original is
    kept under files_dir); unreadable ones move to inbox/failed/; if the model is down
    the file stays put and is retried on the next scan."""
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(p for p in inbox.iterdir() if p.is_file()):
        if path.name.startswith(".") or path.suffix.lower() in _PARTIAL:
            continue
        if time.time() - path.stat().st_mtime < settle_seconds:
            continue  # probably still being copied in
        try:
            result = ingest(conn, path.read_bytes(), path.name, now, files_dir,
                            source="inbox", vision=vision, llm=llm)
        except LLMError as e:
            log(f"inbox: {path.name} waits, model unavailable: {e}")
            continue
        except (UnreadableFile, ValueError) as e:
            failed = inbox / "failed"
            failed.mkdir(exist_ok=True)
            path.replace(failed / path.name)
            log(f"inbox: couldn't read {path.name}: {e}")
            continue
        path.unlink()
        log(f"inbox: {result.reply}")
        results.append(result)
    return results


class InboxWatcher:
    """Polls the inbox folder in a background thread while the API runs."""

    def __init__(self, open_conn: Callable[[], sqlite3.Connection], inbox: str, files_dir: str,
                 clock: Callable[[], datetime], vision: Transcriber | None, llm: JsonModel | None,
                 interval: float = 5.0):
        self._args = (open_conn, inbox, files_dir, clock, vision, llm)
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="inbox", daemon=True)
        self._said: set[str] = set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _log(self, line: str) -> None:
        if line not in self._said:  # a file waiting on the model would otherwise repeat every 5 s
            self._said.add(line)
            print(line, flush=True)

    def _run(self) -> None:
        open_conn, inbox, files_dir, clock, vision, llm = self._args
        while not self._stop.wait(self._interval):
            try:
                conn = open_conn()
                try:
                    scan_inbox(conn, inbox, files_dir, clock(), vision=vision, llm=llm, log=self._log)
                finally:
                    conn.close()
            except Exception as e:  # keep watching whatever happens to one scan
                self._log(f"inbox: scan failed: {e!r}")
