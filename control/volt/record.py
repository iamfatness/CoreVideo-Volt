"""Record start and partial proxy. The ID exists before wrap."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from volt import shots
from volt.ingest import sha256
from volt.store import Store


class RecordError(Exception):
    pass


class ProxyBusy(RecordError):
    """Another build holds this asset. The caller waits; it does not encode."""


@dataclass
class Record:
    asset_id: str
    essence_id: str
    minted: bool


def record_start(
    store: Store,
    source_key: str,
    location: Path,
    owner: str,
    camera: str | None = None,
    show_name: str | None = None,
) -> Record:
    existing = store.find_by_source(source_key)
    asset = store.mint_source(
        source_key, owner=owner, camera=camera, show_name=show_name
    )
    row = store.conn.execute(
        "SELECT id FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset.id,),
    ).fetchone()
    if row is None:
        essence_id = store.add_essence(
            asset.id, role="hi-res", location=str(location), open_file=True
        )
    else:
        essence_id = row["id"]
    store.enqueue(asset.id, "proxy", f"proxy:{source_key}")
    return Record(asset.id, essence_id, existing is None)


def build_proxy(store: Store, asset_id: str, proxy_dir: Path, owned: bool = False) -> str:
    """owned: a runner holds the job row and records the outcome, so this does not touch jobs."""
    source = store.conn.execute(
        "SELECT id, location, open FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    if source is None:
        raise RecordError(f"no hi-res for {asset_id}")
    from volt.archive import require_online
    require_online(store, asset_id)   # an archived hi-res has no file to read: restore it first
    proxy_dir.mkdir(parents=True, exist_ok=True)
    dest = proxy_dir / f"{asset_id}.mp4"
    current = store.conn.execute(
        "SELECT id, open FROM essence WHERE asset_id = ? AND role = 'proxy'",
        (asset_id,),
    ).fetchone()
    if (
        current is not None
        and not source["open"]
        and dest.exists()
        and dest.stat().st_mtime >= Path(source["location"]).stat().st_mtime
    ):
        return current["id"]
    started_open = bool(source["open"])
    lock = _proxy_lock(proxy_dir, asset_id)
    # The record-start proxy and the final proxy are separate jobs. One holds the lock for the encode;
    # the other waits. ffmpeg writes a private name and the finished file is renamed over `dest`.
    temp = dest.with_name(f"{asset_id}.{uuid.uuid4().hex[:12]}.part.mp4")
    try:
        probe = subprocess.run(
            [
                "ffmpeg", "-y", "-i", source["location"],
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-an", str(temp),
            ],
            capture_output=True,
            text=True,
        )
        built = probe.returncode == 0 and temp.exists()
        if built and _still_current(store, asset_id, started_open):
            os.replace(temp, dest)
        elif built:
            temp.unlink(missing_ok=True)
            existing = store.conn.execute(
                "SELECT id FROM essence WHERE asset_id = ? AND role = 'proxy'",
                (asset_id,),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            raise RecordError("source wrapped during proxy build")
    finally:
        temp.unlink(missing_ok=True)
        lock.release()
    if not built:
        job_id = "" if owned else _proxy_job(store, asset_id)
        if job_id:
            store.fail_job(job_id, probe.stderr[-400:] or "proxy failed")
        raise RecordError((probe.stderr[-400:].strip() if owned else "") or "proxy failed")
    with store.transaction(f"proxy-essence:{asset_id}"):
        existing = store.conn.execute(
            "SELECT id FROM essence WHERE asset_id = ? AND role = 'proxy'",
            (asset_id,),
        ).fetchone()
        if existing is None:
            essence_id = store.add_essence(
                asset_id,
                role="proxy",
                location=str(dest),
                open_file=bool(source["open"]),
            )
        else:
            essence_id = existing["id"]
    return essence_id


def _proxy_lock(proxy_dir: Path, asset_id: str):
    """One encode per asset. A lock older than the publish lease is a dead builder and is taken."""
    path = proxy_dir / f"{asset_id}.proxy.lock"
    stale = 1800.0
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            return _Lock(fd, path)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale:
                path.unlink(missing_ok=True)
                continue
            raise ProxyBusy(asset_id)
    raise ProxyBusy(asset_id)


class _Lock:
    def __init__(self, fd: int, path: Path):
        self.fd, self.path = fd, path

    def release(self) -> None:
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def _still_current(store: Store, asset_id: str, started_open: bool) -> bool:
    """An open-file build that finishes after wrap does not replace the final proxy."""
    if not started_open:
        return True
    row = store.conn.execute(
        "SELECT open FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    return row is not None and bool(row["open"])


def wrap(store: Store, asset_id: str, settle: float = 0.25) -> str:
    source = store.conn.execute(
        "SELECT id, location, open, checksum FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    if source is None:
        raise RecordError(f"no hi-res for {asset_id}")
    if not source["open"] and source["checksum"]:
        chain_after_wrap(store, asset_id)  # idempotent; heals a crash between closing the file and queueing
        return source["checksum"]
    path = Path(source["location"])
    size = path.stat().st_size
    time.sleep(settle)
    if path.stat().st_size != size:
        raise RecordError(f"{path.name} is still growing")
    digest = sha256(Path(source["location"]))
    rate = shots.record_media_facts(store, source["id"], path)  # the closed file is finally probeable
    store.wrap_essence(source["id"], digest, duration_frames=shots.probe_duration_frames(path, rate or None))
    store.conn.execute(
        "UPDATE essence SET open = FALSE WHERE asset_id = ? AND role = 'proxy'",
        (asset_id,),
    )
    store.conn.commit()
    job_id = _proxy_job(store, asset_id)
    if job_id:
        store.complete_job(job_id)
    chain_after_wrap(store, asset_id)
    return digest


def chain_after_wrap(store: Store, asset_id: str) -> None:
    """Queue the follow-on work for a closed asset: the final proxy and the transcript.

    Both read the closed hi-res and neither needs the other, so they are queued together rather than
    proxy-then-asr. Keys are per asset, so calling this twice queues nothing new. The asr job waits
    (without using an attempt) until the asset is cleared."""
    store.enqueue(asset_id, "proxy", f"proxy-final:{asset_id}")
    store.enqueue(asset_id, "asr", f"asr:{asset_id}")


def _proxy_job(store: Store, asset_id: str) -> str:
    asset = store.get_asset(asset_id)
    if asset.source_key is None:
        return ""
    row = store.conn.execute(
        "SELECT id FROM job WHERE asset_id = ? AND type = 'proxy' AND idempotency_key = ?",
        (asset_id, f"proxy:{asset.source_key}"),
    ).fetchone()
    return row["id"] if row else ""
