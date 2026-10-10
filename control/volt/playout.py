"""Playout. The clock walks the rundown and writes as-run on the item."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from volt.rundown import take
from volt.store import Store, _now


class PlayoutError(Exception):
    pass


def on_air(store: Store, show_id: str) -> Optional[str]:
    row = store.conn.execute(
        "SELECT id FROM item WHERE show_id = ? AND status = 'on air' ORDER BY position",
        (show_id,),
    ).fetchone()
    return row["id"] if row else None


@dataclass
class Advance:
    target: Optional[str]
    previous: Optional[str]
    passed: list[str] = field(default_factory=list)


def go(store: Store, show_id: str, clock: Callable[[], str] = _now) -> Optional[str]:
    """Take the next item. Returns its id, or None when the show is over."""
    return advance(store, show_id, clock).target


def rewind(store: Store, adv: Advance) -> None:
    """Undo an advance: the target is ready again, the outgoing item back on air."""
    if adv.target is not None:
        store.conn.execute(
            "UPDATE item SET status = 'ready', as_run_in = NULL WHERE id = ?", (adv.target,)
        )
    if adv.previous is not None:
        store.conn.execute(
            "UPDATE item SET status = 'on air', as_run_out = NULL WHERE id = ?", (adv.previous,)
        )
    for item_id in adv.passed:
        store.conn.execute("UPDATE item SET status = 'killed' WHERE id = ?", (item_id,))
    store.conn.commit()


def advance(store: Store, show_id: str, clock: Callable[[], str] = _now) -> Advance:
    """Take the next item and say what changed.

    The outgoing item gets its as-run out and the incoming item its as-run in from
    the same clock read. A killed item that is passed over is recorded as skipped.
    If the next item cannot air (not cleared), nothing changes.
    """
    current = on_air(store, show_id)
    floor = 0
    if current is not None:
        floor = store.conn.execute("SELECT position FROM item WHERE id = ?", (current,)).fetchone()["position"]
    passed: list[str] = []
    target: Optional[str] = None
    for row in store.conn.execute(
        """SELECT id, status FROM item
           WHERE show_id = ? AND position > ? AND status IN ('ready', 'killed')
           ORDER BY position""",
        (show_id, floor),
    ).fetchall():
        if row["status"] == "killed":
            passed.append(row["id"])
        else:
            target = row["id"]
            break
    if target is not None:
        take(store, target)  # raises before changing anything if it cannot air
    now = clock()
    if current is not None:
        store.conn.execute(
            "UPDATE item SET status = 'aired', as_run_out = ? WHERE id = ?", (now, current)
        )
    for item_id in passed:
        store.conn.execute("UPDATE item SET status = 'skipped' WHERE id = ?", (item_id,))
    if target is not None:
        store.conn.execute("UPDATE item SET as_run_in = ? WHERE id = ?", (now, target))
    store.conn.commit()
    return Advance(target, current, passed)


def skip(store: Store, item_id: str) -> None:
    row = store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    if row["status"] == "on air":
        raise PlayoutError("cannot skip the item on air")
    if row["status"] != "ready":
        raise PlayoutError(f"cannot skip a {row['status']} item")
    store.conn.execute("UPDATE item SET status = 'skipped' WHERE id = ?", (item_id,))
    store.conn.commit()
