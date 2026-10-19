# Roadmap

## In the tree

MAM mint and search. Storage quarantine and wrap. Proxy transcode. Rundown and stored cues. Publish versions and URLs on the item. Job runner. Operator API and UI. Speech worker at scale. Playout clock and as-run. S3, Azure Blob and GCS object storage, and Postgres. Cue dispatch into CoreVideo, including `show-input.take`. Live proof against a real CoreVideo Pro core of record, lower third and stream (the stream run of 2026-10-16 is in [rooms.md](rooms.md)). Shot spans, detection, transcript marking and NLE export (xmeml and EDL; the import into a real NLE is still a manual, owner-deferred check, see [shot-spans.md](specs/shot-spans.md)).

## Next

The operations layer in [product-readiness.md](product-readiness.md) (start command, health, logs, packaging, disk floor, purge, version skew, numbered 0.1.0) is in the tree. What is left before more MAM features is [foundation](specs/foundation.md), built as [plan-foundation.md](plan-foundation.md):

1. Job payload column.
2. Ingest as a runner job; HTTP only enqueues.
3. Watch folder for stable files.
4. Item status `taking`; dispatch holds no lock; two servers cannot both take.
5. Signed media tickets so a token-gated proxy still Ranges.
6. Append-only audit of operator writes (one shared token, an actor name).
7. Operator pages and schema inside the `volt` package; `/` is the desk.
8. Backup writes a media inventory; optional local copy.
9. Docs match the tree; NOTICE; SQLite+room is a preflight warning.

A live Program take (`show-input.take`) stays a parallel room track. It does not block 1–9. It still needs CoreVideo Pro #861.

## After the session is the daily tool

Per-user tokens and roles. TLS as a supported reverse-proxy recipe. MOS export of the rundown. Hardware playout adapter. Archive tier with restore. A second NLE. Recurring and relative playout schedules. License SPDX (owner).
