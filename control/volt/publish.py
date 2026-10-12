"""Publish. One span, three versions, no second asset."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import url2pathname

from volt import deliverables, storage
from volt.deliverables import DeliverableError
from volt.storage import Backend, LocalBackend, StorageError, StoredObject
from volt.store import Store, _id, _now

KINDS = ("house", "ott", "social")

# How long a writer's claim on one version (the job row `publish:<span>:<kind>`) holds before another writer may
# take it over. A heartbeat renews it while the renderer runs, so a long cut keeps its claim. A writer that
# dies mid-render blocks that one version until the lease lapses. Runner.recover_expired also re-queues it.
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


class _Heartbeat(threading.Thread):
    """Renew this writer's claim while a render runs. Own connection: the render holds the caller's."""

    def __init__(self, store: Store, job_id: str, owner: str, lease: float):
        super().__init__(daemon=True)
        self.store, self.job_id, self.owner, self.lease = store, job_id, owner, lease
        self._done = threading.Event()

    def run(self) -> None:
        if not self.store.path or self.store.path == ":memory:":
            return
        interval = max(self.lease / 3, 0.05)
        other = Store(self.store.path, init=False)
        try:
            while not self._done.wait(interval):
                cur = other.conn.execute(
                    """UPDATE job SET lease_expires_at = ? WHERE id = ? AND claimed_by = ?
                       AND status = 'running'""",
                    (_stamp(self.lease), self.job_id, self.owner))
                other.conn.commit()
                if cur.rowcount != 1:
                    break
        finally:
            other.close()

    def stop(self) -> None:
        self._done.set()
        self.join(timeout=5)


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
            beat = _Heartbeat(store, job.id, owner, lease)
            beat.start()
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
            finally:
                beat.stop()
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
    return _refresh(store, version_id, backend, expires)[0]


def _refresh(store: Store, version_id: str, backend: Optional[Backend], expires: Optional[int]):
    """(url, won). The write is compare-and-set on the URL that was read, so when two refreshers race for one
    version exactly one changes the row (`won`); the other gets the winner's URL back and `won` is False."""
    row = store.conn.execute(
        "SELECT url, storage_backend, storage_key FROM version WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise KeyError(version_id)
    if not row["storage_backend"] or not row["storage_key"]:
        return row["url"], False  # a volt:// placeholder or a version from before the storage seam
    if row["storage_backend"] == "local":
        return row["url"], False  # file:// URLs do not expire
    backend = backend if backend is not None else storage.from_env()
    if backend.id != row["storage_backend"]:
        raise StorageError(f"this version is stored in {row['storage_backend']} but the configured "
                           f"storage is {backend.id}")
    url = backend.url(row["storage_key"], expires)
    cur = store.conn.execute("UPDATE version SET url = ? WHERE id = ? AND url = ?",
                             (url, version_id, row["url"]))
    store.conn.commit()
    if cur.rowcount == 1:
        return url, True
    current = store.conn.execute("SELECT url FROM version WHERE id = ?", (version_id,)).fetchone()
    return (current["url"] if current else url), False


def url_expiry(url: Optional[str]) -> Optional[datetime]:
    """When a presigned URL stops working, read from the URL itself (the issue time is not stored).

    SigV4: `X-Amz-Date` (YYYYMMDDTHHMMSSZ) plus `X-Amz-Expires` seconds. SigV2: `Expires` is an absolute epoch.
    None when the URL carries neither (a public or placeholder URL, or a shape this does not know)."""
    if not url:
        return None
    query = parse_qs(urlparse(url).query)

    def one(name):
        got = query.get(name)
        return got[0] if got else None

    try:
        issued, lifetime = one("X-Amz-Date"), one("X-Amz-Expires")
        if issued and lifetime:
            start = datetime.strptime(issued, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return start + timedelta(seconds=int(lifetime))
        absolute = one("Expires")
        if absolute:
            return datetime.fromtimestamp(int(absolute), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


@dataclass
class RefreshReport:
    refreshed: list = field(default_factory=list)   # version ids given a new URL
    skipped: int = 0                                # fresh enough, or a different bucket/prefix, or no expiry
    failed: list = field(default_factory=list)      # (version id, reason); the pass went on past each


def refresh_expiring(store: Store, backend: Optional[Backend] = None, within_seconds: int = 86400,
                     expires: Optional[int] = None, now: Optional[datetime] = None) -> RefreshReport:
    """Re-sign every done version whose presigned URL expires within `within_seconds` (or already has).

    Only versions stored under `backend` (same bucket and prefix) are looked at. Nothing happens for the local
    backend: file:// URLs do not expire. With an S3 public URL base the URLs never expire either, so nothing is
    re-signed; instead any done version still holding an older (presigned) URL is moved to its stable public URL
    once. A URL whose expiry cannot be read is counted as skipped, not guessed at. One bad version is recorded in
    `failed` and the loop goes on. Safe from several processes: a version is refreshed by exactly one."""
    report = RefreshReport()
    backend = backend if backend is not None else storage.from_env()
    if isinstance(backend, LocalBackend):
        return report
    public = bool(getattr(getattr(backend, "config", None), "public_url_base", None))
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(seconds=within_seconds)
    rows = store.conn.execute(
        """SELECT id, url, storage_key FROM version
           WHERE status = 'done' AND storage_backend = ? AND storage_key IS NOT NULL ORDER BY id""",
        (backend.id,)).fetchall()
    for row in rows:
        try:
            if public:
                if row["url"] == backend.url(row["storage_key"]):
                    report.skipped += 1
                    continue
            else:
                expiry = url_expiry(row["url"])
                if expiry is None or expiry > horizon:
                    report.skipped += 1
                    continue
            _, won = _refresh(store, row["id"], backend, expires)
            if won:
                report.refreshed.append(row["id"])
            else:
                report.skipped += 1  # another refresher got there first
        except Exception as exc:  # noqa: BLE001 - one bad version must not stop the pass
            report.failed.append((row["id"], str(exc) or type(exc).__name__))
    return report


def main(argv: list[str]) -> int:
    """`python -m volt.publish refresh-urls DB --within SECONDS`: one pass, for cron. Exit 1 if any failed."""
    import argparse
    import os
    ap = argparse.ArgumentParser(prog="python -m volt.publish", description="Publish maintenance.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh-urls", help="re-sign presigned URLs that expire soon (storage from VOLT_STORAGE)")
    r.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"), help="SQLite path or postgres:// DSN (or VOLT_DB)")
    r.add_argument("--within", type=int, default=86400, help="refresh URLs expiring within this many seconds (default 86400)")
    r.add_argument("--expires", type=int, default=None, help="lifetime of the new URLs in seconds (default VOLT_S3_URL_EXPIRES)")
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required")
    store = Store(args.db)
    try:
        report = refresh_expiring(store, within_seconds=args.within, expires=args.expires)
    finally:
        store.close()
    print(f"refreshed={len(report.refreshed)} skipped={report.skipped} failed={len(report.failed)}")
    for version_id, reason in report.failed:
        print(f"failed {version_id}: {reason}", file=sys.stderr)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
