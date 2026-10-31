# Changelog

All notable changes to Volt. Versions follow [Semantic Versioning](https://semver.org/); the number lives in
`VERSION` and `control/volt/__init__.py` (a test keeps them equal). Before 0.1.0 there were no versions: the
history below is rebuilt from the git log and grouped by feature, newest work first within each group.

## [Unreleased]

- Operator UI redesign, slice 2: the Show screen. The rundown gains length and front-time columns, a total and a signed drift readout, the state language on every row and cue, and a divider before Kill; TAKE carries the take icon and sits apart from HOLD and Advance; the Operator workspace hides the build forms and widens the rundown. `/api/state` media entries gain `frames` and `rate` (additive).

- Operator UI redesign, slice 1: spacing, radius and type tokens; a 38-icon SVG sprite and `icon()`; one state language (`stateChip`); and a new shell with a workspace switcher (Producer, Editor, Operator), an icon rail, a user pill and a footer. Front end only: no route, schema or key changed. The empty-state glyph helper in `operator.js` is renamed `stateGlyph`.

- MOS export of the rundown: `GET /api/shows/{show}/mos` and `volt mos export <show> [--out file]` render a show as a MOS `roCreate` XML message (items as stories, the attached span as the story's item, durations and timebase from the span, cues optional and namespaced). Read-only; nothing is sent. Structurally follows MOS 2.8.5 as documented; unvalidated against an NCS or the MOS XSD/DTD ([mos.md](docs/specs/mos.md)).

- Per-user tokens with two roles, operator and admin (migration 17, table `volt_user`, `volt user add|list|disable|enable|rotate`). Tokens are stored as sha256 hashes and shown once; the audit actor is the token's user; operators get `403 forbidden_role` on delete, unpublish, re-detect and user management. `VOLT_TOKEN` keeps working as an admin named `desk`. A users-only server may bind beyond loopback once an enabled admin exists. Every POST route is classified in `ROUTE_ROLES`, and a test fails on an unclassified one. New: `GET /api/whoami`, `GET/POST /api/users...`. See [operations](docs/operations.md#users-and-tokens).

- `deploy/postgres/compose.yaml`: a local Postgres 16 container for Volt, with setup and a recorded run in [operations](docs/operations.md).

- [docs/tls.md](docs/tls.md): a supported TLS reverse-proxy recipe for Caddy and nginx (Volt on loopback, token required, 64 KiB body cap, Range passthrough, HSTS). Run through both with an internal certificate; no code change was needed. Linked from operations and product-readiness gap 14.

- `show-input.take` is live-proven: a recorded Program take against CoreVideo Pro beta-2026-10-26 (`bench/live_take_check.py`, [rooms.md](docs/rooms.md)); the cue picker drops "(not live-proven)" for it.

- The operator desk treats a `taking` item as the current row (`GET /api/state` names it; TAKE/GO wait). `volt watch --once` persists fingerprints so a later scan can enqueue; a brand-new file still waits for `VOLT_WATCH_STABLE_SECONDS`.
- Docs list the finished foundation work as in the tree. `NOTICE` lists ffmpeg, faster-whisper, and the optional SDKs; it is not a Volt license.
- `volt check` warns `sqlite_on_air` when `VOLT_ROOM_URL` is set and `VOLT_DB` is SQLite.
- Unpublish a publish version (`POST /api/versions/{id}/unpublish`, migration 14): withdrawn versions are kept as history and no longer block deleting the asset.
- Delete a show that has not played (`POST /api/shows/{id}/delete`).
- Attach is now one transaction under the show lock with a conditional write, so it cannot land on an item that went on air.

## [0.1.0] - 2026-10-16

The first numbered build. It adds the operations layer (everything under "Running Volt") on top of the
control plane that had been built since 2026-10-09.

### Running Volt

- `volt` command: `serve`, `runner`, `clock`, `run` (all three supervised), `check` (preflight), `version`.
  `python -m volt.serve`, `volt.runner` and `volt.clock` still work. `serve` now has real arguments
  (`--host`, `--port`, `--db`, `--help`); before, `--help` started a server.
- `pyproject.toml` with no required dependencies and extras `speech`, `s3`, `azure`, `gcs`, `postgres`.
- Logging to stderr and a rotating file per process, with secrets redacted; opt-in access log.
- `GET /health` (open liveness), `GET /api/health` and `GET /metrics` (behind the operator token): database
  and schema version, runner heartbeat, queue depth, stuck jobs, as-run, disk, storage.
- Disk-space floor (`VOLT_MIN_FREE_MB`) that refuses new ingest, proxy and thumbnail work, and a capped,
  self-pruning thumbnail cache. Stale temp files from a killed encode are swept.
- Migration 12: `runner_heartbeat` table.
- Service files for systemd and Windows (Task Scheduler), and `docs/operations.md`.

### Product readiness

- Audit of what is missing before a small team can run Volt daily (`docs/product-readiness.md`), roadmap
  rewrite, shot-spans rulings.

### Dress rehearsal and live proofs

- Frame rate and audio layout recorded on ingest and wrap; ingest queues its follow-on jobs.
- Live checks against a real core: record start/stop/wrap and lower third, the cue dispatcher, and a live
  stream into a local SRT sink (sender state read from `outputSenderSession`).

### Shot spans and the NLE panel

- Spec and mockups (2026-10-12), then slices 1 to 6: migration 9, `volt/shots.py`, read API with proxy
  ranges and thumbnails, detection job with `shot_run`, split and merge, the Library screen with marking,
  transcript marking (from words, handles, snap), NLE export (xmeml, CMX3600 EDL) and the Export tab.
- NLE export no longer refuses not-cleared spans; legal hold no longer blocks export; Publish lists clips
  and shots, not words.

### Operator UI and API

- Operator POST API: go, take, hold, skip, record, rights, publish, with a bearer token (2026-10-11).
- `/operator` page wired to it, read-only `/api/state`, FakeRoom harness, axe-clean; Space takes.
- Export tab: dry run preview, refusals in words, path map, blob download.

### Job runner, clock and speech

- Job runner: claim, lease, retry with backoff, chaining from wrap; heartbeat, one proxy build and one
  transcript at a time (job limits).
- Schedule clock: takes items at `scheduled_at`, once, honouring hold.
- Speech worker: faster-whisper engine, vocabulary and corrections, provenance, consent gate; windowed
  long-file ASR with resume, VAD hallucination guard, model reuse.
- `volt/timecode.py`: drop-frame and exact-rate timecode.

### Storage and publishing

- Storage seam with local and S3 backends (SSE-KMS, stable public URLs, role credentials, scheduled URL
  refresh), then Azure Blob and Google Cloud Storage.
- Deliverables: real house, OTT and social files for a span; retry only failed versions.
- Publish an approved span as versions on the same asset.

### Database

- Postgres through a small connection adapter; hardening: no leaked transactions, pooling, race fixes,
  versioned migrations (`schema_version`), CI against Postgres 16.
- Cross-process race fixes: proxy builds, transcripts, kill versus go, publish claims.

### Control plane and playout

- Store (asset, essence, span, job), watch-folder ingest, library search, rundown, cues, dispatch to a
  room, playout with as-run times, hold, take with cues and rewind on cue failure.
- Room adapter and live record flow: mint on observed output, wrap on observed finalize.
- Show-input take handler with program-frame evidence.

### Project

- Product plan, five-area documentation, brand pass (the four-slash mark), CI on Linux and Windows.
