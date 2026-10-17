"""Rundown. Items point at spans. Killing an item does not delete the asset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from volt.store import Store, _id, _now


@dataclass
class Item:
    id: str
    position: int
    slug: str
    status: str
    span_id: Optional[str]


def open_show(store: Store, title: str) -> str:
    show_id = _id()
    store.conn.execute(
        "INSERT INTO show (id, title, created_at) VALUES (?, ?, ?)",
        (show_id, title, _now()),
    )
    store.conn.commit()
    return show_id


def show_lock(show_id: str) -> str:
    """The lock name every read-modify-write on one show's items takes (see Store.transaction)."""
    return f"show:{show_id}"


def add_item(store: Store, show_id: str, slug: str, script: str = "") -> str:
    item_id = _id()
    with store.transaction(show_lock(show_id)):  # next position is read then written: one at a time
        position = store.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM item WHERE show_id = ?",
            (show_id,),
        ).fetchone()[0]
        store.conn.execute(
            """INSERT INTO item (id, show_id, position, slug, script, status, span_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'ready', NULL, ?)""",
            (item_id, show_id, position, slug, script, _now()),
        )
    return item_id


class ItemNotReady(Exception):
    """The item is not `ready` (on air, aired, killed or skipped), so its media cannot change. Nothing was written."""

    def __init__(self, item_id: str, status: str):
        super().__init__(f"cannot attach media to a {status} item")
        self.item_id, self.status = item_id, status


def attach(store: Store, item_id: str, span_id: str) -> None:
    """Attach a span to a ready item. KeyError for an unknown span or item; ItemNotReady otherwise refused.

    One transaction under the show's lock (the one go and take hold) ending in a write that restates the
    condition (`status = 'ready'`), so an attach racing a take gets one outcome: it lands before the item airs,
    or it is refused. It can never change the media of an item that is on air."""
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    with store.transaction(show_lock(row["show_id"])):
        if store.conn.execute("SELECT 1 FROM span WHERE id = ?", (span_id,)).fetchone() is None:
            raise KeyError(span_id)
        cur = store.conn.execute(
            "UPDATE item SET span_id = ? WHERE id = ? AND status = 'ready'", (span_id, item_id))
        if cur.rowcount != 1:
            now = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()
            if now is None:
                raise KeyError(item_id)
            raise ItemNotReady(item_id, now["status"])


def take(store: Store, item_id: str) -> None:
    row = store.conn.execute(
        "SELECT show_id, span_id FROM item WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None or row["span_id"] is None:
        raise KeyError(item_id)
    with store.transaction(show_lock(row["show_id"])):
        span = store.conn.execute(
            "SELECT asset_id FROM span WHERE id = ?", (row["span_id"],)
        ).fetchone()
        store.assert_publishable(span["asset_id"])
        # Only this show's on-air item steps down. (This used to clear every show's.)
        store.conn.execute(
            "UPDATE item SET status = 'ready' WHERE status = 'on air' AND id != ? AND show_id = ?",
            (item_id, row["show_id"]),
        )
        store.conn.execute("UPDATE item SET status = 'on air' WHERE id = ?", (item_id,))


def kill(store: Store, item_id: str) -> bool:
    """Kill a ready item. Returns True when this call killed it, False when it was not ready (on air, aired,
    skipped, already killed) or does not exist; the status is then left alone.

    The decision is one conditional write (`WHERE status = 'ready'`) taken under the show's lock, the same one
    go and take hold while they pick and air the next item. So a kill and a go that race get one outcome: the
    go airs the item and the kill returns False, or the kill lands first and the go passes the item over. An
    item is never killed after it went on air, so no cue is sent for a killed item."""
    row = store.conn.execute("SELECT show_id FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return False
    with store.transaction(show_lock(row["show_id"])):
        cur = store.conn.execute(
            "UPDATE item SET status = 'killed' WHERE id = ? AND status = 'ready'", (item_id,))
    return cur.rowcount == 1


def reorder(store: Store, show_id: str, item_ids: list[str]) -> None:
    with store.transaction(show_lock(show_id)):  # two reorders interleaving would deadlock or mix orders
        for position, item_id in enumerate(item_ids, start=1):
            store.conn.execute(
                "UPDATE item SET position = ? WHERE id = ? AND show_id = ?",
                (position, item_id, show_id),
            )


def items(store: Store, show_id: str) -> list[Item]:
    rows = store.conn.execute(
        """SELECT id, position, slug, status, span_id
           FROM item WHERE show_id = ? ORDER BY position""",
        (show_id,),
    ).fetchall()
    return [Item(row["id"], row["position"], row["slug"], row["status"], row["span_id"]) for row in rows]
