# Cues

A cue is a room, a command, and a payload on the item.

CoreVideo Pro is the first room. Its commands come from the typed control contract in CoreVideoPro: show input take, overlay take, audio snapshot, record arm, record start, stream start. vMix and OBS are other rooms behind the same record.

A take arms the cue. Dispatch sends it to that room's adapter and waits for the acknowledgement, then the snapshot. A failed cue stays armed and the rundown does not advance.

See [rooms](../rooms.md).
