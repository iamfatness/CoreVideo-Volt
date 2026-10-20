"""Schedule clock. Takes items at their scheduled time, at most once, and says what it did.

An item with `scheduled_at` is due when the clock reads that instant or later. All times are UTC instants:
stored as `YYYY-MM-DDTHH:MM:SSZ`, compared as aware datetimes, so DST and the machine's zone never enter.
`set_schedule` refuses a naive time because it names no instant.

Each tick looks at every due item that has no outcome yet and does one of these:

  fired    the take landed; the item is on air
  failed   a cue did not land (the rundown was rewound exactly as take_item does) or the take was refused;
           the reason is on the item in `schedule_note`. A failed item is never retried by the clock.
  missed   too late to fire: older than the grace window, or it was due while the show was held, or the item
           was no longer ready (killed, skipped, taken by hand). The reason is in `schedule_note`.
  held     (also stored, as schedule_state) the show was held when the item came due. It is not fired on
           release unless fire_after_hold is set; the next tick marks it missed.
  blocked  (reported only, nothing stored) a predecessor is still ready, so this item is not the next
           one in order. take advances in order and would air the wrong item, so the clock waits; once the
           grace window passes the item is missed.

Exactly-once: the clock claims an item with one conditional UPDATE (`schedule_state` NULL -> 'claimed'). Only the
process whose UPDATE changes a row may take, so two clocks (threads, processes, hosts, SQLite or Postgres) cannot
fire the same item twice. If a clock dies after claiming, the claim stays; after `claim_lease` seconds the next
tick settles it from what the database shows (all cues fired -> fired, otherwise failed, with a note to check
the room). It is never retried, because a take may have reached the room.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from volt import logs
from volt.migrations import SchemaTooNewError
from volt.playout import HeldError, TakingError, hold_state, on_air, taking
from volt.store import Store
from volt.show_input import configured_actions
from volt.take import take_item

log = logging.getLogger("volt.clock")

DEFAULT_GRACE = 30.0  # seconds past scheduled_at in which a due item may still fire
DEFAULT_CLAIM_LEASE = 300.0  # seconds before another clock settles a claim that never finished
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_text(when) -> str:
    """An aware datetime or ISO-8601 string with an offset (or Z) -> the stored UTC text."""
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("scheduled time must carry a UTC offset; a naive time names no instant")
    return when.astimezone(timezone.utc).strftime(_FMT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _FMT).replace(tzinfo=timezone.utc)


def set_schedule(store: Store, item_id: str, when) -> str:
    """Schedule an item (or clear it with None). Resets any earlier outcome. Returns the stored UTC text."""
    text = None if when is None else to_utc_text(when)
    cur = store.conn.execute(
        """UPDATE item SET scheduled_at = ?, schedule_state = NULL, schedule_note = NULL,
                  schedule_claimed_at = NULL WHERE id = ?""",
        (text, item_id),
    )
    if cur.rowcount == 0:
        raise KeyError(item_id)
    store.conn.commit()
    return text


@dataclass
class Event:
    item_id: str
    show_id: str
    outcome: str  # fired | failed | missed | held | blocked
    note: str = ""


def _next_ready(store: Store, show_id: str) -> Optional[str]:
    """The item `go` would take next: first ready item after the on-air one, passing over killed ones."""
    current = on_air(store, show_id)
    floor = 0
    if current is not None:
        floor = store.conn.execute("SELECT position FROM item WHERE id = ?", (current,)).fetchone()["position"]
    for row in store.conn.execute(
        """SELECT id, status FROM item WHERE show_id = ? AND position > ? AND status IN ('ready', 'killed')
           ORDER BY position""",
        (show_id, floor),
    ).fetchall():
        if row["status"] == "ready":
            return row["id"]
    return None


def _transition(store: Store, item_id: str, expect, new: Optional[str], note: Optional[str] = None,
                claimed_at: Optional[str] = None) -> bool:
    """Atomic state change: succeeds only if schedule_state still equals `expect` (None = NULL)."""
    guard = "schedule_state IS NULL" if expect is None else "schedule_state = ?"
    params = [new, note, claimed_at, item_id] + ([] if expect is None else [expect])
    cur = store.conn.execute(
        f"""UPDATE item SET schedule_state = ?, schedule_note = ?, schedule_claimed_at = ?
            WHERE id = ? AND {guard}""",
        params,
    )
    store.conn.commit()
    return cur.rowcount == 1


def _failure_note(result) -> str:
    bad = [r for r in result.results if r.status != "fired"]
    if not bad:
        return "take did not advance"
    return "; ".join(f"cue {r.command} {r.status}" + (f": {r.detail}" if r.detail else "") for r in bad)


def settle_stale_claims(store: Store, now: datetime, claim_lease: float = DEFAULT_CLAIM_LEASE) -> list[Event]:
    """Settle claims a dead clock left behind. Never retries: the take may have reached the room."""
    events = []
    rows = store.conn.execute(
        """SELECT id, show_id, status, schedule_claimed_at FROM item WHERE schedule_state = 'claimed'"""
    ).fetchall()
    for row in rows:
        claimed = row["schedule_claimed_at"]
        if claimed is not None and now - _parse(claimed) < timedelta(seconds=claim_lease):
            continue
        unfired = store.conn.execute(
            "SELECT COUNT(*) FROM cue WHERE item_id = ? AND status != 'fired'", (row["id"],)
        ).fetchone()[0]
        if row["status"] in ("on air", "aired") and unfired == 0:
            new, note = "fired", "settled after a clock restart: item is on air with every cue fired"
        else:
            new, note = "failed", ("clock stopped mid-take; item is " + row["status"]
                                   + ", check the room and take by hand if needed")
        if _transition(store, row["id"], "claimed", new, note, claimed):
            events.append(Event(row["id"], row["show_id"], new, note))
    return events


def tick(
    store: Store,
    room,
    now: Callable[[], datetime] = utcnow,
    grace: float = DEFAULT_GRACE,
    fire_after_hold: bool = False,
    claim_lease: float = DEFAULT_CLAIM_LEASE,
    owner: str = "clock",
    **take_kw,
) -> list[Event]:
    """One pass over the schedule. `take_kw` goes to take_item (attempts, interval, sleep) so tests never sleep."""
    at = now()
    events = settle_stale_claims(store, at, claim_lease)
    stamp = lambda: now().astimezone(timezone.utc).strftime(_FMT)  # as-run comes from the same injected clock
    rows = store.conn.execute(
        """SELECT id, show_id, status, scheduled_at, schedule_state FROM item
           WHERE scheduled_at IS NOT NULL AND (schedule_state IS NULL OR schedule_state = 'held')
           ORDER BY scheduled_at, position"""
    ).fetchall()
    for row in rows:
        item_id, show_id = row["id"], row["show_id"]
        due = _parse(row["scheduled_at"])
        if due > at:
            continue  # not yet; never early
        state = row["schedule_state"]
        late = (at - due).total_seconds()

        busy = taking(store, show_id)
        if busy is not None:
            events.append(Event(item_id, show_id, "blocked", f"item {busy} is taking"))
            continue
        if row["status"] != "ready":
            note = f"item was {row['status']} when it came due"
            if _transition(store, item_id, state, "missed", note):
                events.append(Event(item_id, show_id, "missed", note))
            continue
        if late > grace:
            note = f"{int(late)}s late, past the {int(grace)}s grace window; not fired"
            if state == "held":
                note = "was held when due; " + note
            if _transition(store, item_id, state, "missed", note):
                events.append(Event(item_id, show_id, "missed", note))
            continue
        held = hold_state(store, show_id)
        if held is not None:
            note = f"show held: {held.reason or 'no reason given'}"
            if state is None:
                _transition(store, item_id, None, "held", note)
            events.append(Event(item_id, show_id, "held", note))
            continue
        if state == "held" and not fire_after_hold:
            note = "was held when due; not auto-fired after release (take it by hand)"
            if _transition(store, item_id, "held", "missed", note):
                events.append(Event(item_id, show_id, "missed", note))
            continue
        if _next_ready(store, show_id) != item_id:
            events.append(Event(item_id, show_id, "blocked", "an earlier item has not been taken"))
            continue

        if not _transition(store, item_id, state, "claimed", None, stamp()):
            continue  # another clock won the claim
        try:
            result = take_item(store, room, show_id, owner=owner, clock=stamp, **take_kw)
        except HeldError as exc:  # held between our check and the take: nothing changed, put it back
            _transition(store, item_id, "claimed", "held", str(exc))
            events.append(Event(item_id, show_id, "held", str(exc)))
            continue
        except TakingError as exc:  # a take started after we claimed: unclaim and wait
            _transition(store, item_id, "claimed", None, None, None)
            events.append(Event(item_id, show_id, "blocked", str(exc)))
            continue
        except Exception as exc:  # refused (not cleared, no span) or the room blew up
            store.conn.rollback()
            note = f"{type(exc).__name__}: {exc}"
            _transition(store, item_id, "claimed", "failed", note)
            events.append(Event(item_id, show_id, "failed", note))
            log.warning("scheduled take of %s failed: %s", item_id, note)
            continue
        if result.advanced and result.item_id == item_id:
            _transition(store, item_id, "claimed", "fired", None)
            events.append(Event(item_id, show_id, "fired"))
        else:
            note = _failure_note(result)
            _transition(store, item_id, "claimed", "failed", note)
            events.append(Event(item_id, show_id, "failed", note))
            log.warning("scheduled take of %s failed, rundown rewound: %s", item_id, note)
    return events


def schedule_rows(store: Store, show_id: str) -> list[dict]:
    """Scheduled items with their outcome, for the desk."""
    return [
        {"id": r["id"], "scheduledAt": r["scheduled_at"], "state": r["schedule_state"], "note": r["schedule_note"]}
        for r in store.conn.execute(
            "SELECT id, scheduled_at, schedule_state, schedule_note FROM item "
            "WHERE show_id = ? AND scheduled_at IS NOT NULL ORDER BY scheduled_at, position",
            (show_id,),
        ).fetchall()
    ]


def run_forever(store: Store, room, poll: float = 1.0, stop: Optional[threading.Event] = None, **kw) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            for ev in tick(store, room, **kw):
                log.info("%s %s %s", ev.outcome, ev.item_id, ev.note)
        except Exception:
            log.exception("clock tick failed")
            store.conn.rollback()
        stop.wait(poll)


def main(argv: list[str]) -> int:
    import argparse
    from volt.rooms import CoreVideoProRoom

    ap = argparse.ArgumentParser(prog="python -m volt.clock", description="Take scheduled rundown items on time.")
    ap.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"), help="SQLite path or postgres:// DSN (or VOLT_DB)")
    ap.add_argument("--once", action="store_true", help="one pass, then exit (cron, tests)")
    ap.add_argument("--grace", type=float, default=float(os.environ.get("VOLT_CLOCK_GRACE") or DEFAULT_GRACE),
                    help="seconds past the scheduled time an item may still fire (default 30)")
    ap.add_argument("--fire-after-hold", action="store_true",
                    help="fire an item that came due during a hold once released, if still inside the grace window")
    ap.add_argument("--claim-lease", type=float, default=DEFAULT_CLAIM_LEASE)
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required")
    url = os.environ.get("VOLT_ROOM_URL")
    if not url:
        ap.error("set VOLT_ROOM_URL (and VOLT_ROOM_TOKEN if the room needs one)")
    try:
        actions = configured_actions()
    except ValueError as exc:
        print(f"volt clock: {exc}", file=sys.stderr)
        return 78
    logs.setup("clock")
    room = CoreVideoProRoom(url, os.environ.get("VOLT_ROOM_TOKEN") or None)
    try:
        store = Store(args.db)
    except SchemaTooNewError as exc:  # a clock older than its database must not run
        print(f"volt clock: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG: `volt run` does not restart it
    kw = dict(grace=args.grace, fire_after_hold=args.fire_after_hold, claim_lease=args.claim_lease,
              show_input_actions=actions)
    try:
        if args.once:
            events = tick(store, room, **kw)
            for ev in events:
                print(f"{ev.outcome} {ev.item_id} {ev.note}".rstrip())
            return 1 if any(e.outcome == "failed" for e in events) else 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        log.info("clock started (grace %ss)", args.grace)
        run_forever(store, room, poll=args.poll, stop=stop, **kw)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
