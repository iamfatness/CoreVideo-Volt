"""Dispatch armed cues to a room. A cue is fired when the room shows it, not when it acks."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from volt.cues import cues, record_failure
from volt.live import start_recording
from volt.roomreg import COREVIDEO, as_registry
from volt.rooms import LiveError
from volt.show_input import show_input_take
from volt.store import Store

ROOM = COREVIDEO


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


def _show_input_take(store, room, cue, ctx) -> None:
    show_input_take(room, cue.payload, ctx["attempts"], ctx["interval"], ctx["sleep"],
                    actions=ctx.get("show_input_actions"))


# Only commands with a known control action AND a known piece of evidence belong here.
HANDLERS = {
    "show-input.take": _show_input_take,
    "record.start": _record_start,
    "stream.start": _stream_start,
    "overlay.take": _overlay_take,
}

# Per room name, the commands that room has a verified handler for. A second room kind adds its table here.
ROOM_HANDLERS = {COREVIDEO: HANDLERS}


def dispatch_item(
    store: Store,
    room,
    item_id: str,
    owner: str = "desk",
    attempts: int = 40,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    show_input_actions: Optional[list] = None,
) -> list[Result]:
    """Send the item's armed cues in order. Stop at the first one that does not land.

    A cue that fails or is unsupported stays armed. Fired cues are skipped on replay.
    `show_input_actions` is the configurable list of {action, args} steps that run
    before transport.take for show-input.take (default: input.assign then scene.route.set).
    """
    ctx = {"owner": owner, "attempts": attempts, "interval": interval, "sleep": sleep,
           "show_input_actions": show_input_actions}
    rooms = as_registry(room)  # a single room object is the CoreVideo Pro room
    results: list[Result] = []
    for cue in cues(store, item_id):
        if cue.status == "fired":
            continue
        handler = ROOM_HANDLERS.get(cue.room, {}).get(cue.command)
        client = rooms.get(cue.room)
        if handler is None or client is None:
            detail = (f"no verified adapter for {cue.room}/{cue.command}" if handler is None
                      else f"room {cue.room} is not configured")
            record_failure(store, cue.id, "unsupported", detail)
            results.append(Result(cue.id, cue.command, "unsupported", detail))
            break
        try:
            handler(store, client, cue, ctx)
        except LiveError as exc:
            record_failure(store, cue.id, "failed", str(exc))
            results.append(Result(cue.id, cue.command, "failed", str(exc)))
            break
        store.conn.execute(
            "UPDATE cue SET status = 'fired', fail_status = NULL, fail_detail = NULL WHERE id = ?", (cue.id,))
        store.conn.commit()
        results.append(Result(cue.id, cue.command, "fired"))
    return results
