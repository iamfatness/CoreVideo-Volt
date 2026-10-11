"""Publish. One span, three versions, no second asset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from volt import deliverables, storage
from volt.deliverables import DeliverableError
from volt.storage import Backend, LocalBackend, StorageError, StoredObject
from volt.store import Store, _id, _now

KINDS = ("house", "ott", "social")

# How long a writer's claim on one version (the job row `publish:<span>:<kind>`) holds before another writer may
# take it over. A render has no heartbeat, so this is longer than any cut should take; a writer that dies
# mid-render blocks that one version for at most this long. Runner.recover_expired also re-queues such a row.
CLAIM_LEASE = 1800.0


@dataclass
class Version:
    """status is done, failed, queued (placeholder path), or busy: another writer holds this version's claim
    and is rendering it, so this call did not render or upload it. Busy is never stored on the version row."""
    id: str
    kind: str
    status: str
    url: str | None


def _stamp(offset: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_version(store: Store, asset_id: str, span_id: str, kind: str) -> tuple[str, str, str | None]:
    """The (id, status, url) of this span's version of `kind`, created queued if it is not there yet.
    One conditional insert, so two writers racing to create it end with the one row."""
    store.conn.execute(
        """INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at)
           VALUES (?, ?, ?, ?, 'queued', NULL, ?)
           ON CONFLICT (asset_id, span_id, kind) DO NOTHING""",
        (_id(), asset_id, span_id, kind, _now()))
    row = store.conn.execute(
        "SELECT id, status, url FROM version WHERE asset_id = ? AND span_id = ? AND kind = ?",
        (asset_id, span_id, kind)).fetchone()
    return row["id"], row["status"], row["url"]


def _claim(store: Store, job_id: str, owner: str, lease: float) -> bool:
    """Take the version's job row for this writer: one conditional UPDATE, so of two writers exactly one gets
    rowcount 1. A row another writer holds with a live lease is not taken; a lapsed one is."""
    now = _stamp()
    cur = store.conn.execute(
        """UPDATE job SET status = 'running', claimed_by = ?, lease_expires_at = ?, updated_at = ?
           WHERE id = ? AND (status != 'running' OR lease_expires_at IS NULL OR lease_expires_at < ?)""",
        (owner, _stamp(lease), now, job_id, now))
    return cur.rowcount == 1


def _settle(store: Store, job_id: str, owner: str, status: str, error: str | None) -> bool:
    """Record the outcome on the job row, only if this writer still holds the claim."""
    cur = store.conn.execute(
        """UPDATE job SET status = ?, error = ?, claimed_by = NULL, lease_expires_at = NULL, updated_at = ?
           WHERE id = ? AND claimed_by = ? AND status = 'running'""",
        (status, error, _stamp(), job_id, owner))
    return cur.rowcount == 1


def publish(
    store: Store,
    span_id: str,
    fail: str | None = None,
    renderer: Optional[Callable[[Store, object, str], "str | StoredObject"]] = None,
    lease: float = CLAIM_LEASE,
) -> list[Version]:
    """Publish a span's three versions.

    With a renderer, each version is claimed before it is rendered (see `_claim`): a second writer that finds
    it claimed does not render or upload it and gets that version back as status "busy", or "done" if the
    first writer finished meanwhile. Without one (the placeholder path) there is nothing to protect."""
    span = store.conn.execute(
        "SELECT id, asset_id, tc_in, tc_out FROM span WHERE id = ?", (span_id,)
    ).fetchone()
    if span is None:
        raise KeyError(span_id)
    store.assert_publishable(span["asset_id"])
    asset_id = span["asset_id"]
    owner = f"publish-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    versions = []
    for kind in KINDS:
        key = f"publish:{span_id}:{kind}"
        if renderer is not None:
            version_id, status, url = _ensure_version(store, asset_id, span_id, kind)
            store.conn.commit()
            if status == "done" and url:
                versions.append(Version(version_id, kind, "done", url))
                continue
            job = store.enqueue(asset_id, "publish", key)
            with store.transaction():
                claimed = _claim(store, job.id, owner, lease)
            if not claimed:  # another writer is rendering this version; do not render or upload it too
                _, status, url = _ensure_version(store, asset_id, span_id, kind)
                store.conn.commit()
                done = status == "done" and bool(url)
                versions.append(Version(version_id, kind, "done" if done else "busy", url if done else None))
                continue
            # A writer that finished between our read and our claim leaves a done version behind.
            _, status, url = _ensure_version(store, asset_id, span_id, kind)
            store.conn.commit()  # no write lock held across the render
            if status == "done" and url:
                with store.transaction():
                    _settle(store, job.id, owner, "done", None)
                versions.append(Version(version_id, kind, "done", url))
                continue
        else:
            job = store.enqueue(asset_id, "publish", key)
            version_id, _, _ = _ensure_version(store, asset_id, span_id, kind)
        error = "worker failed" if fail == kind else None
        url = None
        stored: StoredObject | None = None
        if error is None and renderer is not None:
            try:
                url = renderer(store, span, kind)
            except (DeliverableError, StorageError) as exc:
                error = str(exc) or "render failed"
            except BaseException as exc:
                # Not a render failure we know how to report: give the claim back (failed, so a retry
                # re-queues it) rather than hold it until the lease lapses, then let the error propagate.
                store.conn.rollback()
                with store.transaction():
                    _settle(store, job.id, owner, "failed", str(exc) or exc.__class__.__name__)
                raise
            if isinstance(url, StoredObject):
                stored, url = url, url.url
        # The outcome of the job and of the version is one commit, and only the writer holding the claim
        # records either: one that lost its lease (taken over after CLAIM_LEASE) records nothing.
        with store.transaction():
            if renderer is None:
                store.conn.execute(
                    "UPDATE job SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    ("failed" if error is not None else "done", error, _stamp(), job.id))
                held = True
            else:
                held = _settle(store, job.id, owner, "failed" if error is not None else "done", error)
            if held:
                if error is not None:
                    url = None
                    status = "failed"
                    store.conn.execute("UPDATE version SET status = 'failed' WHERE id = ?", (version_id,))
                else:
                    url = url or f"volt://{kind}/{asset_id}/{span_id}"
                    status = "done"
                    store.conn.execute(
                        """UPDATE version SET status = 'done', url = ?, storage_backend = ?, storage_key = ?,
                           storage_sha256 = ? WHERE id = ?""",
                        (url, stored and stored.backend, stored and stored.key, stored and stored.sha256,
                         version_id))
        versions.append(Version(version_id, kind, status, url) if held else Version(version_id, kind, "busy", None))
    store.conn.commit()
    return versions


def publish_files(
    store: Store,
    span_id: str,
    out_dir: Optional[Path] = None,
    backend: Optional[Backend] = None,
    scratch: Optional[Path] = None,
) -> list[Version]:
    """Publish with real cut files. Done versions are not re-rendered or re-uploaded.

    `backend` defaults to what VOLT_STORAGE says, and to local files under `out_dir` when that is unset. With
    the local backend a cut is written straight to its place under `out_dir` (the URL is `file://`, as always).
    With any other backend the cut is rendered into a scratch directory, uploaded, verified, and only then
    deleted from scratch; an upload or verification failure fails that version with the reason and keeps the
    scratch file out of the way of the next attempt (a retry renders it again over the same path).

    Two writers publishing one span do not both render it: each version is claimed first, and a writer that
    finds it claimed leaves it (status "busy" in what it returns). The scratch path is therefore never shared
    by two live renders, so neither deletes the other's file."""
    backend = backend if backend is not None else storage.from_env(local_root=out_dir)
    is_local = isinstance(backend, LocalBackend)
    if is_local:
        work = Path(backend.root)
    else:
        work = Path(scratch) if scratch else (
            Path(out_dir) / ".scratch" if out_dir else Path(tempfile.gettempdir()) / "volt-publish-scratch")

    def render_and_store(st, span, kind):
        uri = deliverables.render(st, span, kind, work)
        path = Path(url2pathname(urlparse(uri).path))
        obj = backend.put_file(path, f"{span['asset_id']}/{span['id']}-{kind}.mp4")
        if not is_local:  # verified: the copy in the bucket is the one we cut
            path.unlink(missing_ok=True)
        return obj

    return publish(store, span_id, renderer=render_and_store)


def refresh_url(store: Store, version_id: str, backend: Optional[Backend] = None,
                expires: Optional[int] = None) -> str:
    """Make a new URL for a stored version (a presigned URL expires) and write it back. Needs the same
    bucket and prefix the version was stored under; a mismatch is an error rather than a wrong URL."""
    row = store.conn.execute(
        "SELECT url, storage_backend, storage_key FROM version WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise KeyError(version_id)
    if not row["storage_backend"] or not row["storage_key"]:
        return row["url"]  # a volt:// placeholder or a version from before the storage seam
    if row["storage_backend"] == "local":
        return row["url"]  # file:// URLs do not expire
    backend = backend if backend is not None else storage.from_env()
    if backend.id != row["storage_backend"]:
        raise StorageError(f"this version is stored in {row['storage_backend']} but the configured "
                           f"storage is {backend.id}")
    url = backend.url(row["storage_key"], expires)
    store.conn.execute("UPDATE version SET url = ? WHERE id = ?", (url, version_id))
    store.conn.commit()
    return url
