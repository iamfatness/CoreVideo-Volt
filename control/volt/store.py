"""Volt control plane. SQLite runs the contract. Postgres is the same objects."""

from __future__ import annotations

import json
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
  deleted_at TEXT,
  archived_at TEXT
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
  audio_sample_rate INTEGER,
  tier TEXT,
  archive_key TEXT,
  archive_sha256 TEXT
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
  payload TEXT,
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
  held_at TEXT,
  slot_seconds INTEGER,
  recurrence TEXT,
  recurrence_of TEXT,
  occurrence_on TEXT,
  read_wpm INTEGER
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
  as_run_out_precise TEXT,
  schedule_mode TEXT,
  schedule_offset_seconds INTEGER,
  story_status TEXT,
  script_rev INTEGER,
  approved_by TEXT,
  approved_at TEXT,
  approved_rev INTEGER,
  floated INTEGER,
  source_wire_id TEXT
);

CREATE TABLE IF NOT EXISTS story_rev (
  item_id TEXT NOT NULL,
  rev INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  actor TEXT,
  at TEXT NOT NULL,
  PRIMARY KEY (item_id, rev)
);

CREATE TABLE IF NOT EXISTS story_event (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_value TEXT,
  to_value TEXT,
  rev INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS wire_story (
  id TEXT PRIMARY KEY,
  feed TEXT NOT NULL,
  external_id TEXT NOT NULL,
  headline TEXT NOT NULL,
  body TEXT,
  byline TEXT,
  published_at TEXT,
  received_at TEXT NOT NULL,
  state TEXT NOT NULL,
  item_id TEXT,
  UNIQUE (feed, external_id)
);

CREATE TABLE IF NOT EXISTS wire_feed (
  feed TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  last_attempt_at TEXT,
  last_ok_at TEXT,
  last_error TEXT,
  next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS story_template (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  body TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_archive (
  id TEXT PRIMARY KEY,
  original_item_id TEXT,
  show_title TEXT,
  slug TEXT NOT NULL,
  script TEXT,
  story_status TEXT,
  approved_by TEXT,
  approved_at TEXT,
  aired_at TEXT,
  archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mos_sync (
  device TEXT NOT NULL,
  show_id TEXT NOT NULL,
  state TEXT NOT NULL,
  acked_hash TEXT,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (device, show_id)
);

CREATE TABLE IF NOT EXISTS mos_counter (
  device TEXT PRIMARY KEY,
  next_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mos_device (
  device TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  detail TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_status (
  device TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  observed TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_event (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  device TEXT NOT NULL,
  command TEXT NOT NULL,
  params TEXT,
  outcome TEXT NOT NULL,
  detail TEXT,
  cue_id TEXT,
  actor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_safety (
  id INTEGER PRIMARY KEY,
  safe_mode INTEGER NOT NULL,
  reason TEXT,
  set_by TEXT,
  set_at TEXT
);

CREATE TABLE IF NOT EXISTS macro (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_step (
  macro_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  position INTEGER NOT NULL,
  room TEXT NOT NULL,
  command TEXT NOT NULL,
  payload TEXT NOT NULL,
  offset_ms INTEGER NOT NULL,
  group_key TEXT,
  PRIMARY KEY (macro_id, version, position)
);

CREATE TABLE IF NOT EXISTS device_confirm (
  id TEXT PRIMARY KEY,
  cue_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  state TEXT NOT NULL
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
  withdrawn_at TEXT,
  storage_deleted_at TEXT,
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
  fail_detail TEXT,
  offset_ms INTEGER,
  preroll_ms INTEGER,
  group_key TEXT,
  due_at TEXT,
  claimed_at TEXT,
  preload_status TEXT,
  preload_detail TEXT,
  preloaded_at TEXT
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

CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS volt_user (
  name TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  disabled_at TEXT,
  email TEXT,
  display_name TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS volt_group (
  name TEXT PRIMARY KEY,
  role TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_member (
  group_name TEXT NOT NULL,
  user_name TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (group_name, user_name)
);

CREATE TABLE IF NOT EXISTS session (
  id_hash TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS session_by_user ON session (user_name);

CREATE TABLE IF NOT EXISTS field_def (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  type TEXT NOT NULL,
  show_name TEXT,
  required INTEGER NOT NULL DEFAULT 0,
  multi INTEGER NOT NULL DEFAULT 0,
  position INTEGER NOT NULL,
  retired_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_term (
  field_id TEXT NOT NULL REFERENCES field_def(id),
  term TEXT NOT NULL,
  position INTEGER NOT NULL,
  retired_at TEXT,
  PRIMARY KEY (field_id, term)
);

CREATE TABLE IF NOT EXISTS asset_meta_value (
  asset_id TEXT NOT NULL REFERENCES asset(id),
  field_id TEXT NOT NULL REFERENCES field_def(id),
  ord INTEGER NOT NULL,
  v_text TEXT,
  v_num REAL,
  v_date TEXT,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, field_id, ord)
);

CREATE TABLE IF NOT EXISTS search_doc (
  seq INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  kind TEXT NOT NULL,
  ref_id TEXT,
  tc_in TEXT,
  tc_out TEXT,
  frame_in INTEGER,
  frame_out INTEGER,
  body TEXT NOT NULL,
  weight REAL NOT NULL,
  status TEXT,
  person_id TEXT,
  confidence REAL,
  value_norm TEXT,
  created_at TEXT NOT NULL,
  tsv TEXT
);

CREATE TABLE IF NOT EXISTS saved_search (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner TEXT NOT NULL,
  shared INTEGER NOT NULL DEFAULT 0,
  query TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bin (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner TEXT NOT NULL,
  shared INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bin_member (
  bin_id TEXT NOT NULL REFERENCES bin(id),
  asset_id TEXT NOT NULL REFERENCES asset(id),
  span_id TEXT,
  position INTEGER NOT NULL,
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_flow (
  state TEXT PRIMARY KEY,
  nonce TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  provider TEXT NOT NULL,
  next TEXT NOT NULL,
  binder_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity (
  issuer TEXT NOT NULL,
  sub TEXT NOT NULL,
  user_name TEXT NOT NULL,
  email TEXT,
  display_name TEXT,
  last_login_at TEXT NOT NULL,
  PRIMARY KEY (issuer, sub)
);

CREATE TABLE IF NOT EXISTS oidc_group_map (
  provider TEXT NOT NULL,
  claim_value TEXT NOT NULL,
  group_name TEXT NOT NULL,
  PRIMARY KEY (provider, claim_value, group_name)
);

CREATE TABLE IF NOT EXISTS show_acl (
  show_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  level TEXT NOT NULL,
  PRIMARY KEY (show_id, subject_kind, subject)
);

CREATE INDEX IF NOT EXISTS show_acl_subject ON show_acl (subject_kind, subject);

CREATE TABLE IF NOT EXISTS lease (
  resource_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  holder TEXT NOT NULL,
  client_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  renewed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (resource_kind, resource_id)
);

CREATE TABLE IF NOT EXISTS presence (
  user_name TEXT NOT NULL,
  client_id TEXT NOT NULL,
  show_id TEXT,
  view TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  PRIMARY KEY (user_name, client_id)
);

CREATE INDEX IF NOT EXISTS presence_by_show ON presence (show_id);

CREATE TABLE IF NOT EXISTS comment (
  id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  show_id TEXT,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  parent_id TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS comment_mention (
  comment_id TEXT NOT NULL,
  user_name TEXT NOT NULL,
  PRIMARY KEY (comment_id, user_name)
);

CREATE TABLE IF NOT EXISTS notification (
  id TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  show_id TEXT,
  target_kind TEXT,
  target_id TEXT,
  comment_id TEXT,
  actor TEXT,
  created_at TEXT NOT NULL,
  read_at TEXT
);

CREATE INDEX IF NOT EXISTS comment_by_target ON comment (target_kind, target_id, created_at);
CREATE INDEX IF NOT EXISTS comment_by_author ON comment (author, created_at);
CREATE INDEX IF NOT EXISTS comment_by_show ON comment (show_id);
CREATE INDEX IF NOT EXISTS notification_by_user ON notification (user_name, read_at, created_at);
CREATE INDEX IF NOT EXISTS notification_by_comment ON notification (comment_id);
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


class ArchivedError(Exception):
    """The asset's hi-res is in the archive (or on its way back): it cannot air, publish, be proxied or be exported until it is restored."""


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
    payload: Optional[str]


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

    def enqueue(self, asset_id: str, job_type: str, idempotency_key: str, payload: Optional[dict] = None) -> Job:
        """Queue a job once per (asset, type, key). A failed one is re-queued with its attempt counted.

        Safe to race: the insert and the re-queue are each one conditional statement, so two callers
        end with one row and a single attempt increment. Conflict does not overwrite payload."""
        self.get_asset(asset_id)
        now = _now()
        payload_text = json.dumps(payload) if payload is not None else None
        with self.transaction():
            self.conn.execute(
                """INSERT INTO job
                   (id, asset_id, type, idempotency_key, status, attempt, created_at, updated_at, payload)
                   VALUES (?, ?, ?, ?, 'queued', 1, ?, ?, ?)
                   ON CONFLICT (asset_id, type, idempotency_key) DO NOTHING""",
                (_id(), asset_id, job_type, idempotency_key, now, now, payload_text),
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
        if self.conn.execute("SELECT 1 FROM essence WHERE asset_id = ? AND role = 'hi-res' AND tier IN ('archived', 'restoring')",
                             (asset_id,)).fetchone():
            raise ArchivedError(asset_id)

    def append_audit(
        self,
        actor: str,
        action: str,
        target_kind: str,
        target_id: str,
        detail: Optional[str] = None,
    ) -> str:
        """Append one operator-audit row. The table is insert-only; nothing here updates or deletes."""
        row_id = _id()
        self.conn.execute(
            """INSERT INTO audit (id, at, actor, action, target_kind, target_id, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row_id, _now_precise(), actor, action, target_kind, target_id, detail),
        )
        self.conn.commit()
        return row_id

    def list_audit_page(self, actor=None, action=None, kind=None, target=None, since=None, until=None, before=None, limit: int = 100):
        """Filtered audit rows, newest first, in pages. Returns (rows, next_cursor); the cursor is the `at|id` of the last row and the
        next call passes it as `before`. Ordering is (at, id) so rows with equal times neither repeat nor go missing. Raises ValueError
        for a limit outside 1..500 or a cursor that is not one of ours."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1 to 500")
        where, params = [], []
        for column, value in (("actor", actor), ("action", action), ("target_kind", kind), ("target_id", target)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            where.append("at >= ?")
            params.append(since)
        if until is not None:
            where.append("at <= ?")
            params.append(until)
        if before is not None:
            at, sep, row_id = before.rpartition("|")
            if not sep or not at or not row_id:
                raise ValueError("before is not a cursor")
            where.append("(at < ? OR (at = ? AND id < ?))")
            params += [at, at, row_id]
        sql = ("SELECT id, at, actor, action, target_kind, target_id, detail FROM audit"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC, id DESC LIMIT ?")
        fetched = self.conn.execute(sql, params + [limit + 1]).fetchall()
        rows = []
        for row in fetched[:limit]:
            at = row["at"]
            if not isinstance(at, str):
                at = at.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(at, "strftime") else str(at)
            rows.append({"id": row["id"], "at": at, "actor": row["actor"], "action": row["action"],
                         "target_kind": row["target_kind"], "target_id": row["target_id"], "detail": row["detail"]})
        cursor = f"{rows[-1]['at']}|{rows[-1]['id']}" if len(fetched) > limit and rows else None
        return rows, cursor

    def list_audit(self, target_id: str, limit: int = 200) -> list:
        """Newest-first rows for one target_id, capped at `limit` (the HTTP route uses 200)."""
        order = "at DESC, id DESC" if self.postgres else "rowid DESC"
        rows = self.conn.execute(
            f"""SELECT id, at, actor, action, target_kind, target_id, detail
                FROM audit WHERE target_id = ? ORDER BY {order} LIMIT ?""",
            (target_id, limit),
        ).fetchall()
        out = []
        for row in rows:
            at = row["at"]
            if not isinstance(at, str):
                at = at.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(at, "strftime") else str(at)
            out.append({
                "id": row["id"],
                "at": at,
                "actor": row["actor"],
                "action": row["action"],
                "target_kind": row["target_kind"],
                "target_id": row["target_id"],
                "detail": row["detail"],
            })
        return out


def _job(row) -> Job:
    return Job(
        id=row["id"],
        asset_id=row["asset_id"],
        type=row["type"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempt=row["attempt"],
        payload=row["payload"],
    )
