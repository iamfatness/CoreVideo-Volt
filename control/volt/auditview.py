"""Reading the audit table: CSV that a spreadsheet cannot turn into a formula."""

from __future__ import annotations

import csv
import io

COLUMNS = ("at", "actor", "action", "target_kind", "target_id", "detail", "id")


def csv_cell(value) -> str:
    """A cell as text. A value that starts with = + - @ (or a tab or carriage return) gets a leading single quote, so Excel and
    Sheets show it as text instead of running it (CSV injection). The csv module does the quoting of commas, quotes and newlines."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def render_csv(rows: list) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow([csv_cell(r.get(c)) for c in COLUMNS])
    return out.getvalue()
