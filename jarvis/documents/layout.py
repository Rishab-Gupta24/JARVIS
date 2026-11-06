"""Read a menu grid from a PDF page by where the words sit, not by table detection.

Real mess-menu PDFs often defeat table detection: borders drawn as one rectangle
per cell, row shading drawn line by line, and the meal headings typed as
spaced-out letters ("B R E A K F A S T") above the table rather than inside it.
pdfplumber then reports dozens of fragments. Here instead:

- the heading line is the one with at least two meal names (spaced letters joined up);
- each full day name in the left column starts a row;
- row and column borders are the edges that span (almost) the whole table, which
  separates real borders from in-cell shading; without borders, midpoints are used;
- each cell's words are put back into lines, one dish per line, exactly as printed.

The result is an ordinary table (heading row + one row per day) for menu.parse_tables.
Only used when a page has >= 2 meal headings and >= 3 day names.
"""

from __future__ import annotations

from jarvis.documents.menu import meal_of

FULL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
Table = list[list[str]]


def _center(w: dict, axis: str) -> float:
    return (w["x0"] + w["x1"]) / 2 if axis == "x" else (w["top"] + w["bottom"]) / 2


def _lines(words: list[dict], tolerance: float = 3.0) -> list[list[dict]]:
    """Group words into lines (top to bottom), each line left to right."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _join_letters(line: list[dict], max_gap: float = 15.0) -> list[dict]:
    """'B','R','E','A','K'... -> one 'BREAKFAST' token; normal words pass through."""
    tokens: list[dict] = []
    for w in line:
        prev = tokens[-1] if tokens else None
        if (prev is not None and len(w["text"]) <= 2 and prev.get("_letters")
                and w["x0"] - prev["x1"] <= max_gap):
            prev.update(text=prev["text"] + w["text"], x1=w["x1"])
            continue
        token = dict(w)
        token["_letters"] = len(w["text"]) <= 2
        tokens.append(token)
    return tokens


def _borders(edges: list[dict], axis: str, lo: float, hi: float, min_cover: float = 0.95) -> list[float]:
    """Positions of edges that, together, cover >= 95% of the table's span across them."""
    along = ("x0", "x1") if axis == "y" else ("top", "bottom")
    at = "top" if axis == "y" else "x0"
    groups: dict[int, list[tuple[float, float]]] = {}
    for e in edges:
        if e[along[1]] - e[along[0]] > 1:
            groups.setdefault(round(e[at]), []).append((e[along[0]], e[along[1]]))
    found = []
    for pos, spans in sorted(groups.items()):
        covered, end = 0.0, lo
        for a, b in sorted(spans):
            a = max(a, end)
            if b > a:
                covered += min(b, hi) - a if a < hi else 0
                end = max(end, b)
        if covered / max(hi - lo, 1) >= min_cover and (not found or pos - found[-1] > 3):
            found.append(float(pos))
    return found


def _interval(center: float, borders: list[float], fallback: tuple[float, float]) -> tuple[float, float]:
    for a, b in zip(borders, borders[1:]):
        if a <= center <= b:
            return a, b
    return fallback


def page_grid(page) -> Table | None:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    day_words = [w for w in words if w["text"].lower().strip(",.:") in FULL_DAYS]
    if len({w["text"].lower() for w in day_words}) < 3:
        return None
    first_day_top = min(w["top"] for w in day_words)

    # the heading line: above the first day, with >= 2 meal names
    heading = None
    for line in _lines([w for w in words if w["bottom"] <= first_day_top + 1]):
        tokens = _join_letters(line)
        if sum(meal_of(t["text"]) is not None for t in tokens) >= 2:
            heading = tokens
    if heading is None:
        return None
    label_x = sum(_center(w, "x") for w in day_words) / len(day_words)
    columns = [t for t in heading if _center(t, "x") > label_x + 20]  # meal (and other) headings
    label_heading = " ".join(t["text"] for t in heading if _center(t, "x") <= label_x + 20)

    # borders: whole-table edges; else midpoints between headings / day names
    left = min(w["x0"] for w in words)
    right = max(w["x1"] for w in words)
    top = min(t["top"] for t in heading) - 5
    bottom = max(w["bottom"] for w in words) + 5
    # column borders must run the full height of the day rows (the heading may sit outside the table)
    xs = _borders(page.vertical_edges, "x", min(w["top"] for w in day_words) - 5,
                  max(w["bottom"] for w in day_words) + 5)
    ys = _borders(page.horizontal_edges, "y", left, right)

    centers = [_center(t, "x") for t in columns]
    mids = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    col_spans = []
    for i, c in enumerate(centers):
        fallback = (mids[i - 1] if i else (label_x + c) / 2, mids[i] if i < len(mids) else right + 1)
        col_spans.append(_interval(c, xs, fallback))
    label_span = (left - 1, col_spans[0][0])

    # one row per day: the bordered band holding its label (labels may be two lines)
    days = sorted(day_words, key=lambda w: w["top"])
    day_centers = [_center(w, "y") for w in days]
    row_mids = [(a + b) / 2 for a, b in zip(day_centers, day_centers[1:])]
    rows = []
    for i, c in enumerate(day_centers):
        fallback = (row_mids[i - 1] if i else top, row_mids[i] if i < len(row_mids) else bottom)
        rows.append(_interval(c, ys, fallback))

    def cell(xspan, yspan) -> str:
        inside = [w for w in words if xspan[0] <= _center(w, "x") < xspan[1]
                  and yspan[0] <= _center(w, "y") < yspan[1]]
        return "\n".join(" ".join(w["text"] for w in line) for line in _lines(inside))

    table = [[label_heading] + [t["text"] for t in columns]]
    for yspan in rows:
        table.append([cell(label_span, yspan)] + [cell(xspan, yspan) for xspan in col_spans])
    return table
