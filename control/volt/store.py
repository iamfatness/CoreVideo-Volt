"""Volt control plane. SQLite runs the contract. Postgres is the same objects."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from volt import migrations, pg


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_precise() -> str:
    """Now to the millisecond, UTC: 2026-10-10T20:00:09.123Z. The second-precision _now() is its prefix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _id() -> str:
    return str(uuid.uuid4())


SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  retention_class TEXT NOT NULL,
  legal_hold INTEGER NOT NULL DEFAULT 0,
  cleared INTEGER NOT NULL DEFAULT 0,
  show_name TEXT,
  room TEXT,
  camera TEXT,
  source_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS essence (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  role TEXT NOT NULL,
  location TEXT NOT NULL,
  checksum TEXT,
  codec TEXT,
  frame_rate TEXT,
  timecode_start TEXT,
  audio_layout TEXT,
  open INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  duration_frames INTEGER,
  timecode_source TEXT,
  video_profile TEXT,
  pix_fmt TEXT,
  audio_codec TEXT,
  audio_sample_rate INTEGER
);

CREATE TABLE IF NOT EXISTS span (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  kind TEXT NOT NULL,
  tc_in TEXT NOT NULL,
  tc_out TEXT NOT NULL,
  text TEXT,
  created_at TEXT NOT NULL,
  label TEXT,
  origin TEXT,
  run_id TEXT,
  frame_in INTEGER,
  frame_out INTEGER
);

CREATE TABLE IF NOT EXISTS job (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  claimed_by TEXT,
  lease_expires_at TEXT,
  run_after TEXT,
  UNIQUE (asset_id, type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS asr_run (
  job_id TEXT PRIMARY KEY REFERENCES job(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  engine TEXT NOT NULL,
  model TEXT,
  device TEXT,
  compute_type TEXT,
  vocabulary_requested TEXT NOT NULL,
  vocabulary_used TEXT NOT NULL,
  corrections_applied TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  settings TEXT
);

CREATE TABLE IF NOT EXISTS asr_chunk (
  job_id TEXT NOT NULL REFERENCES job(id),
  idx INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  own_start REAL NOT NULL,
  own_end REAL NOT NULL,
  words TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS shot_run (
  job_id TEXT PRIMARY KEY REFERENCES job(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  essence_id TEXT NOT NULL REFERENCES essence(id),
  engine TEXT NOT NULL,
  engine_version TEXT,
  threshold REAL,
  min_frames INTEGER,
  shot_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  settings TEXT
);

CREATE TABLE IF NOT EXISTS show (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  hold_reason TEXT,
  held_at TEXT
);

CREATE TABLE IF NOT EXISTS item (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES show(id),
  position INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  status TEXT NOT NULL,
  span_id TEXT REFERENCES span(id),
  created_at TEXT NOT NULL,
  as_run_in TEXT,
  as_run_out TEXT,
  scheduled_at TEXT,
  schedule_state TEXT,
  schedule_note TEXT,
  schedule_claimed_at TEXT,
  as_run_in_precise TEXT,
  as_run_out_precise TEXT
);

CREATE TABLE IF NOT EXISTS version (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  span_id TEXT NOT NULL REFERENCES span(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  url TEXT,
  created_at TEXT NOT NULL,
  storage_backend TEXT,
  storage_key TEXT,
  storage_sha256 TEXT,
  UNIQUE (asset_id, span_id, kind)
);

CREATE TABLE IF NOT EXISTS cue (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES item(id),
  position INTEGER NOT NULL,
  room TEXT NOT NULL,
  command TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fail_status TEXT,
  fail_detail TEXT
);

CREATE TABLE IF NOT EXISTS purge_audit (
  id TEXT PRIMARY KEY,
  purged_at TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT,
  role TEXT,
  location TEXT,
  outcome TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS runner_heartbeat (
  name TEXT PRIMARY KEY,
  pid INTEGER,
  host TEXT,
  started_at TEXT NOT NULL,
  beat_at TEXT NOT NULL
);
"""


class _LiteConnection(sqlite3.Connection):
    """sqlite3 connection with the same transaction() the Postgres adapter has.

    Inside transaction() a commit() from the code being run is held back until the block ends, so a
    function that commits at its own end (take, advance) can be composed into one atomic step."""

    _depth = 0

    def commit(self):
        if not self._depth:
            super().commit()

    @contextmanager
    def transaction(self, lock=None):
        # `lock` is the Postgres advisory-lock name; SQLite's write lock already serializes writers.
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        if not self.in_transaction:
            self.execute("BEGIN IMMEDIATE")  # take the write lock now, so the reads below cannot go stale
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self.rollback()
            raise
        self._depth = 0
        super().commit()


class HoldError(Exception):
    pass


class NotClearedError(Exception):
    pass


@dataclass
class Asset:
    id: str
    owner: str
    retention_class: str
    legal_hold: bool
    cleared: bool
    show_name: Optional[str]
    room: Optional[str]
    camera: Optional[str]
    source_key: Optional[str] = None
    deleted_at: Optional[str] = None  # soft-delete tombstone; None = live


@dataclass
class Job:
    id: str
    asset_id: str
    type: str
    idempotency_key: str
    status: str
    attempt: int


class Store:
    def __init__(self, path: str = ":memory:", init: bool = True):
        """`path` is a SQLite path, or a postgres:// DSN (needs psycopg).

        init=False skips schema bootstrap on postgres, for callers that did it.
        """
        self.postgres = pg.is_dsn(path)
        self.path = path
        if self.postgres:
            self.conn = pg.PgConnection(path)
            self.IntegrityError = self.conn.IntegrityError
            if init:
                try:
                    self.conn.apply_schema()
                except BaseException:
                    self.conn.close()  # a failed bootstrap must not strand a connection (or pool slot)
                    raise
            return
        self.IntegrityError = sqlite3.IntegrityError
        self.conn = sqlite3.connect(path, timeout=10, factory=_LiteConnection)
        try:
            self.conn.row_factory = sqlite3.Row
            if path != ":memory:":
                self._enable_wal()
            self.conn.executescript(SCHEMA)
            self._migrate()
        except BaseException:
            self.conn.close()
            raise

    def _enable_wal(self) -> None:
        # Switching the journal mode needs the file to itself and ignores the busy timeout, so processes
        # starting together can get "database is locked". The mode persists once one of them wins.
        for attempt in range(50):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == 49:
                    raise
                time.sleep(0.05)

    def _migrate(self) -> None:
        """Bring an older SQLite file to the latest shape. See volt/migrations.py."""
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")  # two processes starting together take turns
        try:
            migrations.run(migrations.SqliteBackend(self.conn))
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    @contextmanager
    def transaction(self, lock: Optional[str] = None):
        """Run a read-modify-write as one atomic step, one at a time per `lock` name.

        Use it when a decision is made from a read and then written (next position, who is on air, is it
        free). Without it two writers can both read the same state. `lock` is a name such as
        f"show:{show_id}"; the same name serializes across threads and processes on Postgres (advisory
        lock) and SQLite serializes every writer anyway. Reentrant. Commits on exit, rolls back on error."""
        with self.conn.transaction(lock):
            yield

    def close(self) -> None:
        self.conn.close()

    def mint_asset(
        self,
        owner: str,
        retention_class: str = "show",
        show_name: Optional[str] = None,
        room: Optional[str] = None,
        camera: Optional[str] = None,
        source_key: Optional[str] = None,
    ) -> Asset:
        asset_id = _id()
        self.conn.execute(
            """INSERT INTO asset
               (id, owner, retention_class, legal_hold, cleared, show_name, room, camera, source_key, created_at)
               VALUES (?, ?, ?, FALSE, FALSE, ?, ?, ?, ?, ?)""",
            (asset_id, owner, retention_class, show_name, room, camera, source_key, _now()),
        )
        self.conn.commit()
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> Asset:
        row = self.conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return Asset(
            id=row["id"],
            owner=row["owner"],
            retention_class=row["retention_class"],
            legal_hold=bool(row["legal_hold"]),
            cleared=bool(row["cleared"]),
            show_name=row["show_name"],
            room=row["room"],
            camera=row["camera"],
            source_key=row["source_key"],
            deleted_at=row["deleted_at"],
        )

    def find_by_source(self, source_key: str) -> Optional[Asset]:
        row = self.conn.execute(
            "SELECT id FROM asset WHERE source_key = ?", (source_key,)
        ).fetchone()
        if row is None:
            return None
        return self.get_asset(row["id"])

    def mint_source(
        self,
        source_key: str,
        owner: str,
        retention_class: str = "show",
        show_name: Optional[str] = None,
        room: Optional[str] = None,
        camera: Optional[str] = None,
    ) -> Asset:
        existing = self.find_by_source(source_key)
        if existing is not None:
            return existing
        try:
            return self.mint_asset(
                owner, retention_class, show_name, room, camera, source_key=source_key
            )
        except self.IntegrityError:
            self.conn.rollback()
            winner = self.find_by_source(source_key)
            if winner is None:
                raise
            return winner

    def set_hold(self, asset_id: str, held: bool) -> None:
        self.conn.execute(
            "UPDATE asset SET legal_hold = ? WHERE id = ?",
            (bool(held), asset_id),
        )
        self.conn.commit()

    def set_cleared(self, asset_id: str, cleared: bool) -> None:
        self.conn.execute(
            "UPDATE asset SET cleared = ? WHERE id = ?",
            (bool(cleared), asset_id),
        )
        self.conn.commit()

    def add_essence(
        self,
        asset_id: str,
        role: str,
        location: str,
        open_file: bool = False,
        checksum: Optional[str] = None,
        codec: Optional[str] = None,
        frame_rate: Optional[str] = None,
        timecode_start: Optional[str] = None,
        audio_layout: Optional[str] = None,
        duration_frames: Optional[int] = None,
    ) -> str:
        self.get_asset(asset_id)
        essence_id = _id()
        self.conn.execute(
            """INSERT INTO essence
               (id, asset_id, role, location, checksum, codec, frame_rate, timecode_start, audio_layout, open,
                created_at, duration_frames)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                essence_id,
                asset_id,
                role,
                location,
                checksum,
                codec,
                frame_rate,
                timecode_start,
                audio_layout,
                bool(open_file),
                _now(),
                duration_frames,
            ),
        )
        self.conn.commit()
        return essence_id

    def wrap_essence(self, essence_id: str, checksum: str, duration_frames: Optional[int] = None) -> None:
        """Close a growing essence. `duration_frames` (when known) is recorded with the close; None leaves it."""
        with self.transaction():
            self.conn.execute(
                "UPDATE essence SET open = FALSE, checksum = ? WHERE id = ?",
                (checksum, essence_id),
            )
            if duration_frames is not None:
                self.conn.execute(
                    "UPDATE essence SET duration_frames = ? WHERE id = ?", (int(duration_frames), essence_id))

    def add_span(
        self,
        asset_id: str,
        kind: str,
        tc_in: str,
        tc_out: str,
        text: Optional[str] = None,
        label: Optional[str] = None,
        origin: Optional[str] = None,
        run_id: Optional[str] = None,
        frame_in: Optional[int] = None,
        frame_out: Optional[int] = None,
    ) -> str:
        self.get_asset(asset_id)
        span_id = _id()
        self.conn.execute(
            """INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at,
                                  label, origin, run_id, frame_in, frame_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (span_id, asset_id, kind, tc_in, tc_out, text, _now(), label, origin, run_id, frame_in, frame_out),
        )
        self.conn.commit()
        return span_id

    def enqueue(self, asset_id: str, job_type: str, idempotency_key: str) -> Job:
        """Queue a job once per (asset, type, key). A failed one is re-queued with its attempt counted.

        Safe to race: the insert and the re-queue are each one conditional statement, so two callers
        end with one row and a single attempt increment."""
        self.get_asset(asset_id)
        now = _now()
        with self.transaction():
            self.conn.execute(
                """INSERT INTO job
                   (id, asset_id, type, idempotency_key, status, attempt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'queued', 1, ?, ?)
                   ON CONFLICT (asset_id, type, idempotency_key) DO NOTHING""",
                (_id(), asset_id, job_type, idempotency_key, now, now),
            )
            self.conn.execute(
                """UPDATE job
                   SET status = 'queued', attempt = attempt + 1, error = NULL, updated_at = ?
                   WHERE asset_id = ? AND type = ? AND idempotency_key = ? AND status = 'failed'""",
                (now, asset_id, job_type, idempotency_key),
            )
            row = self.conn.execute(
                """SELECT * FROM job WHERE asset_id = ? AND type = ? AND idempotency_key = ?""",
                (asset_id, job_type, idempotency_key),
            ).fetchone()
        return _job(row)

    def get_job(self, job_id: str) -> Job:
        row = self.conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def complete_job(self, job_id: str) -> None:
        self.conn.execute(
            "UPDATE job SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
            (_now(), job_id),
        )
        self.conn.commit()

    def fail_job(self, job_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE job SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, _now(), job_id),
        )
        self.conn.commit()

    def delete_asset(self, asset_id: str) -> str:
        """Queue the delete job. The runner's handler soft-deletes (tombstones) the asset after re-checking the
        safeguards; purging is a separate, later step. See volt/retention.py and docs/specs/retention.md."""
        asset = self.get_asset(asset_id)
        if asset.legal_hold:
            raise HoldError(asset_id)
        return self.enqueue(asset_id, "delete", f"delete:{asset_id}").id

    def assert_publishable(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        if asset.legal_hold or not asset.cleared or asset.deleted_at:
            raise NotClearedError(asset_id)


def _job(row) -> Job:
    return Job(
        id=row["id"],
        asset_id=row["asset_id"],
        type=row["type"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempt=row["attempt"],
    )
