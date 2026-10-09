"""Record against a room. Mint on observed output; wrap on observed finalize."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from volt.record import Record, record_start, wrap
from volt.rooms import KNOWN_STATES, LiveError, Observation
from volt.store import Store

_DEAD = {"failed", "interrupted"}


def _refuse_if_dead(o: Observation, what: str) -> None:
    if o.state in _DEAD:
        raise LiveError(f"{what}: room reports {o.state}" + (f" ({o.error})" if o.error else ""))
    if o.state not in KNOWN_STATES and o.state != "unknown":
        raise LiveError(f"{what}: unknown lifecycle state {o.state!r}, refusing to guess")


def start_recording(
    store: Store,
    room,
    owner: str,
    camera: Optional[str] = None,
    show_name: Optional[str] = None,
    attempts: int = 40,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> Record:
    """Ask the room to record, then mint only once it is observed producing a file."""
    room.invoke("transport.record.set", [True])
    for _ in range(attempts):
        o = room.observe()
        _refuse_if_dead(o, "record start")
        if o.state in ("producing", "live") and o.usable() and o.artifact_path and o.session_id:
            return record_start(
                store, f"cv:{o.session_id}", Path(o.artifact_path),
                owner=owner, camera=camera, show_name=show_name,
            )
        sleep(interval)
    raise LiveError("record start: never observed fresh, healthy output; nothing minted")


def finish_recording(
    store: Store,
    room,
    asset_id: str,
    attempts: int = 120,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Stop, then wrap only on completed + finalized for this asset's session."""
    key = store.get_asset(asset_id).source_key
    room.invoke("transport.record.set", [False])
    for _ in range(attempts):
        o = room.observe()
        _refuse_if_dead(o, "record stop")
        if o.session_id and key != f"cv:{o.session_id}":
            raise LiveError(f"record stop: room session {o.session_id!r} is not this asset's")
        if o.state == "completed" and o.finalized and o.usable():
            return wrap(store, asset_id, settle=0)
        sleep(interval)
    raise LiveError("record stop: never observed completed and finalized; not wrapped")
