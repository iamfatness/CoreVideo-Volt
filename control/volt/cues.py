"""Cues hang on the item. A take does not call CoreVideo."""

from __future__ import annotations

from dataclasses import dataclass

from volt.rundown import take
from volt.store import Store, _id, _now


@dataclass
class Cue:
    id: str
    target: str
    payload: str
    status: str


def add_cue(store: Store, item_id: str, target: str, payload: str) -> str:
    position = store.conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM cue WHERE item_id = ?",
        (item_id,),
    ).fetchone()[0]
    cue_id = _id()
    store.conn.execute(
        """INSERT INTO cue (id, item_id, position, target, payload, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'stored', ?)""",
        (cue_id, item_id, position, target, payload, _now()),
    )
    store.conn.commit()
    return cue_id


def cues(store: Store, item_id: str) -> list[Cue]:
    rows = store.conn.execute(
        "SELECT id, target, payload, status FROM cue WHERE item_id = ? ORDER BY position",
        (item_id,),
    ).fetchall()
    return [Cue(row["id"], row["target"], row["payload"], row["status"]) for row in rows]


def arm(store: Store, item_id: str) -> list[Cue]:
    take(store, item_id)
    store.conn.execute(
        "UPDATE cue SET status = 'armed' WHERE item_id = ? AND status = 'stored'",
        (item_id,),
    )
    store.conn.commit()
    return cues(store, item_id)
