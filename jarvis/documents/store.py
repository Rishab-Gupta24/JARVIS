"""Stored documents and menu rows: the only code that reads or writes those tables."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from jarvis.calendar.repo import from_db, to_db
from jarvis.documents.menu import MEALS, MenuRow, WeekInfo, date_rows

_SEARCH_STOP = {"the", "a", "an", "is", "are", "what", "when", "where", "which", "my", "in", "on", "of",
                "for", "to", "do", "does", "i", "about", "file", "document", "and", "or", "it", "there"}


@dataclass
class Document:
    id: int
    kind: str
    filename: str
    stored_path: str | None
    source: str
    received_at: datetime
    valid_from: date | None
    valid_to: date | None
    repeats_weekly: bool | None
    status: str

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "filename": self.filename, "source": self.source,
            "received_at": self.received_at.isoformat(),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "repeats_weekly": self.repeats_weekly, "status": self.status,
        }


def _date_or_none(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _doc(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"], kind=row["kind"], filename=row["filename"], stored_path=row["stored_path"],
        source=row["source"], received_at=from_db(row["received_at"]),
        valid_from=_date_or_none(row["valid_from"]), valid_to=_date_or_none(row["valid_to"]),
        repeats_weekly=None if row["repeats_weekly"] is None else bool(row["repeats_weekly"]),
        status=row["status"],
    )


def insert(
    conn: sqlite3.Connection, *, kind: str, filename: str, stored_path: str | None, sha256: str | None,
    source: str, now: datetime, week: WeekInfo, raw_text: str, rows: list[MenuRow],
) -> Document:
    status = "needs_week" if kind == "mess_menu" and not week.known else "ready"
    cur = conn.execute(
        """INSERT INTO documents (kind, filename, stored_path, sha256, source, received_at,
                                  valid_from, valid_to, repeats_weekly, status, raw_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, filename, stored_path, sha256, source, to_db(now),
         week.valid_from.isoformat() if week.valid_from else None,
         week.valid_to.isoformat() if week.valid_to else None,
         None if week.repeats_weekly is None else int(week.repeats_weekly), status, raw_text),
    )
    doc_id = cur.lastrowid
    _insert_rows(conn, doc_id, rows)
    conn.execute("INSERT INTO documents_fts (rowid, filename, body) VALUES (?, ?, ?)", (doc_id, filename, raw_text))
    conn.commit()
    return get(conn, doc_id)


def _insert_rows(conn: sqlite3.Connection, doc_id: int, rows: list[MenuRow]) -> None:
    conn.executemany(
        "INSERT INTO menu_items (document_id, weekday, date, meal, items) VALUES (?, ?, ?, ?, ?)",
        [(doc_id, r.weekday, r.date.isoformat() if r.date else None, r.meal, r.items) for r in rows],
    )


def delete(conn: sqlite3.Connection, doc_id: int) -> None:
    """Forget a document and its menu rows (the original file under data/files is kept)."""
    conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))  # menu_items cascade
    conn.commit()


def get(conn: sqlite3.Connection, doc_id: int) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _doc(row) if row else None


def by_sha256(conn: sqlite3.Connection, sha256: str) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    return _doc(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Document]:
    return [_doc(r) for r in conn.execute("SELECT * FROM documents ORDER BY received_at DESC, id DESC")]


def waiting_for_week(conn: sqlite3.Connection) -> list[Document]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE status = 'needs_week' ORDER BY received_at DESC, id DESC"
    ).fetchall()
    return [_doc(r) for r in rows]


def rows_of(conn: sqlite3.Connection, doc_id: int) -> list[MenuRow]:
    rows = conn.execute("SELECT * FROM menu_items WHERE document_id = ? ORDER BY id", (doc_id,)).fetchall()
    return [MenuRow(r["weekday"], _date_or_none(r["date"]), r["meal"], r["items"]) for r in rows]


def set_week(conn: sqlite3.Connection, doc_id: int, week: WeekInfo) -> Document:
    """Record which week a menu covers, re-dating its rows to match."""
    rows = [MenuRow(r.weekday, None, r.meal, r.items) for r in rows_of(conn, doc_id)]
    if week.valid_from and week.valid_to and not week.repeats_weekly:
        rows = date_rows(rows, week.valid_from, week.valid_to)
    conn.execute(
        """UPDATE documents SET valid_from = ?, valid_to = ?, repeats_weekly = ?, status = 'ready'
           WHERE id = ?""",
        (week.valid_from.isoformat(), week.valid_to.isoformat() if week.valid_to else None,
         int(bool(week.repeats_weekly)), doc_id),
    )
    conn.execute("DELETE FROM menu_items WHERE document_id = ?", (doc_id,))
    _insert_rows(conn, doc_id, rows)
    conn.commit()
    return get(conn, doc_id)


def covering_menu(conn: sqlite3.Connection, day: date) -> Document | None:
    """The menu in force on `day`: newest received wins when menus overlap."""
    row = conn.execute(
        """SELECT * FROM documents
           WHERE kind = 'mess_menu' AND status = 'ready'
             AND valid_from <= :d AND (valid_to IS NULL OR valid_to >= :d)
           ORDER BY received_at DESC, id DESC LIMIT 1""",
        {"d": day.isoformat()},
    ).fetchone()
    return _doc(row) if row else None


def menu_for_day(conn: sqlite3.Connection, day: date) -> tuple[Document | None, list[MenuRow]]:
    doc = covering_menu(conn, day)
    if doc is None:
        return None, []
    rows = conn.execute(
        """SELECT * FROM menu_items
           WHERE document_id = ? AND (date = ? OR (date IS NULL AND weekday = ?))""",
        (doc.id, day.isoformat(), day.weekday()),
    ).fetchall()
    found = [MenuRow(r["weekday"], _date_or_none(r["date"]), r["meal"], r["items"]) for r in rows]
    return doc, sorted(found, key=lambda r: MEALS.index(r.meal))


def search(conn: sqlite3.Connection, query: str, limit: int = 3) -> list[tuple[Document, str]]:
    """Full-text search over every stored file, best match first, with a short excerpt."""
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in _SEARCH_STOP and len(w) > 1]
    if not words:
        return []
    match = " OR ".join(f'"{w}"*' for w in words)
    rows = conn.execute(
        """SELECT rowid, snippet(documents_fts, 1, '', '', ' … ', 40) AS excerpt
           FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?""",
        (match, limit),
    ).fetchall()
    return [(get(conn, r["rowid"]), r["excerpt"]) for r in rows]
