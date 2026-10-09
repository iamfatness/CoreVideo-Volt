"""Watch-folder ingest. One source key, one asset. Replay does not mint."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


def ingest_file(
    store: Store,
    source: Path,
    quarantine: Path,
    owner: str,
    source_key: Optional[str] = None,
    show_name: Optional[str] = None,
    camera: Optional[str] = None,
) -> IngestResult:
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"not a file: {source}")
    key = source_key or f"file:{source.name}:{source.stat().st_size}"
    existing = store.find_by_source(key)
    minted = existing is None
    asset = store.mint_source(key, owner=owner, show_name=show_name, camera=camera)
    quarantine.mkdir(parents=True, exist_ok=True)
    dest = quarantine / f"{asset.id}-{source.name}"
    if not dest.exists():
        shutil.copy2(source, dest)
    job = store.enqueue(asset.id, "ingest", key)
    if not parses(dest):
        store.fail_job(job.id, "parse failed")
        return IngestResult(asset.id, "", job.id, "quarantine", minted)
    digest = sha256(dest)
    existing_essence = store.conn.execute(
        "SELECT id FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset.id,),
    ).fetchone()
    if existing_essence is None:
        essence_id = store.add_essence(
            asset.id,
            role="hi-res",
            location=str(dest),
            open_file=False,
            checksum=digest,
        )
    else:
        essence_id = existing_essence["id"]
    store.complete_job(job.id)
    return IngestResult(asset.id, essence_id, job.id, "done", minted)
