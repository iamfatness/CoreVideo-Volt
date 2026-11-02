"""Device status, the event log, and the status poller (device-control spec).

`record_event` writes one row per send attempt or refusal: the audit of what went out. `poll` only observes; it never sends, so the
panel stays truthful in safe mode. Nothing configured means no rows and nothing shown.
"""

from __future__ import annotations

import json
import logging

from volt.adapters.base import DeviceError
from volt.store import Store, _id, _now

log = logging.getLogger("volt.devices")

OUTCOMES = ("fired", "failed", "refused", "prepared")


def record_event(store: Store, device: str, command: str, params, outcome: str, detail: str = "",
                 cue_id=None, actor: str = "system") -> str:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    event_id = _id()
    store.conn.execute(
        "INSERT INTO device_event (id, at, device, command, params, outcome, detail, cue_id, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, _now(), device, command, params if isinstance(params, str) else json.dumps(params, sort_keys=True, default=str),
         outcome, (detail or "")[:500], cue_id, actor))
    store.conn.commit()
    return event_id


def events(store: Store, device: str = None, limit: int = 50) -> list:
    sql = "SELECT id, at, device, command, params, outcome, detail, cue_id, actor FROM device_event"
    args: tuple = ()
    if device:
        sql, args = sql + " WHERE device = ?", (device,)
    rows = store.conn.execute(sql + " ORDER BY at DESC, id DESC LIMIT ?", args + (int(limit),)).fetchall()
    return [{"id": r["id"], "at": str(r["at"]), "device": r["device"], "command": r["command"], "params": r["params"],
             "outcome": r["outcome"], "detail": r["detail"] or "", "cueId": r["cue_id"], "actor": r["actor"]} for r in rows]


def status(store: Store) -> list:
    """What the panel shows: one entry per configured device the poller has seen (kind, name, state, detail, observed, last event)."""
    out = []
    for r in store.conn.execute("SELECT device, kind, state, detail, observed, updated_at FROM device_status ORDER BY device").fetchall():
        last = store.conn.execute(
            "SELECT command, outcome, at FROM device_event WHERE device = ? ORDER BY at DESC, id DESC LIMIT 1", (r["device"],)).fetchone()
        try:
            observed = json.loads(r["observed"]) if r["observed"] else None
        except ValueError:
            observed = None
        out.append({"kind": r["kind"], "name": r["device"].partition(":")[2], "state": r["state"], "detail": r["detail"] or "",
                    "observed": observed, "updatedAt": str(r["updated_at"]),
                    "lastEvent": None if last is None else {"command": last["command"], "outcome": last["outcome"], "at": str(last["at"])}})
    return out


def _set_status(store: Store, kind: str, name: str, state: str, detail: str, observed) -> None:
    store.conn.execute(
        """INSERT INTO device_status (device, kind, state, detail, observed, updated_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (device) DO UPDATE SET kind = excluded.kind, state = excluded.state, detail = excluded.detail,
                                              observed = excluded.observed, updated_at = excluded.updated_at""",
        (f"{kind}:{name}", kind, state, (detail or "")[:300], None if observed is None else json.dumps(observed, sort_keys=True, default=str), _now()))
    store.conn.commit()


def poll(store: Store, registry) -> None:
    """Read each registered adapter device's state and record it. Observation only: this never calls perform or prepare."""
    for kind in registry.names():
        adapter = registry.get(kind)
        if not hasattr(adapter, "device_names") or not hasattr(adapter, "ACTIONS"):
            continue                                     # CoreVideo and the older rooms are not adapters
        for name in adapter.device_names():
            try:
                session = adapter.open(name)
            except DeviceError as exc:
                _set_status(store, kind, name, "down", str(exc), None)
                continue
            try:
                obs = session.observe()
                _set_status(store, kind, name, "up", obs.detail, obs.state)
            except DeviceError as exc:
                _set_status(store, kind, name, "down", str(exc), None)
            except Exception:                            # a bad adapter must not stop the poller
                log.exception("device poll failed for %s:%s", kind, name)
            finally:
                session.close()
