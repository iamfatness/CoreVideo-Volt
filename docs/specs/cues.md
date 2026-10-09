# Cues

A cue is a CoreVideo action on the item: input, PTZ preset, graphic, audio snapshot, replay.

Stored cues arm when the item is taken. Dispatch sends the armed cue to the CoreVideo control API. A failed cue stays on the item and the rundown does not advance.

Acceptance: "camera 2, lower third" is one cue record. An uncleared span does not arm. Dispatch is the next build.
