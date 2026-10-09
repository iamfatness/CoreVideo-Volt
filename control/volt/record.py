"""Record start and partial proxy. The ID exists before wrap."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from volt.ingest import sha256
from volt.store import Store


class RecordError(Exception):
    pass


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


def build_proxy(store: Store, asset_id: str, proxy_dir: Path) -> str:
    source = store.conn.execute(
        "SELECT id, location, open FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    if source is None:
        raise RecordError(f"no hi-res for {asset_id}")
    proxy_dir.mkdir(parents=True, exist_ok=True)
    dest = proxy_dir / f"{asset_id}.mp4"
    current = store.conn.execute(
        "SELECT id FROM essence WHERE asset_id = ? AND role = 'proxy'",
        (asset_id,),
    ).fetchone()
    if (
        current is not None
        and not source["open"]
        and dest.exists()
        and dest.stat().st_mtime >= Path(source["location"]).stat().st_mtime
    ):
        return current["id"]
    probe = subprocess.run(
        [
            "ffmpeg", "-y", "-i", source["location"],
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-an", str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or not dest.exists():
        job_id = _proxy_job(store, asset_id)
        if job_id:
            store.fail_job(job_id, probe.stderr[-400:] or "proxy failed")
        raise RecordError("proxy failed")
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


def wrap(store: Store, asset_id: str, settle: float = 0.25) -> str:
    source = store.conn.execute(
        "SELECT id, location, open, checksum FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    if source is None:
        raise RecordError(f"no hi-res for {asset_id}")
    if not source["open"] and source["checksum"]:
        return source["checksum"]
    path = Path(source["location"])
    size = path.stat().st_size
    time.sleep(settle)
    if path.stat().st_size != size:
        raise RecordError(f"{path.name} is still growing")
    digest = sha256(Path(source["location"]))
    store.wrap_essence(source["id"], digest)
    store.conn.execute(
        "UPDATE essence SET open = 0 WHERE asset_id = ? AND role = 'proxy'",
        (asset_id,),
    )
    store.conn.commit()
    job_id = _proxy_job(store, asset_id)
    if job_id:
        store.complete_job(job_id)
    return digest


def _proxy_job(store: Store, asset_id: str) -> str:
    asset = store.get_asset(asset_id)
    if asset.source_key is None:
        return ""
    row = store.conn.execute(
        "SELECT id FROM job WHERE asset_id = ? AND type = 'proxy' AND idempotency_key = ?",
        (asset_id, f"proxy:{asset.source_key}"),
    ).fetchone()
    return row["id"] if row else ""
