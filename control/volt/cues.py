"""Cues hang on the item. A room adapter sends them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from volt.rundown import take
from volt.store import Store, _id, _now

COREVIDEO_COMMANDS = {
    "show-input.take",
    "overlay.take",
    "audio.snapshot",
    "record.arm",
    "record.start",
    "stream.start",
}


@dataclass
class Cue:
    id: str
    room: str
    command: str
    payload: str
    status: str
    # The last dispatch failure ("failed" | "unsupported") and its reason. None once the cue fires.
    fail_status: Optional[str] = None
    fail_detail: Optional[str] = None


def add_cue(store: Store, item_id: str, room: str, command: str, payload: str) -> str:
    if room == "corevideo.pro" and command not in COREVIDEO_COMMANDS:
        raise ValueError(command)
    if room == "hyperdeck":
        from volt import hyperdeck
        if command not in hyperdeck.DECK_COMMANDS:
            raise ValueError(command)
        hyperdeck.parse_payload(payload)   # a ValueError for a payload that names no deck
    cue_id = _id()
    with store.transaction(f"item-cues:{item_id}"):  # next position is read then written: one at a time
        position = store.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM cue WHERE item_id = ?",
            (item_id,),
        ).fetchone()[0]
        store.conn.execute(
            """INSERT INTO cue (id, item_id, position, room, command, payload, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'stored', ?)""",
            (cue_id, item_id, position, room, command, payload, _now()),
        )
    return cue_id


def cues(store: Store, item_id: str) -> list[Cue]:
    rows = store.conn.execute(
        "SELECT id, room, command, payload, status, fail_status, fail_detail "
        "FROM cue WHERE item_id = ? ORDER BY position",
        (item_id,),
    ).fetchall()
    return [Cue(row["id"], row["room"], row["command"], row["payload"], row["status"],
                row["fail_status"], row["fail_detail"]) for row in rows]


def record_failure(store: Store, cue_id: str, status: str, detail: str) -> None:
    """Remember why a cue did not land. The cue stays armed; this only adds the reason."""
    store.conn.execute("UPDATE cue SET fail_status = ?, fail_detail = ? WHERE id = ?",
                       (status, detail, cue_id))
    store.conn.commit()


def clear_failure(store: Store, cue_id: str) -> None:
    """Forget a recorded failure without firing the cue. Firing clears it too."""
    store.conn.execute("UPDATE cue SET fail_status = NULL, fail_detail = NULL WHERE id = ?", (cue_id,))
    store.conn.commit()


def arm_cues(store: Store, item_id: str) -> None:
    store.conn.execute(
        "UPDATE cue SET status = 'armed' WHERE item_id = ? AND status = 'stored'",
        (item_id,),
    )
    store.conn.commit()


def arm(store: Store, item_id: str) -> list[Cue]:
    take(store, item_id)
    arm_cues(store, item_id)
    return cues(store, item_id)