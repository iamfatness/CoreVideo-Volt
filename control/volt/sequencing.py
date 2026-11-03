"""Cross-process exclusion by a lease row.

`serve` used to order record start and stop with an in-process `threading.Lock`, which two server processes
behind a balancer do not share. A lease is a row `(name, holder, expires_at)` taken by one conditional upsert:

- free or expired: the caller takes it;
- held by the same holder: the caller renews it;
- held by someone else and live: the caller is refused (`LeaseBusy`), it does not wait.

The row is released in `finally`, and only if it is still ours (a holder whose lease expired and was taken over
must not delete the new holder's row). A crashed node simply stops renewing and the row expires after `ttl`.

Time is epoch seconds from `now`; nodes are assumed to keep clocks within a few seconds (the TTL is 120 s, so a
skew of that order is harmless). A lease is NOT held in a database transaction: a transaction held across a room
wait of up to 30 s would tie up a pooled connection.

go, skip and kill are not ordered here: each already decides under the show's own advisory lock.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Optional

DEFAULT_TTL = 120.0


class LeaseBusy(Exception):
    """Someone else holds the lease and it has not expired. Nothing changed."""

    def __init__(self, name: str, holder: Optional[str]):
        super().__init__(f"{name} is held by {holder or 'another process'}")
        self.name, self.holder = name, holder


def record_lease_name(room_key: str) -> str:
    return f"record:{room_key}"


def default_holder() -> str:
    """Unique per call: two threads of one process are different holders, so they exclude each other too."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


_UPSERT = (
    "INSERT INTO exclusive_lease (name, holder, expires_at) VALUES (?, ?, ?) "
    "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, expires_at = excluded.expires_at "
    "WHERE exclusive_lease.expires_at <= ? OR exclusive_lease.holder = excluded.holder")


def _acquire(store, name: str, holder: str, ttl: float, now: float) -> None:
    with store.transaction():
        cur = store.conn.execute(_UPSERT, (name, holder, now + ttl, now))
        won = cur.rowcount == 1
        row = None if won else store.conn.execute(
            "SELECT holder FROM exclusive_lease WHERE name = ?", (name,)).fetchone()
    if not won:
        raise LeaseBusy(name, row["holder"] if row else None)


def _release(store, name: str, holder: str) -> None:
    with store.transaction():
        store.conn.execute("DELETE FROM exclusive_lease WHERE name = ? AND holder = ?", (name, holder))


@contextmanager
def exclusive(store, name: str, ttl: float = DEFAULT_TTL, holder: Optional[str] = None,
              now: Callable[[], float] = time.time):
    """Hold the lease `name` for the block. Raises LeaseBusy at entry when someone else holds a live lease."""
    holder = holder or default_holder()
    _acquire(store, name, holder, ttl, now())
    try:
        yield
    finally:
        _release(store, name, holder)
