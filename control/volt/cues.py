"""Cues hang on the item. A room adapter sends them."""

from __future__ import annotations

from dataclasses import dataclass

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


def add_cue(store: Store, item_id: str, room: str, command: str, payload: str) -> str:
    if room == "corevideo.pro" and command not in COREVIDEO_COMMANDS:
        raise ValueError(command)
    position = store.conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM cue WHERE item_id = ?",
        (item_id,),
    ).fetchone()[0]
    cue_id = _id()
    store.conn.execute(
        """INSERT INTO cue (id, item_id, position, room, command, payload, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'stored', ?)""",
        (cue_id, item_id, position, room, command, payload, _now()),
    )
    store.conn.commit()
    return cue_id


def cues(store: Store, item_id: str) -> list[Cue]:
    rows = store.conn.execute(
        "SELECT id, room, command, payload, status FROM cue WHERE item_id = ? ORDER BY position",
        (item_id,),
    ).fetchall()
    return [Cue(row["id"], row["room"], row["command"], row["payload"], row["status"]) for row in rows]


def arm(store: Store, item_id: str) -> list[Cue]:
    take(store, item_id)
    store.conn.execute(
        "UPDATE cue SET status = 'armed' WHERE item_id = ? AND status = 'stored'",
        (item_id,),
    )
    store.conn.commit()
    return cues(store, item_id)