# Playout

The clock plays the rundown to program and writes as-run in and out on the item.

Take, hold, and skip follow the item order. Secondary events are the cues on that item.

Acceptance: take advances the rundown and the as-run matches. A killed item is skipped and recorded as skipped. Restart resumes the on-air item.

Built in `control/volt/playout.py`: `go` takes the next item in order, writing the outgoing item's as-run out and the incoming item's as-run in from one clock read. A killed item that is passed over becomes `skipped` with no as-run. `skip` marks a ready item skipped and refuses the one on air. If the next item is not cleared, nothing changes. `on_air` finds the on-air item, which is how a restart resumes. `/rundown` returns `asRunIn` and `asRunOut` (whole seconds) and `asRunInPrecise` and `asRunOutPrecise` (milliseconds, migration 10).

Built: a schedule clock (see Schedule clock below) that takes items at their time. `go` alone moves the rundown and does not dispatch cues; the clock calls `take_item`.

## Take with cues

`control/volt/take.py` `take_item` advances the rundown, arms the new item's cues and dispatches them. If every cue lands, the item is on air with its as-run in written. If any cue fails or is unsupported, the rundown is rewound: the new item is ready again with no as-run in, the outgoing item is back on air with no as-run out, and passed-over killed items are killed again. Cues that already fired stay fired, because they happened in the room. The failed cue stays armed, and taking again resumes there.

## Hold

An operator holds the rundown so nothing advances, then releases it.

Hold is per show: `hold_reason` and `held_at` on the show row, held when `held_at` is set. `hold(store, show_id, reason, clock)` sets it; holding an already held show keeps the first reason and time. `release` clears it and returns whether the show was held. `hold_state` reads it.

While held, `go`, `advance` and so `take_item` raise `PlayoutError` before reading the rundown or the room. Nothing changes: no as-run, no status, no cue armed or dispatched. The on-air item stays on air. `skip` of a ready item still works, so the desk can fix the rundown during a hold. `on_air`, `kill`, `reorder`, and `attach` are unaffected. Release resumes with the next `go`. The hold is stored, so it survives a restart. Existing databases gain the two columns on open.

`/rundown` returns `{"hold": null | {"reason", "heldAt"}, "items": [...]}`. It is read-only. Hold and release have no endpoint until the screens are approved.

Not built: a hold that pauses the on-air item's own cues or media, and an auto-release.

## Schedule clock

`python -m volt.clock <sqlite path or postgres:// DSN> [--once] [--grace 30] [--fire-after-hold]` takes items at their scheduled time. It reads `VOLT_ROOM_URL` and `VOLT_ROOM_TOKEN` like `serve.py` and refuses to start without a room. `--once` does one pass and exits 1 if anything failed. Code is `control/volt/clock.py`; `tick(store, room, now=...)` takes an injectable `now()` returning an aware datetime, and extra keywords go to `take_item`, so tests never sleep.

Schema, on `item`: `scheduled_at`, `schedule_state`, `schedule_note`, `schedule_claimed_at`. There is no show-level start: the first item's `scheduled_at` is the show start. `set_schedule(store, item_id, when)` stores UTC `YYYY-MM-DDTHH:MM:SSZ`, accepts an aware datetime or an ISO string with an offset, and refuses a naive time (it names no instant, and on a DST fall-back night it names two). Everything is compared as an instant, so zone, DST and the machine's local time never matter.

For each due item with no outcome yet, one tick does one of:

| Outcome | Meaning | Stored in `schedule_state` |
|---|---|---|
| fired | `take_item` landed; item on air, as-run in from the injected clock | `fired` |
| failed | a cue failed or the take was refused (not cleared, no span, room error). The rundown is rewound exactly as `take_item` does and the reason is in `schedule_note`. The clock never retries; the desk takes by hand | `failed` |
| missed | too late: older than the grace window (default 30 s, `--grace` or `VOLT_CLOCK_GRACE`), or came due while held, or the item was no longer ready (killed, skipped, taken by hand) | `missed` |
| held | the show was held when the item came due | `held` |
| blocked | reported only: an earlier ready item has not been taken, so this is not the next item in order | none |

Not before its time: an item is never touched until `now >= scheduled_at`.

Exactly once: the clock claims an item with one conditional UPDATE (`schedule_state` NULL to `claimed`). Only the process whose UPDATE changes a row takes, so two clocks cannot fire an item twice, on SQLite or Postgres (tested with two threads on each). A clock that dies after claiming leaves `claimed`; after 300 s any clock settles it from the database: on air or aired with every cue fired is `fired`, anything else is `failed` with a note to check the room. A claim is never retried, because the take may have reached the room.

Hold: a held show does not advance. The due item is stored as `held` and reported `held`. After release it is not fired, even inside the grace window: the next tick marks it `missed` with the reason, so the desk sees it. `--fire-after-hold` changes that to fire inside the grace window. If no clock was running while the show was held, nothing recorded the hold, so on release an item still inside the grace window fires; the grace window is the only backstop.

Out-of-order schedules: take advances in order, so the clock only takes an item that is the next ready item (killed ones are passed over and recorded skipped, as `go` does). If a predecessor is still ready, the item is `blocked` and stays due; it fires if the predecessor is taken inside the grace window, otherwise it is `missed`. The clock never airs a predecessor on a successor's behalf.

Not built: recurring or relative ("after the previous item") schedules, a show-level start field, scheduling endpoints and UI (`serve.py` does not expose `scheduled_at`; `volt.clock.schedule_rows` reads it), a retry of a failed scheduled take. Not run: against a live core.

## Kill racing go

`rundown.kill` is one conditional write, `UPDATE item SET status = 'killed' WHERE id = ? AND status = 'ready'`, taken under the show's lock (the lock `go`, `take` and `skip` use), and returns whether it killed the item. A kill and a go on two connections or processes get one outcome: the go airs the item and the kill returns False (the operator route answers 409 `not_killable`), or the kill lands first and the go passes the item over as `skipped`. An item is never killed after it went on air, so a cue is never sent for a killed item; a kill that arrives after `advance` committed finds the item `on air` and is refused. Only a ready item can be killed: the library call no longer kills an on-air one. Not covered: killing the on-air item in order to cut it (that is a `go` to the next item); `attach` and `reorder` of an item while it is being taken are still last-writer-wins.
