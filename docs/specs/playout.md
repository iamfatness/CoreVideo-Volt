# Playout

The clock plays the rundown to program and writes as-run in and out on the item.

Take, hold, and skip follow the item order. Secondary events are the cues on that item.

Acceptance: take advances the rundown and the as-run matches. A killed item is skipped and recorded as skipped. Restart resumes the on-air item.

Built in `control/volt/playout.py`: `go` takes the next item in order, writing the outgoing item's as-run out and the incoming item's as-run in from one clock read. A killed item that is passed over becomes `skipped` with no as-run. `skip` marks a ready item skipped and refuses the one on air. If the next item is not cleared, nothing changes. `on_air` finds the on-air item, which is how a restart resumes. `/rundown` returns `asRunIn` and `asRunOut`.

Not built: hold, and a clock that fires cues on its own. `go` moves the rundown and does not dispatch cues.
