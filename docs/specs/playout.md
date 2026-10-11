# Playout

The clock plays the rundown to program and writes as-run in and out on the item.

Take, hold, and skip follow the item order. Secondary events are the cues on that item.

Acceptance: take advances the rundown and the as-run matches. A killed item is skipped and recorded as skipped. Restart resumes the on-air item.

Built in `control/volt/playout.py`: `go` takes the next item in order, writing the outgoing item's as-run out and the incoming item's as-run in from one clock read. A killed item that is passed over becomes `skipped` with no as-run. `skip` marks a ready item skipped and refuses the one on air. If the next item is not cleared, nothing changes. `on_air` finds the on-air item, which is how a restart resumes. `/rundown` returns `asRunIn` and `asRunOut`.

Not built: a clock that fires cues on its own. `go` moves the rundown and does not dispatch cues.

## Take with cues

`control/volt/take.py` `take_item` advances the rundown, arms the new item's cues and dispatches them. If every cue lands, the item is on air with its as-run in written. If any cue fails or is unsupported, the rundown is rewound: the new item is ready again with no as-run in, the outgoing item is back on air with no as-run out, and passed-over killed items are killed again. Cues that already fired stay fired, because they happened in the room. The failed cue stays armed, and taking again resumes there.

## Hold

An operator holds the rundown so nothing advances, then releases it.

Hold is per show: `hold_reason` and `held_at` on the show row, held when `held_at` is set. `hold(store, show_id, reason, clock)` sets it; holding an already held show keeps the first reason and time. `release` clears it and returns whether the show was held. `hold_state` reads it.

While held, `go`, `advance` and so `take_item` raise `PlayoutError` before reading the rundown or the room. Nothing changes: no as-run, no status, no cue armed or dispatched. The on-air item stays on air. `skip` of a ready item still works, so the desk can fix the rundown during a hold. `on_air`, `kill`, `reorder`, and `attach` are unaffected. Release resumes with the next `go`. The hold is stored, so it survives a restart. Existing databases gain the two columns on open.

`/rundown` returns `{"hold": null | {"reason", "heldAt"}, "items": [...]}`. It is read-only. Hold and release have no endpoint until the screens are approved.

Not built: a hold that pauses the on-air item's own cues or media, and an auto-release.

