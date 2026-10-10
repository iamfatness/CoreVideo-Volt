# Playout

The clock plays the rundown to program and writes as-run in and out on the item.

Take, hold, and skip follow the item order. Secondary events are the cues on that item.

Acceptance: take advances the rundown and the as-run matches. A killed item is skipped and recorded as skipped. Restart resumes the on-air item.

Built in `control/volt/playout.py`: `go` takes the next item in order, writing the outgoing item's as-run out and the incoming item's as-run in from one clock read. A killed item that is passed over becomes `skipped` with no as-run. `skip` marks a ready item skipped and refuses the one on air. If the next item is not cleared, nothing changes. `on_air` finds the on-air item, which is how a restart resumes. `/rundown` returns `asRunIn` and `asRunOut`.

Not built: hold, and a clock that fires cues on its own. `go` moves the rundown and does not dispatch cues.

## Take with cues

`control/volt/take.py` `take_item` advances the rundown, arms the new item's cues and dispatches them. If every cue lands, the item is on air with its as-run in written. If any cue fails or is unsupported, the rundown is rewound: the new item is ready again with no as-run in, the outgoing item is back on air with no as-run out, and passed-over killed items are killed again. Cues that already fired stay fired, because they happened in the room. The failed cue stays armed, and taking again resumes there.
