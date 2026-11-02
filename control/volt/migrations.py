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
    "REAL": ("REAL", "DOUBLE PRECISION"),
    "BLOB": ("BLOB", "BYTEA"),
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


def _device_tables(ts: str) -> list:
    """The device-control tables as CREATE IF NOT EXISTS statements; `ts` is the timestamp type for the backend."""
    return [
        f"CREATE TABLE IF NOT EXISTS device_status (device TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, detail TEXT, observed TEXT, updated_at {ts} NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS device_event (id TEXT PRIMARY KEY, at {ts} NOT NULL, device TEXT NOT NULL, command TEXT NOT NULL, params TEXT, outcome TEXT NOT NULL, detail TEXT, cue_id TEXT, actor TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS device_safety (id INTEGER PRIMARY KEY, safe_mode INTEGER NOT NULL, reason TEXT, set_by TEXT, set_at {ts})",
        f"CREATE TABLE IF NOT EXISTS macro (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, version INTEGER NOT NULL, created_at {ts} NOT NULL)",
        "CREATE TABLE IF NOT EXISTS macro_step (macro_id TEXT NOT NULL, version INTEGER NOT NULL, position INTEGER NOT NULL, room TEXT NOT NULL, command TEXT NOT NULL, payload TEXT NOT NULL, offset_ms INTEGER NOT NULL, group_key TEXT, PRIMARY KEY (macro_id, version, position))",
        f"CREATE TABLE IF NOT EXISTS device_confirm (id TEXT PRIMARY KEY, cue_id TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at {ts} NOT NULL, expires_at {ts} NOT NULL, state TEXT NOT NULL)",
    ]


def _sqlite_fts(db) -> None:
    """The FTS5 table over search_doc.body and the triggers that keep it in step. A SQLite built without FTS5 skips this:
    volt.searchindex then answers text queries with LIKE and reports degraded."""
    import sqlite3
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
                   "body, content='search_doc', content_rowid='seq', tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            return
        raise
    db.execute("CREATE TRIGGER IF NOT EXISTS search_doc_ai AFTER INSERT ON search_doc BEGIN "
               "INSERT INTO search_fts(rowid, body) VALUES (new.seq, new.body); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS search_doc_ad AFTER DELETE ON search_doc BEGIN "
               "INSERT INTO search_fts(search_fts, rowid, body) VALUES ('delete', old.seq, old.body); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS search_doc_au AFTER UPDATE OF body ON search_doc BEGIN "
               "INSERT INTO search_fts(search_fts, rowid, body) VALUES ('delete', old.seq, old.body); "
               "INSERT INTO search_fts(rowid, body) VALUES (new.seq, new.body); END")
    db.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")   # the table is new, so this indexes any rows already there


MIGRATIONS = (
    Migration(1,"item as-run columns",
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
    Migration(15, "job payload", columns=(("job", "payload", "TEXT"),)),
    # audit is a new table (base-table creation makes it). The statements also create it on an older file
    # whose SCHEMA snapshot did not include it yet; IF NOT EXISTS keeps a fresh database a no-op.
    Migration(16, "audit table",
              sqlite=("CREATE TABLE IF NOT EXISTS audit ("
                      "id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, "
                      "target_kind TEXT NOT NULL, target_id TEXT NOT NULL, detail TEXT)",),
              postgres=("CREATE TABLE IF NOT EXISTS audit ("
                        "id TEXT PRIMARY KEY, at TIMESTAMPTZ NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, "
                        "target_kind TEXT NOT NULL, target_id TEXT NOT NULL, detail TEXT)",)),
    # volt_user is a new table (base-table creation makes it), as in 4, 7 and 12: per-user tokens, stored as a
    # sha256 hash with a role (operator or admin). Named volt_user because USER is reserved on Postgres.
    Migration(17, "volt_user table"),
    # The planned length of a show in seconds, for total-against-slot and back time. NULL means no slot is set.
    Migration(18, "show slot length", columns=(("show", "slot_seconds", "INTEGER"),)),
    # Relative schedules (item.schedule_mode: NULL or 'absolute' is the old scheduled_at, 'after_previous' is due at the
    # previous item's start plus its length plus schedule_offset_seconds) and recurring shows (show.recurrence is a JSON rule;
    # a clone carries recurrence_of and occurrence_on, and the unique index lets two clocks race to make it).
    Migration(19, "relative and recurring schedules",
              columns=(("item", "schedule_mode", "TEXT"), ("item", "schedule_offset_seconds", "INTEGER"),
                       ("show", "recurrence", "TEXT"), ("show", "recurrence_of", "TEXT"), ("show", "occurrence_on", "TEXT")),
              sqlite=("CREATE UNIQUE INDEX IF NOT EXISTS show_occurrence ON show (recurrence_of, occurrence_on)",),
              postgres=("CREATE UNIQUE INDEX IF NOT EXISTS show_occurrence ON show (recurrence_of, occurrence_on)",)),
    # Archive tier. essence.tier is NULL or 'online' (the file is local), 'archiving', 'archived' (only the object under
    # archive_key exists; archive_sha256 is what a restore must match) or 'restoring'. asset.archived_at is when it went cold.
    Migration(20, "archive tier",
              columns=(("essence", "tier", "TEXT"), ("essence", "archive_key", "TEXT"), ("essence", "archive_sha256", "TEXT"),
                       ("asset", "archived_at", "TIMESTAMP"))),
    # The MOS sender (volt/mos_send.py): what each device last acknowledged per show, the per-device message counter, and the connection
    # state the desk sees. All new tables, so SCHEMA makes them on a fresh database; these statements make them on an older one.
    Migration(21, "mos sender tables",
              sqlite=("CREATE TABLE IF NOT EXISTS mos_sync (device TEXT NOT NULL, show_id TEXT NOT NULL, state TEXT NOT NULL, acked_hash TEXT, error TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (device, show_id))",
              "CREATE TABLE IF NOT EXISTS mos_counter (device TEXT PRIMARY KEY, next_id INTEGER NOT NULL)",
              "CREATE TABLE IF NOT EXISTS mos_device (device TEXT PRIMARY KEY, state TEXT NOT NULL, detail TEXT, updated_at TEXT NOT NULL)",),
              postgres=("CREATE TABLE IF NOT EXISTS mos_sync (device TEXT NOT NULL, show_id TEXT NOT NULL, state TEXT NOT NULL, acked_hash TEXT, error TEXT, updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (device, show_id))",
              "CREATE TABLE IF NOT EXISTS mos_counter (device TEXT PRIMARY KEY, next_id INTEGER NOT NULL)",
              "CREATE TABLE IF NOT EXISTS mos_device (device TEXT PRIMARY KEY, state TEXT NOT NULL, detail TEXT, updated_at TIMESTAMPTZ NOT NULL)",)),
    # Groups and richer users (volt/access.py): who a person is (email, display name, where the account came from) and the groups that can
    # raise their role. New tables, so SCHEMA makes them on a fresh database; the statements make them on an older one.
    Migration(22, "groups and user profile columns",
              columns=(("volt_user", "email", "TEXT"), ("volt_user", "display_name", "TEXT"), ("volt_user", "source", "TEXT")),
              sqlite=("CREATE TABLE IF NOT EXISTS volt_group (name TEXT PRIMARY KEY, role TEXT, created_at TEXT NOT NULL)",
                      "CREATE TABLE IF NOT EXISTS group_member (group_name TEXT NOT NULL, user_name TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (group_name, user_name))"),
              postgres=("CREATE TABLE IF NOT EXISTS volt_group (name TEXT PRIMARY KEY, role TEXT, created_at TIMESTAMPTZ NOT NULL)",
                        "CREATE TABLE IF NOT EXISTS group_member (group_name TEXT NOT NULL, user_name TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (group_name, user_name))")),
    # The audit view filters by actor, action and time, newest first: indexes for those, on both backends.
    Migration(23, "audit view indexes",
              sqlite=("CREATE INDEX IF NOT EXISTS audit_at ON audit (at, id)", "CREATE INDEX IF NOT EXISTS audit_actor_at ON audit (actor, at)",
                      "CREATE INDEX IF NOT EXISTS audit_action_at ON audit (action, at)"),
              postgres=("CREATE INDEX IF NOT EXISTS audit_at ON audit (at, id)", "CREATE INDEX IF NOT EXISTS audit_actor_at ON audit (actor, at)",
                        "CREATE INDEX IF NOT EXISTS audit_action_at ON audit (action, at)")),
    # Device control (docs/superpowers/specs/2026-11-01-device-control-design.md): cue timing and preview columns (all nullable; a NULL
    # offset or preroll reads as 0, no group), and the new tables device_status, device_event, device_safety, macro, macro_step and
    # device_confirm. The tables are made by base-table creation; these statements make them on an older database.
    Migration(24, "device control",
              columns=(("cue", "offset_ms", "INTEGER"), ("cue", "preroll_ms", "INTEGER"), ("cue", "group_key", "TEXT"),
                       ("cue", "due_at", "TIMESTAMP"), ("cue", "claimed_at", "TIMESTAMP"), ("cue", "preload_status", "TEXT"),
                       ("cue", "preload_detail", "TEXT"), ("cue", "preloaded_at", "TIMESTAMP")),
              sqlite=tuple(_device_tables("TEXT")), postgres=tuple(_device_tables("TIMESTAMPTZ"))),
    # Newsroom scripts. The item gains its editorial side: story_status (NULL reads as draft), script_rev (NULL reads as 1), the approval
    # record (approved_by/at/rev; approved means story_status 'approved' AND approved_rev = script_rev), floated (a backup story) and
    # source_wire_id. The show gains read_wpm (NULL = the default pace). story_rev, story_event, wire_story, story_template and
    # story_archive are new tables, so base-table creation makes them; this entry adds the one index "who approved revision 7" needs.
    Migration(25, "newsroom scripts",
              columns=(("item", "story_status", "TEXT"), ("item", "script_rev", "INTEGER"), ("item", "approved_by", "TEXT"),
                       ("item", "approved_at", "TIMESTAMP"), ("item", "approved_rev", "INTEGER"), ("item", "floated", "BOOLEAN"),
                       ("item", "source_wire_id", "TEXT"), ("show", "read_wpm", "INTEGER")),
              sqlite=("CREATE INDEX IF NOT EXISTS story_event_item ON story_event (item_id, kind, rev)",),
              postgres=("CREATE INDEX IF NOT EXISTS story_event_item ON story_event (item_id, kind, rev)",)),
    # Server-side sessions (volt/sessions.py): the cookie value is stored only as a sha256 hash. A new table, so SCHEMA makes it on a fresh database.
    Migration(26, "session table",
              sqlite=("CREATE TABLE IF NOT EXISTS session (id_hash TEXT PRIMARY KEY, user_name TEXT NOT NULL, provider TEXT NOT NULL, csrf TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL)",
                      "CREATE INDEX IF NOT EXISTS session_by_user ON session (user_name)"),
              postgres=("CREATE TABLE IF NOT EXISTS session (id_hash TEXT PRIMARY KEY, user_name TEXT NOT NULL, provider TEXT NOT NULL, csrf TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)",
                        "CREATE INDEX IF NOT EXISTS session_by_user ON session (user_name)")),

    # Typed asset metadata (volt/metadata.py). field_def, field_term and asset_meta_value are new tables (base-table creation
    # makes them); the indexes are here because SCHEMA runs on every open and these only matter on the new tables.
    Migration(27, "metadata tables",
              sqlite=("CREATE INDEX IF NOT EXISTS asset_meta_value_text ON asset_meta_value (field_id, v_text)",
                      "CREATE INDEX IF NOT EXISTS asset_meta_value_num ON asset_meta_value (field_id, v_num)",
                      "CREATE INDEX IF NOT EXISTS asset_meta_value_date ON asset_meta_value (field_id, v_date)"),
              postgres=("CREATE INDEX IF NOT EXISTS asset_meta_value_text ON asset_meta_value (field_id, v_text)",
                        "CREATE INDEX IF NOT EXISTS asset_meta_value_num ON asset_meta_value (field_id, v_num)",
                        "CREATE INDEX IF NOT EXISTS asset_meta_value_date ON asset_meta_value (field_id, v_date)")),
    # The derived search index (volt/searchindex.py). search_doc is a new table (base-table creation makes it). On SQLite the
    # full-text side is an FTS5 external-content table kept in step by triggers, made only when this SQLite has FTS5 (without it
    # search falls back to LIKE and says so). On Postgres it is a GIN index over the tsvector column the writer fills.
    Migration(28, "search index",
              sqlite=("CREATE INDEX IF NOT EXISTS search_doc_asset ON search_doc (asset_id, kind)",
                      "CREATE INDEX IF NOT EXISTS search_doc_value ON search_doc (kind, value_norm)",
                      "CREATE INDEX IF NOT EXISTS search_doc_person ON search_doc (person_id)",
                      _sqlite_fts),
              postgres=("CREATE INDEX IF NOT EXISTS search_doc_asset ON search_doc (asset_id, kind)",
                        "CREATE INDEX IF NOT EXISTS search_doc_value ON search_doc (kind, value_norm)",
                        "CREATE INDEX IF NOT EXISTS search_doc_person ON search_doc (person_id)",
                        "CREATE INDEX IF NOT EXISTS search_doc_tsv ON search_doc USING GIN (tsv)")),
    # Saved searches and bins (volt/collections.py): new tables (base-table creation makes them), indexes here. bin_member.span_id has
    # no foreign key on purpose: transcription and detection delete and rewrite spans, and a bin must never block them.
    Migration(29, "saved searches and bins",
              sqlite=(
                      "CREATE INDEX IF NOT EXISTS saved_search_owner ON saved_search (owner)",
                      "CREATE INDEX IF NOT EXISTS bin_owner ON bin (owner)",
                      "CREATE INDEX IF NOT EXISTS bin_member_bin ON bin_member (bin_id, position)",
                      "CREATE INDEX IF NOT EXISTS bin_member_asset ON bin_member (asset_id)",
              ),
              postgres=(
                      "CREATE INDEX IF NOT EXISTS saved_search_owner ON saved_search (owner)",
                      "CREATE INDEX IF NOT EXISTS bin_owner ON bin (owner)",
                      "CREATE INDEX IF NOT EXISTS bin_member_bin ON bin_member (bin_id, position)",
                      "CREATE INDEX IF NOT EXISTS bin_member_asset ON bin_member (asset_id)",
              )),
    # OIDC sign-in (volt/oidc.py): one row per login in flight (state, nonce, PKCE verifier; the browser binder is stored only hashed), the
    # (issuer, sub) a person signs in as, and the admin-set map from a provider's group claim value to a Volt group. New tables, so SCHEMA
    # makes them on a fresh database. Unvalidated against a real provider.
    Migration(30, "oidc sign-in tables",
              sqlite=("CREATE TABLE IF NOT EXISTS auth_flow (state TEXT PRIMARY KEY, nonce TEXT NOT NULL, code_verifier TEXT NOT NULL, provider TEXT NOT NULL, next TEXT NOT NULL, binder_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)",
                      "CREATE TABLE IF NOT EXISTS identity (issuer TEXT NOT NULL, sub TEXT NOT NULL, user_name TEXT NOT NULL, email TEXT, display_name TEXT, last_login_at TEXT NOT NULL, PRIMARY KEY (issuer, sub))",
                      "CREATE TABLE IF NOT EXISTS oidc_group_map (provider TEXT NOT NULL, claim_value TEXT NOT NULL, group_name TEXT NOT NULL, PRIMARY KEY (provider, claim_value, group_name))"),
              postgres=("CREATE TABLE IF NOT EXISTS auth_flow (state TEXT PRIMARY KEY, nonce TEXT NOT NULL, code_verifier TEXT NOT NULL, provider TEXT NOT NULL, next TEXT NOT NULL, binder_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)",
                        "CREATE TABLE IF NOT EXISTS identity (issuer TEXT NOT NULL, sub TEXT NOT NULL, user_name TEXT NOT NULL, email TEXT, display_name TEXT, last_login_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (issuer, sub))",
                        "CREATE TABLE IF NOT EXISTS oidc_group_map (provider TEXT NOT NULL, claim_value TEXT NOT NULL, group_name TEXT NOT NULL, PRIMARY KEY (provider, claim_value, group_name))")),
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
            if callable(stmt):   # a step that needs Python (an optional SQLite module), given the backend
                stmt(db)
            else:
                db.execute(stmt)
        db.record(migration)
        ran.append(migration.version)
    return ran


def current_version(db: _Backend) -> int:
    done = db.applied()
    return max(done) if done else 0
