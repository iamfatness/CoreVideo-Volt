# Data model

Frozen early. Features can wait. These objects cannot.

## Asset

Immutable ID. Owner. Retention class. Legal hold. Rights window. Show, room, and camera it was born in. Created when the recording starts or the card is quarantined, not when the transcode finishes.

## Essence

A located file. Checksum. Codec. Frame rate. Timecode start. Audio layout. Role: hi-res, proxy, mezzanine, deliverable. Many essences per asset. Proxy and hi-res are essences, not new assets. A growing file is an essence with `open = true` until wrap and checksum.

## Span

In and out on source timecode. A marker, a transcript word range, a subclip, or a selected highlight. Search hits return spans. The rundown holds span IDs, not file paths.

## Item

A rundown row. Script text. Ordered spans. Cue list. Status: ready, next, on air, aired, killed. As-run in and out written by the clock.

## Cue

A thing the room must do when the item fires. Input on CoreVideo, PTZ preset, graphic, audio snapshot, replay. Cue IDs hang on the item. They are not a second rundown.

## Version

Master, mezzanine, house file, OTT ladder, social cut. Each points at the asset and the span it was cut from. Loudness and caption state live here.

## Job

Idempotent. Type: ingest, proxy, ASR, conform, publish. Replaying a failed job must not mint a second asset.

## External ref

The same asset as a MOS objID, a Premiere media path, a CoreVideo recording, a publish URL. The ref is not a copy.

## Rights

Every asset has an owner, a retention class, and a legal hold flag on day one. Publish refuses a span that is not cleared. Deletion is a job, not a filesystem rm.
