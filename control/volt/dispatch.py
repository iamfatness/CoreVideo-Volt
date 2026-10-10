"""Dispatch armed cues to a room. A cue is fired when the room shows it, not when it acks."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from volt.cues import cues
from volt.live import start_recording
from volt.rooms import LiveError
from volt.store import Store

ROOM = "corevideo.pro"


@dataclass
class Result:
    cue_id: str
    command: str
    status: str  # fired | failed | unsupported
    detail: str = ""


def _poll(check: Callable[[], bool], attempts: int, interval: float, sleep) -> bool:
    for _ in range(attempts):
        if check():
            return True
        sleep(interval)
    return False


def _stream_start(store, room, cue, ctx) -> None:
    room.invoke("transport.stream.set", [True])

    def producing() -> bool:
        return any(
            o.state in ("producing", "live") and o.usable() for o in room.senders_observed()
        )

    if not _poll(producing, ctx["attempts"], ctx["interval"], ctx["sleep"]):
        raise LiveError("stream start: no sender observed producing")


def _overlay_take(store, room, cue, ctx) -> None:
    want = cue.payload.strip().lower() not in ("off", "false", "0")
    room.invoke("graphics.lowerThird.set", [want])
    if not _poll(
        lambda: room.state().get("lowerThirdOnAir") is want,
        ctx["attempts"], ctx["interval"], ctx["sleep"],
    ):
        raise LiveError(f"overlay: lower third never observed {'on' if want else 'off'} air")


def _record_start(store, room, cue, ctx) -> None:
    start_recording(
        store, room, owner=ctx["owner"], camera=cue.payload or None,
        attempts=ctx["attempts"], interval=ctx["interval"], sleep=ctx["sleep"],
    )


# Only commands with a known control action AND a known piece of evidence belong here.
HANDLERS = {
    "record.start": _record_start,
    "stream.start": _stream_start,
    "overlay.take": _overlay_take,
}


def dispatch_item(
    store: Store,
    room,
    item_id: str,
    owner: str = "desk",
    attempts: int = 40,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Result]:
    """Send the item's armed cues in order. Stop at the first one that does not land.

    A cue that fails or is unsupported stays armed. Fired cues are skipped on replay.
    """
    ctx = {"owner": owner, "attempts": attempts, "interval": interval, "sleep": sleep}
    results: list[Result] = []
    for cue in cues(store, item_id):
        if cue.status == "fired":
            continue
        handler = HANDLERS.get(cue.command) if cue.room == ROOM else None
        if handler is None:
            results.append(Result(cue.id, cue.command, "unsupported",
                                  f"no verified adapter for {cue.room}/{cue.command}"))
            break
        try:
            handler(store, room, cue, ctx)
        except LiveError as exc:
            results.append(Result(cue.id, cue.command, "failed", str(exc)))
            break
        store.conn.execute("UPDATE cue SET status = 'fired' WHERE id = ?", (cue.id,))
        store.conn.commit()
        results.append(Result(cue.id, cue.command, "fired"))
    return results
