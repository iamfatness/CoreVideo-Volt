"""Versioned schema migrations, one list for SQLite and Postgres.

How the schema reaches the latest shape, on either backend:

1. Base tables. `CREATE TABLE IF NOT EXISTS` for every table (store.SCHEMA / schema.sql). A fresh database
   is complete after this step. An existing database keeps the tables it has.
2. Legacy columns. `pg._ADDED_COLUMNS` (columns added before this module existed, and anything appended to
   it later in the old style) is ensured on every start. A column that is already there costs one catalog
   read and no ALTER, so a steady-state start takes no table lock.
3. Numbered migrations. `MIGRATIONS` below, each run once, in order, inside the same transaction as the rest
   of the bootstrap, and recorded in `schema_version`. On Postgres the whole bootstrap holds an advisory lock,
   so concurrent starters queue instead of colliding; on SQLite it holds the write lock (BEGIN IMMEDIATE).

To change the schema, add the column or table to `store.SCHEMA` and `schema.sql` (same column order; the
parity test checks it) and add ONE `Migration` at the end of `MIGRATIONS` with the next version number.
Never edit or renumber an entry that has shipped. Column types are written once, in the neutral names in
`_TYPES`, and become the right type for each backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Neutral type name -> (sqlite, postgres)
_TYPES = {
    "TEXT": ("TEXT", "TEXT"),
    "INTEGER": ("INTEGER", "INTEGER"),
    "BOOLEAN": ("INTEGER", "BOOLEAN"),
    "TIMESTAMP": ("TEXT", "TIMESTAMPTZ"),
}
# pg._ADDED_COLUMNS is written with Postgres type names; map them back for SQLite.
_PG_TO_NEUTRAL = {"TIMESTAMPTZ": "TIMESTAMP"}

VERSION_TABLE = "schema_version"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    # (table, column, neutral type). Added when missing; a column that already exists is left alone.
    columns: tuple = ()
    # Raw statements for changes that are not a plain ADD COLUMN. One per backend; either may be empty.
    sqlite: tuple = ()
    postgres: tuple = ()


MIGRATIONS = (
    Migration(1, "item as-run columns",
              columns=(("item", "as_run_in", "TIMESTAMP"), ("item", "as_run_out", "TIMESTAMP"))),
    Migration(2, "show hold columns",
              columns=(("show", "hold_reason", "TEXT"), ("show", "held_at", "TIMESTAMP"))),
    Migration(3, "job lease columns",
              columns=(("job", "claimed_by", "TEXT"), ("job", "lease_expires_at", "TIMESTAMP"),
                       ("job", "run_after", "TIMESTAMP"))),
    # asr_run is a new table, so base-table creation (step 1) makes it. The entry records that the
    # database has been through that point; it changes nothing by itself.
    Migration(4, "asr_run table"),
    Migration(5, "item schedule columns",
              columns=(("item", "scheduled_at", "TIMESTAMP"), ("item", "schedule_state", "TEXT"),
                       ("item", "schedule_note", "TEXT"), ("item", "schedule_claimed_at", "TIMESTAMP"))),
    Migration(6, "version storage columns",
              columns=(("version", "storage_backend", "TEXT"), ("version", "storage_key", "TEXT"),
                       ("version", "storage_sha256", "TEXT"))),
    Migration(7, "asr settings and chunk table", columns=(("asr_run", "settings", "TEXT"),)),  # asr_chunk: CREATE IF NOT EXISTS
    Migration(8, "cue last failure columns",
              columns=(("cue", "fail_status", "TEXT"), ("cue", "fail_detail", "TEXT"))),
    # shot_run is a new table (base-table creation makes it), as in 4 and 7. The index is created here and not
    # in store.SCHEMA: SCHEMA runs before the columns exist on an older database.
    Migration(9, "span shot columns, essence duration, shot_run table",
              columns=(("span", "label", "TEXT"), ("span", "origin", "TEXT"), ("span", "run_id", "TEXT"),
                       ("span", "frame_in", "INTEGER"), ("span", "frame_out", "INTEGER"),
                       ("essence", "duration_frames", "INTEGER")),
              sqlite=("CREATE INDEX IF NOT EXISTS span_asset_kind_frame ON span (asset_id, kind, frame_in)",),
              postgres=("CREATE INDEX IF NOT EXISTS span_asset_kind_frame ON span (asset_id, kind, frame_in)",)),
    # timecode_source: where timecode_start came from (tmcd_track, video_stream_tag, format_tag, or none/invalid
    # when it stays NULL = 00:00:00:00). The rest are what the file says about its own codecs.
    Migration(10, "essence media facts",
              columns=(("essence", "timecode_source", "TEXT"), ("essence", "video_profile", "TEXT"),
                       ("essence", "pix_fmt", "TEXT"), ("essence", "audio_codec", "TEXT"),
                       ("essence", "audio_sample_rate", "INTEGER"))),
    # purge_audit is a new table (base-table creation makes it); asset.deleted_at is the soft-delete tombstone.
    Migration(11, "asset tombstone, purge audit table", columns=(("asset", "deleted_at", "TIMESTAMP"),)),
    # runner_heartbeat is a new table (base-table creation makes it), as in 4 and 7: each runner upserts a row so
    # health can report how long ago one last checked in.
    Migration(12, "runner_heartbeat table"),
    # The as-run time to the millisecond, as text (2026-10-10T20:00:09.123Z). as_run_in/out keep their second precision.
    Migration(13, "item as-run millisecond columns",
              columns=(("item", "as_run_in_precise", "TEXT"), ("item", "as_run_out_precise", "TEXT"))),
    # Unpublish: a withdrawn version keeps its row (status 'withdrawn'); withdrawn_at is when, storage_deleted_at is when the
    # object Volt stored for it was removed through the storage seam (NULL while that is still to do or was never owned).
    Migration(14, "version withdrawal columns",
              columns=(("version", "withdrawn_at", "TIMESTAMP"), ("version", "storage_deleted_at", "TIMESTAMP"))),
    # Next: Migration(15, "...", columns=(...)). Do not add a gap or reuse a number.
)

LATEST = MIGRATIONS[-1].version


def _check_unique() -> None:
    versions = [m.version for m in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"MIGRATIONS must be numbered 1..n with no gaps or repeats, got {versions}")


_check_unique()


class _Backend:
    """What the runner needs from a database. Subclasses are SQLite and Postgres."""

    postgres = False

    def execute(self, sql: str, params: tuple = ()):
        raise NotImplementedError

    def columns(self, table: str) -> set:
        raise NotImplementedError

    def type_of(self, neutral: str) -> str:
        return _TYPES[neutral][1 if self.postgres else 0]

    def add_column(self, table: str, column: str, neutral: str) -> None:
        self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {self.type_of(neutral)}")

    def ensure_version_table(self) -> None:
        self.execute(f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
                     "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")

    def applied(self) -> set:
        return {row[0] for row in self.execute(f"SELECT version FROM {VERSION_TABLE}").fetchall()}

    def record(self, migration: Migration) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ph = "%s" if self.postgres else "?"
        self.execute(f"INSERT INTO {VERSION_TABLE} (version, name, applied_at) VALUES ({ph}, {ph}, {ph})",
                     (migration.version, migration.name, stamp))


class SqliteBackend(_Backend):
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def columns(self, table):
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}


class PostgresBackend(_Backend):
    postgres = True

    def __init__(self, conn):
        self.conn = conn  # a raw psycopg connection; statements use %s

    def execute(self, sql, params=()):
        return self.conn.execute(sql, tuple(params))

    def columns(self, table):
        rows = self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
        return {row[0] for row in rows}


def legacy_columns() -> tuple:
    """pg._ADDED_COLUMNS as (table, column, neutral type)."""
    from volt import pg
    return tuple((t, c, _PG_TO_NEUTRAL.get(k, k)) for t, c, k in pg._ADDED_COLUMNS)


def _ensure_columns(db: _Backend, columns) -> list:
    added = []
    cache: dict = {}
    for table, column, neutral in columns:
        have = cache.get(table)
        if have is None:
            have = cache[table] = db.columns(table)
        if column not in have:
            db.add_column(table, column, neutral)
            have.add(column)
            added.append(f"{table}.{column}")
    return added


class SchemaTooNewError(RuntimeError):
    """The database was migrated by newer code than this. Running anyway could misread or damage it."""

    def __init__(self, database: int, code: int):
        super().__init__(
            f"the database schema is version {database} but this code only knows up to version {code}; "
            "refusing to run against it. Upgrade this volt install to the version that wrote the database, "
            "or restore a backup made at schema version "
            f"{code} or older (python -m volt.backup restore)")
        self.database, self.code = database, code


def known_version() -> int:
    """The newest schema version this code can bring a database to (read live, so a test that adds one sees it)."""
    return max(m.version for m in MIGRATIONS)


def check_not_newer(db: _Backend) -> int:
    """Raise SchemaTooNewError when the database is ahead of this code. Returns the database's version."""
    have = current_version(db)
    if have > known_version():
        raise SchemaTooNewError(have, known_version())
    return have


def run(db: _Backend) -> list:
    """Bring an existing database (base tables already present) to the latest shape.

    Returns the versions applied this call. The caller owns the transaction and the lock. Refuses
    (SchemaTooNewError) a database whose recorded version is newer than this code knows."""
    db.ensure_version_table()
    check_not_newer(db)
    _ensure_columns(db, legacy_columns())
    done = db.applied()
    ran = []
    for migration in MIGRATIONS:
        if migration.version in done:
            continue
        _ensure_columns(db, migration.columns)
        for stmt in (migration.postgres if db.postgres else migration.sqlite):
            db.execute(stmt)
        db.record(migration)
        ran.append(migration.version)
    return ran


def current_version(db: _Backend) -> int:
    done = db.applied()
    return max(done) if done else 0
