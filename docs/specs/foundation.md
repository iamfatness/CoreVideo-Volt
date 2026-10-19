# Foundation

The control plane is the system of record for MAM. Later MAM work (more span kinds, search, a second NLE, MOS) hangs on identity, bytes, ingest, playback, and an audit of who wrote what. This spec is that layer. Orchestration (a named source on Program) is a parallel track that shares the playout writer; it does not block MAM.

Frozen objects stay frozen: asset, essence, span, item, cue, version, job. This spec adds columns and one table. It does not add a sixth area.

## Why now

The session already mints, proxies, transcribes, marks, and publishes. A small team still cannot treat it as the archive: ingest copies on the HTTP thread, a token-gated proxy cannot play, two `serve` processes can both take, backup copies no media, and the docs still list work that is already in the tree. Building more Library features on that is how the MAM becomes two products.

## Goals

1. One writer for a show's playout, in the database, so a second `serve` or the clock cannot slip a take through.
2. A take does not hold any lock while it waits on the room.
3. Ingest is a job. The operator and a watch folder enqueue; the runner copies, probes, and checksums.
4. Authenticated media plays with Range requests. The operator token never appears in a URL.
5. A job can carry a JSON payload. Settings do not live in the idempotency key.
6. Every mutating operator call records an actor. One shared token is still the auth.
7. `volt` from a wheel finds its pages and schema. `/` is the operator desk.
8. A backup names every essence file the restore will need. An opt-in copy takes the local bytes.
9. Docs state what the tree does. Stale "next" lists are gone.

## Non-goals

- Per-user tokens, passwords, roles. Actor is a name on the call, not an account.
- Replacing `http.server`. TLS is a reverse-proxy recipe in operations, not a Volt TLS stack.
- vMix, OBS, MOS, Premiere import, hardware playout, archive tier.
- Rewriting `operator.js` or generating its timecode from Python. The existing twin test stays.
- Making `show-input.take` work without CoreVideo Pro #861. Volt keeps the handler and the evidence; the route action is the core's.
- Changing SQLite as the test default. An air machine with `VOLT_ROOM_URL` set gets a preflight warning on SQLite.

## Anti-goals

- A second rundown, a second asset ID, or a path on an item.
- Optimistic UI. Refusals stay `code` plus `reason`.
- Silent ingest of files outside `VOLT_INGEST_ROOTS`.
- Putting `VOLT_TOKEN` in a query string, log, or `<video src>`.

## Playout writer

`_SEQ` in `serve.py` only orders requests inside one process. Kill already decides with one conditional write. Take does not.

**Status `taking`.** `take_item` sets the next item to `taking` in the same transaction that leaves the previous item. `go` still sets `on air` (no room). Dispatch runs with no process lock and no show advisory lock. On every cue fired, the item becomes `on air` and as-run in is stamped. On a failed or unsupported cue, `rewind` returns the target to `ready` (as today).

While any item of the show is `taking`:

- `go`, `take`, `skip`, `kill` of a ready row, and the clock's take, refuse `409 taking` with the item id.
- Hold, release, attach to a *different* ready item, rights, and publish do not wait.
- Attach to the taking item stays refused (`not_attachable`).

`on_air()` ignores `taking`. Health and `/api/state` show the taking row as status `taking` so the desk is not blank during a ten-second poll.

Two `serve` processes and the clock then serialise on the row, not on a thread lock. `_SEQ` can remain as an in-process courtesy; it is not the contract.

`record.start` / `record.stop` keep `_REC` until a later pass. Recording is not playout.

## Ingest as a job

Today `ingest_file` copies, probes, and checksums on the caller, then writes an `ingest` job row the runner never claims.

**Enqueue.** `ingest.request(store, source, *, owner, source_key, show_name, camera)` resolves and allow-lists the path, mints or reuses the asset by `source_key`, and enqueues `type=ingest` with key `ingest:<source_key>` and a payload `{path, quarantine, owner, show_name, camera}`. It does not copy. HTTP `POST /api/ingest` returns `202` with `asset`, `minted`, `job`, `status: queued`. A replay of the same key returns the existing job.

**Handler.** The runner claims `ingest`. The handler is today's `ingest_file` body: space check, copy into quarantine, ffprobe, checksum, essence, `chain_after_wrap`. Parse failure completes as today (`quarantine`, job `failed: parse failed`). Missing file is `Permanent`. Disk low is `Defer`.

**Watch.** `volt watch` (also a runner periodic when `VOLT_WATCH=1`) lists regular files directly in each `VOLT_INGEST_ROOTS` folder (not recursive, ignore `*.part` and names starting with `.`). A file whose size and mtime have been stable for `VOLT_WATCH_STABLE_SECONDS` (default 5) is requested. Growing cards wait. `volt run` starts watch with the runner when the flag is set; it is not a fourth supervised process unless watch is run by hand.

The operator path field stays for a file that is already stable. It only enqueues.

## Media tickets

`<video>` and `<img>` cannot send `Authorization`. The page today downloads the whole proxy when a token is set.

**Ticket.** `GET /api/media-ticket?asset=<id>&kind=proxy|thumb` (token-gated) returns `{url, expires}`. `url` is the existing media path plus `?ticket=<hmac>`. HMAC-SHA256 of `asset|kind|exp` with `VOLT_TOKEN` (or a dedicated `VOLT_MEDIA_KEY` if set). Default TTL 120 s, max 3600. The ticket is accepted on that GET only, compared in constant time, and is not enough for POST. Range requests keep working: the ticket is on the URL, not the body.

Loopback with no token is unchanged: native `<video src>` as today.

The operator page uses the ticket URL for `<video>` and for `<img>` thumbs. The blob-download path remains as a fallback if the ticket route 404s (old server).

## Job payload

Migration 15: `job.payload TEXT` (JSON object or NULL). `Store.enqueue(..., payload=None)` writes it only on insert; a conflict leaves the existing payload. `Claimed.payload` is a dict or `{}`.

Shot detection keeps parsing settings from the key until a later change. New ingest jobs use payload. A later MAM job (conform, proxy refresh with options) uses payload from the first line.

## Actor and audit

Migration 16: table `audit` (`id`, `at`, `actor`, `action`, `target_kind`, `target_id`, `detail`). Append-only. Not `purge_audit` (that table is file destruction).

`X-Volt-Actor` on any POST, 1–100 chars `[A-Za-z0-9._:-]`, default `desk`. The existing `owner` body field on take and record is copied into the header if the header is absent. Serve writes one row after a successful mutation (and after a 409 that still changed nothing: no row). Actions are the route names: `take`, `go`, `ingest`, `publish`, `delete_asset`, …

`GET /api/audit?target=<id>` (token-gated, last 200) is enough for an operator to see who took an item. No UI in this spec.

## Install layout

`control/volt/session/` holds the operator pages (moved from `control/session/`). `control/volt/schema.sql` is the shipped schema (moved from `control/schema.sql`). `serve.ROOT` and `preflight.PACKAGE_ROOT` are `Path(volt.__file__).resolve().parent`. A wheel then contains the pages. Checkout layout matches the wheel.

`GET /` serves the operator page. The old search shell is removed. `/assets` and `/seek` stay as API for tests.

`pyproject.toml` `package-data` includes `session/**` and `schema.sql`.

## Backup of bytes

`volt backup` writes `media.json` next to the database dump: every closed essence `id, role, location, checksum` and every `done` version `id, backend, key, sha256`. Restore still does not invent files.

`volt backup --with-media <db> <dir>` copies local hi-res and proxy files whose paths are under `VOLT_PROXY_DIR`, `VOLT_QUARANTINE_DIR`, or `VOLT_PUBLISH_DIR` into `<dir>/media/`. Object storage is listed, not copied. `volt restore` does not move those files back; `docs/operations.md` says where to put them. `volt verify-storage` after restore is the check.

## Postgres on air

`volt check`: if `VOLT_ROOM_URL` is set and `VOLT_DB` is SQLite, status `warn` (`sqlite_on_air`: "the room is configured; Postgres is the store for a live show"). Tests and a laptop without a room stay quiet.

## Room track (parallel)

Not required to call the MAM foundation done.

- Keep `show-input.take` evidence as specified in [cues](cues.md). When CoreVideo Pro ships a route action, only `show_input_actions` changes.
- Until a live Program take is recorded in [rooms](../rooms.md), the operator cue picker keeps labelling it unverified.
- `record.arm` and `audio.snapshot` stay `unsupported`. Do not stub a handler with no evidence.
- If the core exposes an operation id on take, match it; until then the two-poll rule stays and the caveat in cues.md stays.

## Docs truth

Rewrite [areas](../areas.md), [start-here](../start-here.md), [README](../../README.md) "Next" lines, and [roadmap](../roadmap.md) so they name this spec. [product-readiness](../product-readiness.md) stays as a dated audit; it is not the current gap list.

## License

No `LICENSE` until the owner names an SPDX id. `NOTICE` lists ffmpeg, faster-whisper, and the optional SDKs as dependencies the installer must honour. Operations already says this.

## Proven / not this spec

Proven after the plan's tests: taking-state races on SQLite and Postgres, ingest handler idempotency, ticket HMAC accept/reject/expiry, audit row on take, wheel layout via `volt check` against a copied install tree, backup `media.json` shape.

Not this spec: a live Program take, a restore drill on production media, Task Scheduler on a real air box, TLS on the wire.
