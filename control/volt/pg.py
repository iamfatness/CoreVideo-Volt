"""Postgres connection adapter. Imports psycopg only when a postgres DSN is used.

Gives psycopg 3 the small surface the rest of volt already uses on a
sqlite3 connection: execute() with `?` placeholders, rows readable by name
or index, commit, rollback, close. Timestamps come back as the same
`YYYY-MM-DDTHH:MM:SSZ` text the SQLite contract stores.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schema.sql"
# Arbitrary constant. Serializes schema bootstrap across concurrent connections.
_SCHEMA_LOCK = 0x566F6C74


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves an existing table
# alone, so a database made by an older schema.sql needs these. Keep in step with schema.sql.
_ADDED_COLUMNS = (
    ("item", "as_run_in", "TIMESTAMPTZ"),
    ("item", "as_run_out", "TIMESTAMPTZ"),
    ("show", "hold_reason", "TEXT"),
    ("show", "held_at", "TIMESTAMPTZ"),
    ("job", "claimed_by", "TEXT"),
    ("job", "lease_expires_at", "TIMESTAMPTZ"),
    ("job", "run_after", "TIMESTAMPTZ"),
    ("version", "storage_backend", "TEXT"),
    ("version", "storage_key", "TEXT"),
    ("version", "storage_sha256", "TEXT"),
)


def is_dsn(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


def translate(sql: str) -> str:
    """`?` -> `%s` outside single-quoted literals; literal `%` -> `%%`."""
    out = []
    quoted = False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
        if ch == "%":
            out.append("%%")
        elif ch == "?" and not quoted:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class Row(tuple):
    """Tuple that also answers row["name"], like sqlite3.Row."""

    _names: dict = {}

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._names[key])
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _value(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return v


def _row_factory(cursor):
    names = {c.name: i for i, c in enumerate(cursor.description or [])}
    cls = type("Row", (Row,), {"_names": names})
    return lambda values: cls(_value(v) for v in values)


class PgConnection:
    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "a postgres DSN is configured but psycopg is not installed; "
                "pip install -r control/requirements.txt"
            ) from exc
        self.IntegrityError = psycopg.IntegrityError
        self._conn = psycopg.connect(dsn, row_factory=_row_factory)

    def execute(self, sql: str, params=()):
        return self._conn.execute(translate(sql), tuple(params))

    def apply_schema(self) -> None:
        """Apply schema.sql. Idempotent; serialized across concurrent callers."""
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(SCHEMA_FILE.read_text())
            for table, column, kind in _ADDED_COLUMNS:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
