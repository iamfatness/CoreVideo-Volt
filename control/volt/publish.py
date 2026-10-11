"""Publish. One span, three versions, no second asset."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from volt import deliverables, storage
from volt.deliverables import DeliverableError
from volt.storage import Backend, LocalBackend, StorageError, StoredObject
from volt.store import Store, _id, _now

KINDS = ("house", "ott", "social")


@dataclass
class Version:
    id: str
    kind: str
    status: str
    url: str | None


def publish(
    store: Store,
    span_id: str,
    fail: str | None = None,
    renderer: Optional[Callable[[Store, object, str], "str | StoredObject"]] = None,
) -> list[Version]:
    span = store.conn.execute(
        "SELECT id, asset_id, tc_in, tc_out FROM span WHERE id = ?", (span_id,)
    ).fetchone()
    if span is None:
        raise KeyError(span_id)
    store.assert_publishable(span["asset_id"])
    versions = []
    for kind in KINDS:
        existing = store.conn.execute(
            "SELECT id, status, url FROM version WHERE asset_id = ? AND span_id = ? AND kind = ?",
            (span["asset_id"], span_id, kind),
        ).fetchone()
        if renderer is not None and existing is not None and existing["status"] == "done" and existing["url"]:
            versions.append(Version(existing["id"], kind, "done", existing["url"]))
            continue
        job = store.enqueue(span["asset_id"], "publish", f"publish:{span_id}:{kind}")
        if existing is None:
            version_id = _id()
            store.conn.execute(
                """INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at)
                   VALUES (?, ?, ?, ?, 'queued', NULL, ?)""",
                (version_id, span["asset_id"], span_id, kind, _now()),
            )
        else:
            version_id = existing["id"]
        error = "worker failed" if fail == kind else None
        url = None
        stored: StoredObject | None = None
        if error is None and renderer is not None:
            try:
                url = renderer(store, span, kind)
            except (DeliverableError, StorageError) as exc:
                error = str(exc) or "render failed"
            if isinstance(url, StoredObject):
                stored, url = url, url.url
        if error is not None:
            store.fail_job(job.id, error)
            store.conn.execute(
                "UPDATE version SET status = 'failed' WHERE id = ?", (version_id,)
            )
            url = None
            status = "failed"
        else:
            url = url or f"volt://{kind}/{span['asset_id']}/{span_id}"
            store.complete_job(job.id)
            store.conn.execute(
                """UPDATE version SET status = 'done', url = ?, storage_backend = ?, storage_key = ?,
                   storage_sha256 = ? WHERE id = ?""",
                (url, stored and stored.backend, stored and stored.key, stored and stored.sha256, version_id),
            )
            status = "done"
        versions.append(Version(version_id, kind, status, url))
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
    scratch file out of the way of the next attempt (a retry renders it again over the same path)."""
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
