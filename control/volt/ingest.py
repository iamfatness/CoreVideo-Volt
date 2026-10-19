"""Watch-folder ingest. One source key, one asset. Replay does not mint.

`request` mints and enqueues. The runner's `run_ingest` copies, probes, and checksums.
`ingest_file` is the library wrapper: request, then run the handler, so callers still get a finished copy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from volt import disk, shots
from volt.store import Store


class IngestError(Exception):
    pass


@dataclass
class IngestResult:
    asset_id: str
    essence_id: str
    job_id: str
    status: str
    minted: bool


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parses(path: Path) -> bool:
    if path.stat().st_size == 0:
        return False
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and "nb_streams=" in probe.stdout


def request(
    store: Store,
    source: Path,
    *,
    owner: str,
    quarantine: Path,
    source_key: Optional[str] = None,
    show_name: Optional[str] = None,
    camera: Optional[str] = None,
) -> IngestResult:
    """Mint (or reuse) the asset and enqueue ingest. Does not copy."""
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"not a file: {source}")
    key = source_key or f"file:{source.name}:{source.stat().st_size}"
    minted = store.find_by_source(key) is None
    asset = store.mint_source(key, owner=owner, show_name=show_name, camera=camera)
    job = store.enqueue(
        asset.id,
        "ingest",
        f"ingest:{key}",
        payload={
            "path": str(source),
            "quarantine": str(quarantine),
            "owner": owner,
            "show_name": show_name,
            "camera": camera,
        },
    )
    return IngestResult(asset.id, "", job.id, "queued", minted)


def run_ingest(store: Store, job) -> None:
    """Copy, probe, checksum, essence, follow-on jobs. Raises runner.Permanent / runner.Defer."""
    from volt import runner

    payload = job.payload if isinstance(getattr(job, "payload", None), dict) else {}
    try:
        source = Path(payload["path"])
        quarantine = Path(payload["quarantine"])
        _owner, _show_name, _camera = payload.get("owner"), payload.get("show_name"), payload.get("camera")
    except (KeyError, TypeError) as exc:
        raise runner.Permanent(f"ingest payload missing path or quarantine") from exc
    if not source.is_file():
        raise runner.Permanent(f"not a file: {source}")
    dest = quarantine / f"{job.asset_id}-{source.name}"
    if not dest.exists():
        try:
            disk.require_space(quarantine, "ingest", need=source.stat().st_size)
        except disk.DiskLowError as exc:
            raise runner.Defer(str(exc), 60.0) from exc
        quarantine.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    if not parses(dest):
        raise runner.Permanent("parse failed")
    digest = sha256(dest)
    existing_essence = store.conn.execute(
        "SELECT id FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (job.asset_id,),
    ).fetchone()
    if existing_essence is None:
        rate = shots.probe_frame_rate(dest)  # first: the length is counted on the essence's own rate, not the average
        essence_id = store.add_essence(
            job.asset_id,
            role="hi-res",
            location=str(dest),
            open_file=False,
            checksum=digest,
            duration_frames=shots.probe_duration_frames(dest, rate),
            frame_rate=rate,
            audio_layout=shots.probe_audio_layout(dest),
        )
        shots.record_media_facts(store, essence_id, dest)  # codecs and the embedded start timecode, before any span is placed
        from volt.record import chain_after_wrap  # record imports this module
        chain_after_wrap(store, job.asset_id)  # a closed file gets its proxy and transcript like a wrapped recording


def ingest_file(
    store: Store,
    source: Path,
    quarantine: Path,
    owner: str,
    source_key: Optional[str] = None,
    show_name: Optional[str] = None,
    camera: Optional[str] = None,
) -> IngestResult:
    from volt import runner

    result = request(
        store, source, owner=owner, quarantine=quarantine,
        source_key=source_key, show_name=show_name, camera=camera)
    job = store.get_job(result.job_id)
    try:
        payload = json.loads(job.payload) if job.payload else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    claimed = runner.Claimed(
        job.id, job.asset_id, job.type, job.idempotency_key, job.attempt, payload=payload)
    try:
        run_ingest(store, claimed)
    except runner.Defer as exc:
        raise IngestError(str(exc)) from exc
    except runner.Permanent as exc:
        store.fail_job(job.id, str(exc))
        if str(exc) == "parse failed":
            return IngestResult(result.asset_id, "", job.id, "quarantine", result.minted)
        raise IngestError(str(exc)) from exc
    store.complete_job(job.id)
    row = store.conn.execute(
        "SELECT id FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (result.asset_id,),
    ).fetchone()
    return IngestResult(result.asset_id, row["id"] if row else "", job.id, "done", result.minted)
