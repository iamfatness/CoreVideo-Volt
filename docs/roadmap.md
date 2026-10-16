# Roadmap

## In the tree

MAM mint and search. Storage quarantine and wrap. Proxy transcode. Rundown and stored cues. Publish versions and URLs on the item. Job runner. Operator API and UI. Speech worker at scale. Playout clock and as-run. S3, Azure Blob and GCS object storage, and Postgres. Cue dispatch into CoreVideo, including `show-input.take`. Live proof against a real CoreVideo Pro core of record, lower third and stream (the stream run of 2026-10-16 is in [rooms.md](rooms.md)). Shot spans, detection, transcript marking and NLE export (xmeml and EDL; the import into a real NLE is still a manual, owner-deferred check, see [shot-spans.md](specs/shot-spans.md)).

## Next

The gaps between "passes the contract tests" and "a small broadcast team runs it every day". Ranked, with the evidence and the reasoning in [product-readiness.md](product-readiness.md). Severity is for a team running a live show.

1. Backup and restore for SQLite, Postgres and object storage, with a restore drill. (Critical, M)
2. One start command, and service files with restart on crash for Windows and Linux. (Critical, M)
3. A health endpoint, and stuck-job and queue-depth visibility. (High, S)
4. Logging to files with rotation; no more silent access log. (High, S)
5. Real packaging: `pyproject.toml`, entry points, optional extras, a pinned install. (High, M)
6. Disk-space guards and cleanup: thumbnail cache, quarantine, temp and part files. (High, M)
7. Asset deletion and retention that actually run (the `delete` job has no handler). (High, M)
8. An audit trail of who did what, then per-user tokens and roles. (Medium, L)
9. Schema version skew guard between server, runner and database. (Medium, S)
10. Operator install and runbook docs, separate from the developer specs. (Medium, M)
11. A license file and third-party notices. (Medium, S; blocks any outside install)
12. A version number, changelog and release process. (Medium, S)
13. CI: Windows plus Postgres, a packaging smoke test, and a scheduled live-service check. (Medium, M)
14. TLS guidance or a supported reverse-proxy recipe for LAN use. (Medium, S)

## After the session is the daily tool

MOS export of the rundown. Hardware playout adapter. Archive tier with restore. A second NLE. Recurring and relative playout schedules.
