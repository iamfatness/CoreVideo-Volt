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

import json
import logging
import os
import re
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
    """Schedule an item (or clear it with None). Resets any earlier outcome and any relative mode. Returns the stored UTC text."""
    text = None if when is None else to_utc_text(when)
    cur = store.conn.execute(
        """UPDATE item SET scheduled_at = ?, schedule_state = NULL, schedule_note = NULL,
                  schedule_claimed_at = NULL, schedule_mode = NULL, schedule_offset_seconds = NULL WHERE id = ?""",
        (text, item_id),
    )
    if cur.rowcount == 0:
        raise KeyError(item_id)
    store.conn.commit()
    return text


def set_relative(store: Store, item_id: str, offset_seconds: int) -> None:
    """Make an item due `offset_seconds` after the previous item has run its planned length. The first item has no previous."""
    row = store.conn.execute("SELECT show_id, position FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    earlier = store.conn.execute("SELECT 1 FROM item WHERE show_id = ? AND position < ?", (row["show_id"], row["position"])).fetchone()
    if earlier is None:
        raise ValueError("the first item has no previous item to follow")
    store.conn.execute(
        """UPDATE item SET scheduled_at = NULL, schedule_state = NULL, schedule_note = NULL, schedule_claimed_at = NULL,
                  schedule_mode = 'after_previous', schedule_offset_seconds = ? WHERE id = ?""", (int(offset_seconds), item_id))
    store.conn.commit()


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return _parse(str(value)[:19] + "Z")


def planned_seconds(store: Store, item_id: str) -> float:
    """The planned length of an item's clip in seconds; an item with no clip, or one whose length cannot be counted, is zero."""
    from volt import shots
    from volt.operator_state import _span_length
    row = store.conn.execute(
        """SELECT s.asset_id, s.tc_in, s.tc_out, s.frame_in, s.frame_out FROM item i JOIN span s ON s.id = i.span_id WHERE i.id = ?""",
        (item_id,)).fetchone()
    if row is None:
        return 0.0
    frames, rate = _span_length(shots.try_facts(store, row["asset_id"]), row)
    if frames is None or rate is None:
        return 0.0
    n, _, d = str(rate).partition("/")
    return frames * int(d or 1) / int(n)


def materialise_relative(store: Store) -> None:
    """Give every `after_previous` item its due time once the item before it has started: that item's as-run start, plus its
    planned length, plus the offset. Killed and skipped items are passed over. The normal claim and grace rules then apply."""
    pending = store.conn.execute(
        """SELECT id, show_id, position, schedule_offset_seconds FROM item
           WHERE schedule_mode = 'after_previous' AND scheduled_at IS NULL AND schedule_state IS NULL AND status = 'ready'""").fetchall()
    for row in pending:
        prev = store.conn.execute(
            """SELECT id, status, as_run_in FROM item WHERE show_id = ? AND position < ? AND status NOT IN ('killed', 'skipped')
               ORDER BY position DESC LIMIT 1""", (row["show_id"], row["position"])).fetchone()
        if prev is None or prev["status"] not in ("on air", "aired") or not prev["as_run_in"]:
            continue
        due = _utc(prev["as_run_in"]) + timedelta(seconds=planned_seconds(store, prev["id"]) + (row["schedule_offset_seconds"] or 0))
        store.conn.execute(
            "UPDATE item SET scheduled_at = ? WHERE id = ? AND scheduled_at IS NULL AND schedule_state IS NULL",
            (to_utc_text(due), row["id"]))
    store.conn.commit()


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


# ---- recurring shows --------------------------------------------------------------------------------------------
# A rule is {"freq": "daily"|"weekly", "days": [0-6, Monday is 0] (weekly only), "at": "HH:MM", "tz": "<IANA zone>"}. It has no end
# date: it repeats until the rule is removed. The clock, not the HTTP server, makes the next occurrence: a fresh copy of the show,
# 24 hours ahead, with its first item scheduled. Wall-clock time in the zone: a time that does not exist (spring forward) moves
# forward, an ambiguous one (fall back) takes the first.
RECUR_AHEAD = timedelta(hours=24)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_recurrence(rule) -> dict:
    """The rule normalised, or ValueError saying what is wrong."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not isinstance(rule, dict):
        raise ValueError("the rule must be an object")
    extra = set(rule) - {"freq", "days", "at", "tz"}
    if extra:
        raise ValueError("unknown rule field: " + ", ".join(sorted(extra)))
    freq = rule.get("freq")
    if freq not in ("daily", "weekly"):
        raise ValueError("freq must be daily or weekly")
    at = rule.get("at")
    if not isinstance(at, str) or not _HHMM.match(at):
        raise ValueError("at must be a time of day such as 20:00")
    tz = rule.get("tz")
    if not isinstance(tz, str) or not tz:
        raise ValueError("tz is required (an IANA zone such as America/New_York)")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone: {tz}") from None
    out = {"freq": freq, "at": at, "tz": tz}
    days = rule.get("days")
    if freq == "weekly":
        if (not isinstance(days, list) or not days or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
            raise ValueError("weekly needs days: a list of weekday numbers, Monday 0 to Sunday 6")
        out["days"] = sorted(set(days))
    elif days is not None:
        raise ValueError("days only applies to a weekly rule")
    return out


def set_recurrence(store: Store, show_id: str, rule) -> None:
    """Set or clear (None) a show's recurrence. A show that is itself a clone cannot recur."""
    row = store.conn.execute("SELECT recurrence_of FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise KeyError(show_id)
    if row["recurrence_of"] is not None:
        raise PermissionError("this show is a repeat of another; set the rule on the original")
    text = None if rule is None else json.dumps(validate_recurrence(rule), sort_keys=True)
    store.conn.execute("UPDATE show SET recurrence = ? WHERE id = ?", (text, show_id))
    store.conn.commit()


def next_occurrence(rule: dict, after: datetime) -> datetime:
    """The first occurrence at or after `after` (UTC), in the rule's zone."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(rule["tz"])
    hh, mm = (int(x) for x in rule["at"].split(":"))
    start = after.astimezone(zone).date() - timedelta(days=1)
    for n in range(0, 10):
        day = start + timedelta(days=n)
        if rule["freq"] == "weekly" and day.weekday() not in rule["days"]:
            continue
        local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone, fold=0)
        when = local.astimezone(timezone.utc)
        if when >= after:
            return when
    raise ValueError("no occurrence found")


def _clone_show(store: Store, template_id: str, title: str, occ: datetime, day: str) -> Optional[str]:
    """Copy the template's items (ready, no as-run), cues and slot into a new show. None when another clock got there first."""
    from volt.store import _id, _now
    new_id = _id()
    with store.transaction():
        slot = store.conn.execute("SELECT slot_seconds FROM show WHERE id = ?", (template_id,)).fetchone()["slot_seconds"]
        cur = store.conn.execute(
            """INSERT INTO show (id, title, created_at, slot_seconds, recurrence_of, occurrence_on) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (recurrence_of, occurrence_on) DO NOTHING""",
            (new_id, f"{title} {day}", _now(), slot, template_id, day))
        if cur.rowcount != 1:
            return None
        items = store.conn.execute(
            "SELECT id, position, slug, script, span_id, scheduled_at, schedule_mode, schedule_offset_seconds, status FROM item "
            "WHERE show_id = ? ORDER BY position", (template_id,)).fetchall()
        first = next((r for r in items if r["status"] not in ("killed", "skipped")), None)
        base = _utc(first["scheduled_at"]) if first is not None and first["scheduled_at"] else None
        for r in items:
            if r["status"] in ("killed", "skipped"):
                continue
            item_id = _id()
            if first is not None and r["id"] == first["id"]:
                when = to_utc_text(occ)
            elif r["scheduled_at"] and base is not None:
                when = to_utc_text(_utc(r["scheduled_at"]) - base + occ)   # keep its offset from the first item
            else:
                when = None
            store.conn.execute(
                """INSERT INTO item (id, show_id, position, slug, script, status, span_id, created_at, scheduled_at,
                                     schedule_mode, schedule_offset_seconds) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (item_id, new_id, r["position"], r["slug"], r["script"], r["span_id"], _now(), when,
                 r["schedule_mode"], r["schedule_offset_seconds"]))
            for c in store.conn.execute("SELECT position, room, command, payload FROM cue WHERE item_id = ? ORDER BY position", (r["id"],)).fetchall():
                store.conn.execute(
                    "INSERT INTO cue (id, item_id, position, room, command, payload, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'stored', ?)",
                    (_id(), item_id, c["position"], c["room"], c["command"], c["payload"], _now()))
    return new_id


def materialise_recurring(store: Store, now: datetime) -> list:
    """Make the next occurrence of every recurring show that is within 24 hours of starting. Idempotent, and safe with two clocks."""
    from zoneinfo import ZoneInfo
    made = []
    for t in store.conn.execute("SELECT id, title, recurrence FROM show WHERE recurrence IS NOT NULL AND recurrence_of IS NULL").fetchall():
        try:
            rule = validate_recurrence(json.loads(t["recurrence"]))
            occ = next_occurrence(rule, now)
        except (ValueError, TypeError):
            log.warning("show %s has an unusable recurrence rule; skipped", t["id"])
            continue
        if occ - now > RECUR_AHEAD:
            continue
        day = occ.astimezone(ZoneInfo(rule["tz"])).date().isoformat()
        new = _clone_show(store, t["id"], t["title"], occ, day)
        if new is not None:
            made.append(new)
    return made


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
    materialise_relative(store)
    materialise_recurring(store, at)
    if room is None or not room:   # no room configured: clone recurring shows, but take nothing
        return events
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
    from volt import roomreg

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
    if not roomreg.configured():
        ap.error("set VOLT_ROOM_URL (and VOLT_ROOM_TOKEN if the room needs one)")
    try:
        actions = configured_actions()
    except ValueError as exc:
        print(f"volt clock: {exc}", file=sys.stderr)
        return 78
    logs.setup("clock")
    room = roomreg.from_env()  # a registry; take and dispatch route each cue to its own room
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
