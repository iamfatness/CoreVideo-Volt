"""Safe mode: one global switch that makes Volt send nothing to any non-CoreVideo device (device-control spec, "Safety").

It is read from the database at every send (never cached), so the server, the clock and the runner obey one switch within one call.
Observation and the status poller keep working. It does not stop CoreVideo cues: hold is the tool for stopping the rundown.
"""

from __future__ import annotations

from typing import Optional

from volt.adapters.base import SafeModeError
from volt.roomreg import COREVIDEO
from volt.store import Store, _now

SAFE_TEXT = "safe mode is on; nothing sent"


def is_on(store: Store) -> bool:
    row = store.conn.execute("SELECT safe_mode FROM device_safety WHERE id = 1").fetchone()
    return bool(row and row["safe_mode"])


def state(store: Store) -> dict:
    row = store.conn.execute("SELECT safe_mode, reason, set_by, set_at FROM device_safety WHERE id = 1").fetchone()
    if row is None:
        return {"on": False, "reason": "", "setBy": "", "setAt": None}
    return {"on": bool(row["safe_mode"]), "reason": row["reason"] or "", "setBy": row["set_by"] or "",
            "setAt": str(row["set_at"]) if row["set_at"] else None}


def set(store: Store, on: bool, reason: str, actor: str) -> dict:  # noqa: A001 - the spec names it set
    store.conn.execute(
        """INSERT INTO device_safety (id, safe_mode, reason, set_by, set_at) VALUES (1, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET safe_mode = excluded.safe_mode, reason = excluded.reason,
                                          set_by = excluded.set_by, set_at = excluded.set_at""",
        (1 if on else 0, reason[:300], actor, _now()))
    store.conn.commit()
    return state(store)


def check_send(store: Store, kind: str, spec: Optional[object] = None) -> None:
    """Raise SafeModeError when a send to a device of this kind is not allowed. CoreVideo is never refused."""
    if kind == COREVIDEO:
        return
    if is_on(store):
        raise SafeModeError(SAFE_TEXT)
