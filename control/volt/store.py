"""Volt control plane. SQLite runs the contract. Postgres is the same objects."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
  created_at TEXT NOT NULL
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
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS span (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  kind TEXT NOT NULL,
  tc_in TEXT NOT NULL,
  tc_out TEXT NOT NULL,
  text TEXT,
  created_at TEXT NOT NULL
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
  UNIQUE (asset_id, type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS show (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES show(id),
  position INTEGER NOT NULL,
  slug TEXT NOT NULL,
  script TEXT,
  status TEXT NOT NULL,
  span_id TEXT REFERENCES span(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES asset(id),
  span_id TEXT NOT NULL REFERENCES span(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  url TEXT,
  created_at TEXT NOT NULL,
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
  created_at TEXT NOT NULL
);
"""


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


@dataclass
class Job:
    id: str
    asset_id: str
    type: str
    idempotency_key: str
    status: str
    attempt: int


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def mint_asset(
        self,
        owner: str,
        retention_class: str = "show",
        show_name: Optional[str] = None,
        room: Optional[str] = None,
        camera: Optional[str] = None,
    ) -> Asset:
        asset_id = _id()
        self.conn.execute(
            """INSERT INTO asset
               (id, owner, retention_class, legal_hold, cleared, show_name, room, camera, source_key, created_at)
               VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, ?)""",
            (asset_id, owner, retention_class, show_name, room, camera, None, _now()),
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
        asset = self.mint_asset(owner, retention_class, show_name, room, camera)
        self.conn.execute(
            "UPDATE asset SET source_key = ? WHERE id = ?",
            (source_key, asset.id),
        )
        self.conn.commit()
        return self.get_asset(asset.id)

    def set_hold(self, asset_id: str, held: bool) -> None:
        self.conn.execute(
            "UPDATE asset SET legal_hold = ? WHERE id = ?",
            (1 if held else 0, asset_id),
        )
        self.conn.commit()

    def set_cleared(self, asset_id: str, cleared: bool) -> None:
        self.conn.execute(
            "UPDATE asset SET cleared = ? WHERE id = ?",
            (1 if cleared else 0, asset_id),
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
    ) -> str:
        self.get_asset(asset_id)
        essence_id = _id()
        self.conn.execute(
            """INSERT INTO essence
               (id, asset_id, role, location, checksum, codec, frame_rate, timecode_start, audio_layout, open, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                1 if open_file else 0,
                _now(),
            ),
        )
        self.conn.commit()
        return essence_id

    def wrap_essence(self, essence_id: str, checksum: str) -> None:
        self.conn.execute(
            "UPDATE essence SET open = 0, checksum = ? WHERE id = ?",
            (checksum, essence_id),
        )
        self.conn.commit()

    def add_span(
        self,
        asset_id: str,
        kind: str,
        tc_in: str,
        tc_out: str,
        text: Optional[str] = None,
    ) -> str:
        self.get_asset(asset_id)
        span_id = _id()
        self.conn.execute(
            """INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (span_id, asset_id, kind, tc_in, tc_out, text, _now()),
        )
        self.conn.commit()
        return span_id

    def enqueue(self, asset_id: str, job_type: str, idempotency_key: str) -> Job:
        self.get_asset(asset_id)
        existing = self.conn.execute(
            """SELECT * FROM job
               WHERE asset_id = ? AND type = ? AND idempotency_key = ?""",
            (asset_id, job_type, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "failed":
                self.conn.execute(
                    """UPDATE job
                       SET status = 'queued', attempt = attempt + 1, error = NULL, updated_at = ?
                       WHERE id = ?""",
                    (_now(), existing["id"]),
                )
                self.conn.commit()
                existing = self.conn.execute(
                    "SELECT * FROM job WHERE id = ?", (existing["id"],)
                ).fetchone()
            return _job(existing)
        job_id = _id()
        now = _now()
        self.conn.execute(
            """INSERT INTO job
               (id, asset_id, type, idempotency_key, status, attempt, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', 1, ?, ?)""",
            (job_id, asset_id, job_type, idempotency_key, now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
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
        asset = self.get_asset(asset_id)
        if asset.legal_hold:
            raise HoldError(asset_id)
        return self.enqueue(asset_id, "delete", f"delete:{asset_id}").id

    def assert_publishable(self, asset_id: str) -> None:
        asset = self.get_asset(asset_id)
        if asset.legal_hold or not asset.cleared:
            raise NotClearedError(asset_id)


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        asset_id=row["asset_id"],
        type=row["type"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempt=row["attempt"],
    )
