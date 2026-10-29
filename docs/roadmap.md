# Roadmap

## In the tree

MAM mint and search. Storage quarantine and wrap. Proxy transcode. Rundown and stored cues. Publish versions and URLs on the item. Job runner. Operator API and UI. Speech worker at scale. Playout clock and as-run. S3, Azure Blob and GCS object storage, and Postgres. Cue dispatch into CoreVideo, including `show-input.take`. Live proof against a real CoreVideo Pro core of record, lower third and stream (the stream run of 2026-10-16 is in [rooms.md](rooms.md)). Shot spans, detection, transcript marking and NLE export (xmeml and EDL; the import into a real NLE is still a manual, owner-deferred check, see [shot-spans.md](specs/shot-spans.md)).

MAM foundation ([spec](specs/foundation.md), [plan](plan-foundation.md)): job payload column; ingest as a runner job (HTTP only enqueues); watch folder for stable files; item status `taking` (dispatch holds no lock; two servers cannot both take); signed media tickets; append-only audit of operator writes; operator pages and schema inside the `volt` package (`/` is the desk); backup writes a media inventory with optional local copy; docs and NOTICE; SQLite-plus-room is a preflight warning.

The operations layer in [product-readiness.md](product-readiness.md) (start command, health, logs, packaging, disk floor, purge, version skew, numbered 0.1.0) is in the tree.

## Next

Room track: [next-bundle](next-bundle.md). Default pre-take is `input.assign` then `scene.route.set` (CoreVideo Pro #861 / [PR #953](https://github.com/iamfatness/CoreVideoPro/pull/953)). The live Program take is recorded (2026-10-28) and `show-input.take` is live-proven on the desk.

Per-user tokens and roles. TLS as a supported reverse-proxy recipe. MOS export of the rundown. Hardware playout adapter. Archive tier with restore. A second NLE. Recurring and relative playout schedules. License SPDX (owner).
