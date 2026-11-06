"""Whatever the iPhone's Share Sheet sends, decide what it is and do the right thing.

- Text that reads like a menu, timetable or exam schedule -> stored as a document.
- Any other text (a forwarded WhatsApp message, an email) -> events added to the calendar.
- A file -> ingested as a document. If it's none of the known kinds (a screenshot of an
  announcement, a poster), the events it mentions are added too.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jarvis.core import agent
from jarvis.documents.ingest import IngestResult, guess_kind, ingest
from jarvis.documents.readers import IMAGE_TYPES, markdown_tables

DOCUMENT_KINDS = ("mess_menu", "timetable", "exam_schedule")


@dataclass
class ShareResult:
    reply: str
    stored: dict | None = None             # the document, if one was stored
    actions: list[dict] = field(default_factory=list)

    @property
    def events_added(self) -> int:
        return sum(1 for a in self.actions if a["tool"] == "create_event" and a["ok"])


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_TYPES


def share_text(conn: sqlite3.Connection, llm, text: str, now: datetime, files_dir: str | Path, *,
               session: str | None = None, memory: agent.SessionMemory | None = None) -> ShareResult:
    looks_like_document = guess_kind(text) in DOCUMENT_KINDS or bool(markdown_tables(text))
    if looks_like_document:
        r = ingest(conn, text.encode(), "shared-text.txt", now, files_dir, source="phone",
                   llm=llm if hasattr(llm, "json") else None)
        if r.document.kind in DOCUMENT_KINDS:
            _remember(memory, session, now, "(shared some text)", r.reply)
            return ShareResult(r.reply, r.document.to_dict())
    reply = agent.handle(text, conn, llm, now=now, source="phone", session=session, memory=memory, shared=True)
    return ShareResult(reply.text, None, reply.actions)


def share_file(conn: sqlite3.Connection, llm, data: bytes, filename: str, now: datetime, files_dir: str | Path, *,
               vision=None, session: str | None = None, memory: agent.SessionMemory | None = None) -> ShareResult:
    """Raises UnreadableFile / LLMError like ingest()."""
    r: IngestResult = ingest(conn, data, filename, now, files_dir, source="phone", vision=vision,
                             llm=llm if hasattr(llm, "json") else None)
    if r.duplicate or r.document.kind != "other":
        _remember(memory, session, now, f"(shared the file {filename})", r.reply)
        return ShareResult(r.reply, r.document.to_dict())
    # Not a menu/timetable/exam file: a screenshot or notice may still announce events.
    text = conn.execute("SELECT raw_text FROM documents WHERE id = ?", (r.document.id,)).fetchone()[0]
    events = agent.handle(text[:4000], conn, llm, now=now, source="phone", session=session, memory=memory,
                          shared=True)
    result = ShareResult(r.reply, r.document.to_dict(), events.actions)
    if result.events_added:
        result.reply = events.text  # "Added X, ..." says more than "Saved file.jpg"
    return result


def _remember(memory, session, now, user, reply) -> None:
    if memory is not None and session:
        memory.add(session, now, user, reply)
