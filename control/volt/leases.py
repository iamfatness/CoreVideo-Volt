"""Edit leases and presence (docs/superpowers/specs/2026-11-01-collaboration-identity-design.md).

A lease says "this person is editing this item right now". It is advisory UX backed by a server check, and a versioned write (building.item_version)
holds even when nobody uses a lease. A lease lasts `TTL` seconds and the editor renews it; a closed laptop simply lets it lapse. Presence is
"who is looking at which show", live for `PRESENCE_LIVE` seconds.

Time is always passed in (`now`, epoch seconds): the server's clock is the only clock, a client never sends one, and nothing here reads it.

Nothing on the on-air path (take, go, hold, release, skip, kill) imports or reads this module's tables. A page that cannot reach them says
"collaboration unavailable" and carries on; the callers in serve.py and building.py treat a missing table as no lease.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from volt.sessions import _epoch, _stamp

log = logging.getLogger("volt.leases")

TTL = 45                    # seconds a lease lasts without a renew; the browser renews every 15
PRESENCE_LIVE = 30          # seconds a presence row counts as "here"
PRESENCE_PRUNE = 300        # rows older than this are deleted on the next heartbeat
MAX_CLIENTS_PER_USER = 10   # presence rows kept per user; a hostile client cannot fill the table
KINDS = ("item",)           # the resource kinds a lease can name; v1 is the item (slug and script)

_CLIENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_VIEW = re.compile(r"(show|library|item:[A-Za-z0-9_.:-]{1,128})")

# Told when a live lease is taken over: ON_TAKEN(store, previous=, actor=, item=, show=, now=). The notification table arrives with a later
# task; until then this is a no-op that task fills in. A hook that fails is logged and never undoes the takeover.
ON_TAKEN = None


class LeaseError(Exception):
    """A refusal. `status` is the HTTP status, `code` is stable, `reason` is for people. Nothing changed."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


@dataclass(frozen=True)
class Lease:
    kind: str
    resource_id: str
    holder: str
    client_id: str
    acquired_at: float
    renewed_at: float
    expires_at: float
    previous: Optional[str] = field(default=None, compare=False)   # a takeover: who held it
    fresh: bool = field(default=False, compare=False)   # True when this call began the lease (an acquire), False when it only extended one

    def seconds_left(self, now: float) -> int:
        return max(0, math.ceil(self.expires_at - now))

    def public(self, now: float) -> dict:
        return {"item": self.resource_id, "holder": self.holder, "clientId": self.client_id,
                "expiresAt": _stamp(self.expires_at), "secondsLeft": self.seconds_left(now)}


def check_client_id(value) -> str:
    if not isinstance(value, str) or not _CLIENT.fullmatch(value):
        raise LeaseError(422, "bad_input", "clientId must be 1 to 64 characters from letters, digits and _ . : -")
    return value


def _need_kind(kind) -> None:
    if kind not in KINDS:
        raise LeaseError(422, "bad_input", f"cannot lease a {kind}; leases are on {', '.join(KINDS)}")


def _row(store, kind: str, rid: str):
    return store.conn.execute(
        "SELECT holder, client_id, acquired_at, renewed_at, expires_at FROM lease WHERE resource_kind = ? AND resource_id = ?",
        (kind, rid)).fetchone()


def _lease_of(kind: str, rid: str, row, fresh: bool = False, previous: Optional[str] = None) -> Lease:
    return Lease(kind, rid, row["holder"], row["client_id"], _epoch(row["acquired_at"]), _epoch(row["renewed_at"]),
                 _epoch(row["expires_at"]), previous, fresh)


def _conflict(kind: str, rid: str, row, now: float, code: str) -> LeaseError:
    held = _lease_of(kind, rid, row)
    if code == "lease_lost":
        reason = f"{held.holder} took this item over; your text is still in your editor"
    else:
        reason = f"{held.holder} is editing this item"
    return LeaseError(409, code, reason, holder=held.holder, expiresAt=_stamp(held.expires_at), secondsLeft=held.seconds_left(now))


def available(store) -> bool:
    """Whether the lease and presence tables can be read. A write that checks leases asks first, so a missing table is no lease rather than an error."""
    if store.postgres:
        sql = "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = current_schema() AND table_name IN ('lease', 'presence')"
    else:
        sql = "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN ('lease', 'presence')"
    return store.conn.execute(sql).fetchone()["n"] == 2


_UPSERT = (
    "INSERT INTO lease (resource_kind, resource_id, holder, client_id, acquired_at, renewed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (resource_kind, resource_id) DO UPDATE SET "
    "acquired_at = CASE WHEN lease.holder = excluded.holder AND lease.client_id = excluded.client_id AND lease.expires_at > excluded.renewed_at "
    "THEN lease.acquired_at ELSE excluded.acquired_at END, "
    "holder = excluded.holder, client_id = excluded.client_id, renewed_at = excluded.renewed_at, expires_at = excluded.expires_at "
    "WHERE lease.expires_at <= excluded.renewed_at OR (lease.holder = excluded.holder AND lease.client_id = excluded.client_id)")


def acquire(store, kind: str, rid: str, holder: str, client_id: str, now: float, renew: bool = False) -> Lease:
    """Take a free item's lease, or extend your own. One upsert, guarded by expiry or by being the same holder and client: the row count says
    who won, so N concurrent acquirers get exactly one winner on SQLite and on Postgres. The same call acquires and renews.

    `renew=True` says the caller believes it holds the lease; if someone else does, the refusal is `lease_lost` (their editor stops saving)
    and not `leased`. An item that is not ready (on air, aired, skipped, killed) cannot be leased: the editing ended with the take."""
    _need_kind(kind)
    check_client_id(client_id)
    for _ in range(3):
        with store.transaction():
            item = store.conn.execute("SELECT status FROM item WHERE id = ?", (rid,)).fetchone()
            if item is None:
                raise LeaseError(404, "not_found", f"no such item: {rid}")
            if item["status"] != "ready":
                raise LeaseError(409, "not_editable", f"cannot edit a {item['status']} item")
            stamp = _stamp(now)
            if store.conn.execute(_UPSERT, (kind, rid, holder, client_id, stamp, stamp, _stamp(now + TTL))).rowcount == 1:
                won = _row(store, kind, rid)
                return _lease_of(kind, rid, won, fresh=won["acquired_at"] == won["renewed_at"] or _epoch(won["acquired_at"]) == _epoch(won["renewed_at"]))
            row = _row(store, kind, rid)
            if row is not None:
                raise _conflict(kind, rid, row, now, "lease_lost" if renew else "leased")
    raise LeaseError(409, "conflict", "the lease changed while it was being taken; try again")


def release(store, kind: str, rid: str, holder: str, client_id: str) -> bool:
    """Give up your own lease. Idempotent: True when a lease was freed, False when you held nothing. Another tab's lease is not yours to free."""
    _need_kind(kind)
    check_client_id(client_id)
    with store.transaction():
        return store.conn.execute(
            "DELETE FROM lease WHERE resource_kind = ? AND resource_id = ? AND holder = ? AND client_id = ?",
            (kind, rid, holder, client_id)).rowcount == 1


def takeover(store, kind: str, rid: str, holder: str, client_id: str, now: float) -> Lease:
    """Steal a *live* lease. A producer must be able to fix a script when its holder has walked away with a live tab. A free or lapsed item is
    `not_leased` (acquire it instead). The previous holder's next renew is `lease_lost`; ON_TAKEN tells them, when a different person lost it."""
    _need_kind(kind)
    check_client_id(client_id)
    for _ in range(3):
        with store.transaction(f"lease:{kind}:{rid}"):
            item = store.conn.execute("SELECT show_id, status FROM item WHERE id = ?", (rid,)).fetchone()
            if item is None:
                raise LeaseError(404, "not_found", f"no such item: {rid}")
            if item["status"] != "ready":
                raise LeaseError(409, "not_editable", f"cannot edit a {item['status']} item")
            row = _row(store, kind, rid)
            if row is None or _epoch(row["expires_at"]) <= now:
                raise LeaseError(409, "not_leased", "nobody holds this item; open it to edit")
            if row["holder"] == holder and row["client_id"] == client_id:
                return _lease_of(kind, rid, row)
            stamp = _stamp(now)
            took = store.conn.execute(
                "UPDATE lease SET holder = ?, client_id = ?, acquired_at = ?, renewed_at = ?, expires_at = ? "
                "WHERE resource_kind = ? AND resource_id = ? AND holder = ? AND client_id = ?",
                (holder, client_id, stamp, stamp, _stamp(now + TTL), kind, rid, row["holder"], row["client_id"])).rowcount == 1
            if took:
                previous, show = row["holder"], item["show_id"]
                result = _lease_of(kind, rid, _row(store, kind, rid), fresh=True, previous=previous)
                break
    else:
        raise LeaseError(409, "conflict", "the lease changed while it was being taken over; try again")
    if previous != holder and ON_TAKEN is not None:
        try:
            ON_TAKEN(store, previous=previous, actor=holder, item=rid, show=show, now=now)
        except Exception:  # noqa: BLE001 - a notification that fails must not undo a takeover
            log.exception("lease_taken hook failed")
    return result


def check_write(store, kind: str, rid: str, holder: str, client_id: Optional[str], now: float) -> None:
    """Refuse (409 leased) a write to a resource whose live lease belongs to someone else: another person, or another tab of the same person.
    A write that names no client cannot match a lease's client, so it is refused while anyone holds the item. No lease, no check."""
    row = _row(store, kind, rid)
    if row is None or _epoch(row["expires_at"]) <= now:
        return
    if row["holder"] == holder and client_id is not None and row["client_id"] == client_id:
        return
    raise _conflict(kind, rid, row, now, "leased")


def live(store, show_id: str, now: float) -> list:
    """The unexpired leases on the ready items of one show, in rundown order."""
    rows = store.conn.execute(
        "SELECT l.resource_id, l.holder, l.client_id, l.acquired_at, l.renewed_at, l.expires_at FROM lease l JOIN item i ON i.id = l.resource_id "
        "WHERE l.resource_kind = 'item' AND i.show_id = ? AND i.status = 'ready' ORDER BY i.position, i.id", (show_id,)).fetchall()
    out = []
    for r in rows:
        held = _lease_of("item", r["resource_id"], r)
        if held.expires_at > now:
            out.append(held.public(now))
    return out


# ---- presence ---------------------------------------------------------------

def beat(store, user: str, client_id: str, show_id: Optional[str], view, now: float) -> None:
    """Record that `user`, in the browser tab `client_id`, is looking at `view` (`show`, `library` or `item:<id>`) of a show. Sent every 10 s.
    Old rows are removed here, and a user keeps at most MAX_CLIENTS_PER_USER of them."""
    check_client_id(client_id)
    if not isinstance(view, str) or not _VIEW.fullmatch(view):
        raise LeaseError(422, "bad_input", "view must be show, library or item:<id>")
    with store.transaction():
        store.conn.execute(
            "INSERT INTO presence (user_name, client_id, show_id, view, seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_name, client_id) DO UPDATE SET show_id = excluded.show_id, view = excluded.view, seen_at = excluded.seen_at",
            (user, client_id, show_id, view, _stamp(now)))
        store.conn.execute("DELETE FROM presence WHERE seen_at < ?", (_stamp(now - PRESENCE_PRUNE),))
        mine = store.conn.execute("SELECT client_id FROM presence WHERE user_name = ? ORDER BY seen_at DESC, client_id", (user,)).fetchall()
        for extra in mine[MAX_CLIENTS_PER_USER:]:
            store.conn.execute("DELETE FROM presence WHERE user_name = ? AND client_id = ?", (user, extra["client_id"]))


def who(store, show_id: str, now: float) -> list:
    """Who is looking at a show: one entry per browser tab seen in the last PRESENCE_LIVE seconds."""
    rows = store.conn.execute(
        "SELECT p.user_name, p.client_id, p.view, p.seen_at, u.display_name FROM presence p LEFT JOIN volt_user u ON u.name = p.user_name "
        "WHERE p.show_id = ? ORDER BY p.user_name, p.client_id", (show_id,)).fetchall()
    return [{"user": r["user_name"], "name": r["display_name"] or r["user_name"], "clientId": r["client_id"], "view": r["view"]}
            for r in rows if now - _epoch(r["seen_at"]) < PRESENCE_LIVE]
