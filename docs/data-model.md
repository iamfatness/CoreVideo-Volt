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

## Storage

Same objects on SQLite (default) and Postgres. Timestamps are ISO-8601 UTC text to callers on both. See `docs/architecture.md` for what is proven on Postgres.

## Rights

Every asset has an owner, a retention class, and a legal hold flag on day one. Publish refuses a span that is not cleared or whose asset is on legal hold; so does air. A legal hold does not stop marking, detection or an NLE export, because the export leaves the original source untouched. Deletion is a job, not a filesystem rm: it is a soft delete (`asset.deleted_at`), then a purge after a retention window that removes only files Volt owns and records them in `purge_audit`. A legal hold, an attached rundown item, an active (not withdrawn) publish version, an open recording or retention class `permanent` all refuse it. See `docs/specs/retention.md`. Backup and restore: `docs/specs/backup.md`.
