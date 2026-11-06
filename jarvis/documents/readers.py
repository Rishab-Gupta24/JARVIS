"""Read a file into plain text plus any tables it contains.

PDF (text and ruled tables) via pdfplumber, Excel via openpyxl (keeps merged
cells, which pandas drops), CSV and text directly, and photos or scanned PDF
pages through a local vision model that transcribes them, tables as Markdown.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.core.llm import Transcriber

Table = list[list[str]]

TEXT_TYPES = {".txt", ".md", ".text"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
SUPPORTED = TEXT_TYPES | IMAGE_TYPES | {".pdf", ".xlsx", ".xlsm", ".csv", ".tsv"}


class UnreadableFile(ValueError):
    """The file type isn't supported, or nothing could be read from it."""


@dataclass
class Extracted:
    text: str
    tables: list[Table] = field(default_factory=list)
    used_vision: bool = False


def read_bytes(data: bytes, filename: str, vision: Transcriber | None = None) -> Extracted:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _read_pdf(data, vision)
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(data)
    if ext in (".csv", ".tsv"):
        return _read_csv(_decode(data), "\t" if ext == ".tsv" else None)
    if ext in TEXT_TYPES:
        return read_text(_decode(data))
    if ext in IMAGE_TYPES:
        return _read_image(data, vision)
    if ext in (".heic", ".heif"):
        raise UnreadableFile("HEIC photos aren't supported; send it as JPEG (the Share Sheet Shortcut converts it).")
    raise UnreadableFile(f"I can't read {ext or 'that'} files yet. Send a PDF, Excel, CSV, text file or photo.")


def read_text(text: str) -> Extracted:
    """Plain text, e.g. shared from the phone. Markdown or tab-separated tables inside it are picked up."""
    text = text.replace("\r\n", "\n").strip()
    return Extracted(text, markdown_tables(text) or _tsv_tables(text))


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell(value) -> str:
    return "" if value is None else str(value).strip()


def _tables_to_text(tables: list[Table]) -> str:
    return "\n\n".join("\n".join("\t".join(row) for row in t) for t in tables)


def _read_pdf(data: bytes, vision: Transcriber | None) -> Extracted:
    import pdfplumber

    texts, tables, used_vision = [], [], False
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                texts.append(page_text)
                tables += [[[_cell(c) for c in row] for row in t] for t in page.extract_tables()]
            elif vision is not None:  # scanned page: no text layer
                image = io.BytesIO()
                page.to_image(resolution=200).original.save(image, format="PNG")
                scanned = vision.transcribe(image.getvalue())
                texts.append(scanned)
                tables += markdown_tables(scanned)
                used_vision = True
    text = "\n\n".join(texts).strip()
    if not text:
        raise UnreadableFile("That PDF has no text layer (it's a scan), and the vision model isn't available.")
    return Extracted(text, tables, used_vision)


def _read_xlsx(data: bytes) -> Extracted:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    tables = []
    for ws in wb.worksheets:
        grid = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        # A merged cell keeps its value only in the top-left corner. Copy it across the
        # columns it spans (one dish for Mon-Wed counts for each day) but not down the
        # rows: blank rows under a label already read as "same meal, continued", and
        # copying a dish downwards would list it twice.
        for merged in ws.merged_cells.ranges:
            top, left = merged.min_row - 1, merged.min_col - 1
            if top < len(grid) and left < len(grid[top]):
                for c in range(left + 1, min(merged.max_col, len(grid[top]))):
                    grid[top][c] = grid[top][left]
        grid = [row for row in grid if any(row)]
        if grid:
            tables.append(grid)
    if not tables:
        raise UnreadableFile("That spreadsheet is empty.")
    return Extracted(_tables_to_text(tables), tables)


def _read_csv(text: str, delimiter: str | None) -> Extracted:
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise UnreadableFile("That file is empty.")
    return Extracted(_tables_to_text([rows]), [rows])


def _read_image(data: bytes, vision: Transcriber | None) -> Extracted:
    if vision is None:
        raise UnreadableFile("Reading photos needs the vision model, which isn't set up.")
    text = vision.transcribe(data).strip()
    if not text:
        raise UnreadableFile("I couldn't read any text in that photo.")
    return Extracted(text, markdown_tables(text), used_vision=True)


_MD_RULE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def markdown_tables(text: str) -> list[Table]:
    """Pull Markdown tables (| a | b |) out of text, e.g. a vision model's transcription."""
    tables, current = [], []
    for line in text.splitlines() + [""]:
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            if not _MD_RULE.match(s):
                current.append([c.strip().replace("<br>", "\n") for c in s.strip("|").split("|")])
            continue
        if len(current) >= 2:
            tables.append(current)
        current = []
    return tables


def _tsv_tables(text: str) -> list[Table]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    tabbed = [ln for ln in lines if ln.count("\t") >= 2]
    if len(tabbed) >= 3:
        return [[[c.strip() for c in ln.split("\t")] for ln in tabbed]]
    return []
