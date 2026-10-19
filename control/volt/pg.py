"""Postgres connection adapter. Imports psycopg only when a postgres DSN is used.

Gives psycopg 3 the small surface the rest of volt already uses on a
sqlite3 connection: execute() with `?` placeholders, rows readable by name
or index, commit, rollback, close. Timestamps come back as the same
`YYYY-MM-DDTHH:MM:SSZ` text the SQLite contract stores.

Transactions. psycopg opens a transaction on the first statement, even a SELECT, and keeps it open until
commit. The rest of volt ends a unit of work with commit(), which a read never reaches, so a bare read would
leave the connection "idle in transaction", holding locks that block other connections (a schema bootstrap's
ALTER TABLE queues behind it). PgConnection therefore ends a transaction itself after a plain read that has
no write pending; reads inside a write, and anything inside `transaction()`, stay in one transaction.

Pooling is optional: `configure_pool(dsn, ...)` makes every PgConnection to that DSN borrow from a
psycopg_pool pool, imported only then. Without it each PgConnection opens its own connection, as before.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
# Arbitrary constant. Serializes schema bootstrap across concurrent connections.
_SCHEMA_LOCK = 0x566F6C74


# Columns added after a table first shipped, before volt/migrations.py existed. They are ensured on every
# start for both backends (migrations.legacy_columns maps the types for SQLite). Adding a line here still
# works, but new schema changes belong in migrations.MIGRATIONS as a numbered entry. Keep in step with schema.sql.
_ADDED_COLUMNS = (
    ("item", "as_run_in", "TIMESTAMPTZ"),
    ("item", "as_run_out", "TIMESTAMPTZ"),
    ("show", "hold_reason", "TEXT"),
    ("show", "held_at", "TIMESTAMPTZ"),
    ("job", "claimed_by", "TEXT"),
    ("job", "lease_expires_at", "TIMESTAMPTZ"),
    ("job", "run_after", "TIMESTAMPTZ"),
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


_NOT_A_PLAIN_READ = (" FOR UPDATE", " FOR SHARE", " FOR NO KEY UPDATE", " FOR KEY SHARE", "RETURNING", "PG_ADVISORY")


def _is_plain_read(sql: str) -> bool:
    """A SELECT that takes no row lock and writes nothing: safe to end the transaction after."""
    head = sql.lstrip().upper()
    if not head.startswith("SELECT"):
        return False
    return not any(token in head for token in _NOT_A_PLAIN_READ)


# ---- pooling -----------------------------------------------------------------------------------

POOL_ENV = {
    "max_size": ("VOLT_PG_POOL_MAX", int),            # > 0 turns pooling on
    "min_size": ("VOLT_PG_POOL_MIN", int),
    "timeout": ("VOLT_PG_POOL_TIMEOUT", float),        # seconds to wait for a free connection
    "max_idle": ("VOLT_PG_POOL_MAX_IDLE", float),      # seconds before an idle connection is closed
    "max_lifetime": ("VOLT_PG_POOL_MAX_LIFETIME", float),
}


class Pool:
    """A psycopg_pool.ConnectionPool with volt's row factory and a health check on every checkout.

    A connection the server dropped (restart, idle kill, network) is found by the check, discarded and
    replaced, so the caller never receives a dead one. One that dies while checked out surfaces as the
    driver's error to that caller only; putconn discards it and the pool reconnects in the background."""

    def __init__(self, dsn: str, max_size: int = 10, min_size: int = 1, timeout: float = 30.0,
                 max_idle: float = 600.0, max_lifetime: float = 3600.0):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "a connection pool is configured but psycopg_pool is not installed; pip install psycopg_pool"
            ) from exc
        if max_size < 1:
            raise ValueError("pool max_size must be at least 1")
        self.dsn = dsn
        self.max_size = max_size
        self._pool = ConnectionPool(
            dsn, min_size=min(min_size, max_size), max_size=max_size, timeout=timeout,
            max_idle=max_idle, max_lifetime=max_lifetime, check=ConnectionPool.check_connection,
            kwargs={"row_factory": _row_factory, "application_name": "volt-pool"}, open=False, name="volt")
        self._pool.open(wait=True, timeout=timeout)  # fail at startup, loudly, if the database is unreachable

    def getconn(self):
        return self._pool.getconn()

    def putconn(self, conn) -> None:
        self._pool.putconn(conn)  # rolls back an open transaction, discards a broken connection

    def stats(self) -> dict:
        return self._pool.get_stats()

    def close(self) -> None:
        self._pool.close()


_POOLS: dict = {}
_POOLS_LOCK = threading.Lock()


def configure_pool(dsn: str, **options) -> Pool:
    """Pool every later PgConnection to `dsn`. Replaces (and closes) an earlier pool for the same DSN."""
    pool = Pool(dsn, **options)
    with _POOLS_LOCK:
        old = _POOLS.get(dsn)
        _POOLS[dsn] = pool
    if old is not None:
        old.close()
    return pool


def pool_for(dsn: str):
    return _POOLS.get(dsn)


def close_pools() -> None:
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


def pool_options(env=None, **overrides) -> dict:
    """Pool options from VOLT_PG_POOL_* (and explicit overrides). Empty when pooling is not asked for."""
    env = os.environ if env is None else env
    options = {}
    for key, (name, cast) in POOL_ENV.items():
        if env.get(name):
            options[key] = cast(env[name])
    options.update({k: v for k, v in overrides.items() if v is not None})
    if options.get("max_size", 0) <= 0:
        return {}
    return options


def configure_pool_from_env(dsn: str, env=None, **overrides):
    """Turn pooling on when VOLT_PG_POOL_MAX (or an override) asks for it. Returns the Pool or None."""
    if not is_dsn(dsn):
        return None
    options = pool_options(env, **overrides)
    return configure_pool(dsn, **options) if options else None


# ---- connection --------------------------------------------------------------------------------

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
        self._pool = pool_for(dsn)
        self._conn = self._pool.getconn() if self._pool else psycopg.connect(dsn, row_factory=_row_factory)
        self._writing = False  # a statement that is not a plain read ran since the last commit/rollback
        self._depth = 0        # > 0 inside transaction(): nothing ends the transaction but its exit

    def execute(self, sql: str, params=()):
        try:
            cur = self._conn.execute(translate(sql), tuple(params))
        except Exception:
            if not self._writing and not self._depth:
                self._conn.rollback()  # a failed read must not leave an aborted transaction behind
            raise
        if not self._depth and not self._writing:
            if _is_plain_read(sql):
                self._conn.commit()  # rows are already client-side; this only ends the transaction
            else:
                self._writing = True
        return cur

    @contextmanager
    def transaction(self, lock=None):
        """One transaction for everything inside, committed on exit and rolled back on an exception.

        `lock` names an advisory lock held until the transaction ends, so concurrent callers with the same
        name run their read-modify-write one after another. Reentrant: an inner call joins the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._depth = 1
        try:
            if lock is not None:
                self._conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
            yield
        except BaseException:
            self._depth = 0
            self._writing = False
            self._conn.rollback()
            raise
        self._depth = 0
        self._writing = False
        self._conn.commit()

    def apply_schema(self) -> None:
        """Apply schema.sql and the migrations. Idempotent; serialized across concurrent callers."""
        from volt import migrations

        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
            self._conn.execute(SCHEMA_FILE.read_text())
            migrations.run(migrations.PostgresBackend(self._conn))

    def commit(self) -> None:
        if self._depth:
            return  # the enclosing transaction() decides
        self._writing = False
        self._conn.commit()

    def rollback(self) -> None:
        self._writing = False
        self._conn.rollback()

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus
        return self._conn.info.transaction_status != TransactionStatus.IDLE

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        if self._pool:
            self._pool.putconn(conn)
        else:
            conn.close()
