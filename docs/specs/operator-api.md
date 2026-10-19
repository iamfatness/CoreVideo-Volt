# Operator API

POST endpoints in `control/volt/serve.py` that give the operator UI hands. They only call functions that already exist (`advance`, `hold`, `take_item`, `start_recording`, `publish_files`, and so on). The existing GET routes and the `/rundown` shape (`{hold, items}`) are unchanged.

## Contract

- JSON in, JSON out. `Content-Type: application/json` is required on every POST. A body is optional; an empty body is `{}`. Unknown fields are refused.
- Success: HTTP 200, `{"ok": true, ...}`, always including the changed state (`rundown` in the `/rundown` shape for show and item actions, `asset` for rights and record actions, `versions` for publish). `POST /api/ingest` is `202` with `status: queued` (the runner copies).
- Refusal: `{"ok": false, "error": {"code": "<stable code>", "reason": "<human text>"}, ...}`. A refusal changes nothing. Clients switch on `code`; `reason` is for display.
- One store connection per request. Sequencing writes (go, take, skip, kill, reorder, record start/stop) run one at a time in the server process. Hold, release, rights and publish do not wait on them, so HOLD lands while a take is waiting on the room.
- No CORS headers are ever sent. JSON-only POSTs mean a browser page on another origin cannot send one without a preflight, which is never answered.
- Request body limit 64 KiB (`413 too_large`). Chunked bodies are refused (`411`).

## Auth

| Bind | Token | Behaviour |
| --- | --- | --- |
| loopback (`127.0.0.0/8`, `::1`, `localhost`) | unset | No auth. Requests whose `Host` is not loopback get `403 bad_host` (DNS rebinding guard). |
| loopback | `VOLT_TOKEN` set | Bearer token required. |
| anything else | unset or under 16 chars | The server refuses to start. |
| anything else | `VOLT_TOKEN`, 16+ chars | Bearer token required. |

`Authorization: Bearer <VOLT_TOKEN>`, compared in constant time (`hmac.compare_digest`). Missing or wrong: `401 unauthorized` with `WWW-Authenticate: Bearer`, checked before routing. When a token is set it also guards the GET data routes (`/assets`, `/rundown`, `/seek`, `/api/audit`, `/api/media-ticket`); only the static shell at `/` stays open.

`X-Volt-Actor` on any POST, 1–100 characters matching `[A-Za-z0-9._:-]`, default `desk`. If the header is absent, body `owner` (take, record, ingest) is used. Invalid: `422 bad_actor` before the action. A 2xx that changed or confirmed state writes one `audit` row (`actor`, `action`, `target_id`). A 401/404/409 writes none. `GET /api/audit?target=<id>` returns the last 200 rows for that target (newest first).

Environment: `VOLT_HOST` (default `127.0.0.1`), `VOLT_PORT` (default 8765), `VOLT_TOKEN`, `VOLT_MEDIA_KEY` (optional HMAC key for media tickets; otherwise `VOLT_TOKEN`), `VOLT_DB`, `VOLT_INGEST_ROOTS` and `VOLT_QUARANTINE` (ingest, see Show building), `VOLT_OUT` (publish output directory, default `control/out`), `VOLT_ROOM_URL` and `VOLT_ROOM_TOKEN` (the CoreVideoPro room; unset means room-backed routes answer `503 no_room`).

## Endpoints

IDs are path segments. All are `POST`.

| Path | Body | Does | Refusals |
| --- | --- | --- | --- |
| `/api/shows/{show}/go` | none | Advance the rundown (`advance`). Returns `target`, `previous`, `passed`, `over`, `rundown`. No room, no cues. | 404 `not_found`; 409 `held`; 409 `taking` (another item of the show is taking; body `item`); 409 `not_cleared`; 409 `no_media` (next item has no span) |
| `/api/shows/{show}/take` | `owner?` | Set the next item `taking`, dispatch cues with no process lock, then `on air` or rewind. Returns `advanced`, `item`, `results`, `rundown`. | 404; 409 `held`; 409 `taking`; 409 `not_cleared`; 409 `no_media`; 503 `no_room`; 409 `cue_failed` or `cue_unsupported` (see below); 422 |
| `/api/shows/{show}/hold` | `reason?` (string, 500 max) | Hold the rundown. Holding twice keeps the first hold (`alreadyHeld: true`). | 404; 422 |
| `/api/shows/{show}/release` | none | Release the hold. Releasing a show that is not held is a 200 with `wasHeld: false`. | 404 |
| `/api/shows/{show}/reorder` | `items`: every item id of the show, once each; `version?` | Set the order, atomically (one transaction under the show's lock). With `version` (the `orderVersion` the page last saw) it is conditional: see [Show building](#show-building). Returns `rundown`, `orderVersion`. | 404; 422 `bad_input` (missing, duplicate or foreign ids); 409 `stale_order`; 409 `order_locked` |
| `/api/items/{item}/skip` | none | Mark a ready item skipped. | 404; 409 `taking`; 409 `not_skippable` (on air or already past) |
| `/api/items/{item}/kill` | none | Kill a ready item. The asset stays. | 404; 409 `taking`; 409 `not_killable` |
| `/api/items/{item}/attach` | `span` | Attach a span to a ready item. One transaction under the show's lock ending in `UPDATE ... WHERE status = 'ready'`, so an attach racing a take is either applied before the item airs or refused; it never changes the media of an item that is on air. | 404 (item or span); 409 `not_attachable`; 422 |
| `/api/record/start` | `owner?`, `camera?`, `show?` | Ask the room to record; mint only once output is observed (`start_recording`). Returns `asset`, `minted`, `essence`. | 503 `no_room`; 502 `room_refused` (nothing minted); 422 |
| `/api/record/stop` | `asset` | Stop and wrap only on completed + finalized (`finish_recording`). Returns `checksum`, `asset`. | 404; 503; 502 `not_wrapped` (nothing wrapped); 422 |
| `/api/assets/{asset}/cleared` | `cleared`: boolean | Set the cleared flag. | 404; 422 (must be a real boolean) |
| `/api/assets/{asset}/legal-hold` | `held`: boolean | Set the legal hold. A held asset cannot air or publish. It can still be marked, detected and exported to an NLE (the export leaves the source untouched). | 404; 422 |
| `/api/assets/{asset}/delete` | none | Soft delete (tombstone) the asset: hidden from the library, search and the operator lists, every file kept, undone by `/restore`. The runner purges it after the retention window (`docs/specs/retention.md`). Returns `alreadyDeleted`, `deletedAt`, `asset`. Refused when the asset is on legal hold, attached to a rundown item, has an active (not withdrawn) publish version, is still recording, or has retention class `permanent`: `409 delete_refused` with `reasons` (a list) at the top level of the body, nothing changed. | 404; 409 `delete_refused`; 422 |
| `/api/assets/{asset}/restore` | none | Undo a delete. | 404 (also once purged); 409 `not_deleted`; 422 |
| `/api/versions/{version}/unpublish` | none | Withdraw one publish version: the row is kept with status `withdrawn` and `withdrawnAt`, it leaves the rundown's item URLs and the operator's published URL, and an audit row is written (`purge_audit`, kind `version`). The object Volt stored for it is then deleted through the storage seam, only if the version recorded a backend and key, the key is under the asset's own prefix (Volt wrote it; anything else is `storage: "not_owned"` and untouched), and no other active version uses the same key (`kept_shared`). Returns `version`, `alreadyWithdrawn`, `storage` (`deleted`, `already_deleted`, `none`, `not_owned`, `kept_shared`, `pending_on_air`), `location`. Idempotent. Refused while an item using the version's span is on air (that version is live): `409 on_air` with `items`; or while a writer is rendering it: `409 publish_busy`. If the storage delete fails the version is already withdrawn: `502 storage_failed` with `withdrawn: true`, and calling again retries only the delete. Publishing the span again brings a withdrawn version back (new render, new object). Unknown body fields are `422`. | 404; 409 `on_air`, `publish_busy`; 502 `storage_failed`; 422 |
| `/api/spans/{span}/publish` | none | Render house, OTT and social files (`publish_files`) into `VOLT_OUT`. Returns `versions`. | 404; 409 `not_cleared`; 409 `publish_busy` (another writer holds a version; nothing was rendered by this call); 502 `publish_failed` (versions list shows which kind failed; it did record the failure) |
| `/api/exports/nle` | `format`, `spans`, `media?`, `name?`, `start_tc?`, `dry_run?`, `skip_blocked?` | Build an xmeml or EDL file from span IDs (`volt/nle.py`). Returns `content`, `sha256`, `spans`, `warnings`. See [Export](#export-shot-spans-slice-6). | 404; 409 `essence_open`, `no_proxy`, `proxy_mismatch`, `not_local`, `outside_source`; 422 `mixed_rates`, `unsupported_rate`, `bad_input`; 500 `bad_path_map` |

### Take with a failing cue

`take_item` rewinds the rundown if any cue does not land: the previous item is back on air and the target is ready again. Fired cues stay fired (they happened in the room) and the failed cue stays armed, so taking again resumes at it. The response is `409` with:

```json
{"ok": false,
 "error": {"code": "cue_failed", "reason": "cue stream.start did not land (...); the rundown is back where it was"},
 "item": "<item id>",
 "cue": {"cue": "<cue id>", "command": "stream.start", "status": "failed", "detail": "..."},
 "results": [{"cue": "...", "command": "overlay.take", "status": "fired", "detail": ""}, {"...": "failed"}],
 "rundown": {"hold": null, "items": []}}
```

`cue_unsupported` is the same shape for a command that has no verified adapter.

## Show building

Endpoints that let an operator make a show without a developer. The rules live in `control/volt/building.py`; the handlers in `serve.py` (a separate block) read the body, call it, and map its refusals. Same auth, JSON, unknown-field and body-size rules as every POST. Every success carries `rundown` (the `/rundown` shape, unchanged) and `orderVersion`. A refusal writes nothing.

Each edit is one transaction under the show's lock (the lock `go`, `take`, `skip` and `kill` also take, in any process, on SQLite and Postgres) and ends in a write that restates its condition (`WHERE status = 'ready'`), so an edit that races a take gets one outcome. They do not take the in-process sequencing lock, so editing stays possible while a take waits on the room. Only a `ready` item can be edited; once an item is on air or has aired, the as-run log is its record.

| Path | Body | Does | Refusals |
| --- | --- | --- | --- |
| `POST /api/shows` | `title` (1 to 200), `id?` | Create a show. Returns `show: {id, title}`, `existed`. | 409 `id_conflict` (the id is another show's); 422 |
| `POST /api/shows/{show}/items` | `slug` (1 to 200), `script?` (20000 max), `id?` | Append an item (status `ready`, no media). Returns `item`, `existed`. | 404; 409 `id_conflict`; 422 |
| `POST /api/items/{item}/rename` | `slug?`, `script?` (at least one) | Rename. | 404; 409 `not_editable` (not ready); 422 |
| `POST /api/items/{item}/remove` | none | Delete an item that has not aired, with its cues. Allowed for `ready`, `killed`, `skipped` with no as-run. `kill` keeps the item in the list as killed; this takes it out. Returns `removed`. | 404; 409 `not_removable` (on air or aired) |
| `POST /api/items/{item}/attach` | `span` | (existing) attach a clip. | see above |
| `POST /api/shows/{show}/delete` | none | Delete a show that has not played: its `ready`, `killed` and `skipped` items and their cues, then the show row, in one transaction under the show lock (the one go and take hold). Spans, assets and versions are not touched. Refused when any item is on air or has aired (status `on air` or `aired`, or an as-run time): `409 show_not_deletable` with `items` (id and status of each), nothing changed. Returns `deleted`, `removedItems`, `removedCues`. A repeat is `404`. | 404; 409 `show_not_deletable`; 422 |
| `POST /api/items/{item}/detach` | none | Take the span off a ready item. A repeat is a 200 with `wasAttached: false`. | 404; 409 `not_attachable` |
| `POST /api/items/{item}/cues` | `command`, `room?` (only `corevideo.pro`), `payload?` (2000 max) | Add a cue at the end of the item's cues. Returns `cue` (`id, room, command, payload, status, verified, failure`) and, for a command with no verified adapter, `warning`. | 404; 409 `not_editable`; 422 `bad_command` (not in `cues.COREVIDEO_COMMANDS`; body lists `commands`), `bad_input` |
| `POST /api/cues/{cue}/remove` | none | Remove a cue that has not fired, from a ready item. Returns `removed`, `item`. | 404; 409 `not_editable`, `cue_fired` |
| `GET /api/items/{item}/cues` | none | The item's cues in order, plus `commands`. | 404 |
| `GET /api/cue-commands` | none | `commands: [{room, command, verified}]`. `verified` is true when `dispatch.HANDLERS` has an adapter (today `show-input.take`, `overlay.take`, `record.start`, `stream.start`). A cue for an unverified command (`audio.snapshot`, `record.arm`) can be stored and armed, but a take stops at it with `cue_unsupported`, so the UI warns when it is added. | |
| `POST /api/ingest` | `path`, `owner?`, `show?`, `camera?` | Enqueue ingest of one local file (`ingest.request`): mint or reuse the asset, queue `type=ingest`. Returns `202` with `asset`, `essence` (empty until the runner copies), `minted`, `status: queued`, `job`, `jobs`, `assetState`. The runner copies, probes, checksums, and chains proxy and transcript. A replay of the same file (same path, size and modification time) is the same asset and job with `minted: false`. Disk low does not refuse the POST: the job waits (`Defer`) and no quarantine copy is made. Ingest never sets `cleared`. | 403 `path_not_allowed`; 404 `not_found` (missing file or a folder); 503 `ingest_disabled`, `engine_missing` (no ffprobe); 422 |

**Ids and retries.** `id` on create is optional and caller-chosen (letters, digits, `_ . : -`, up to 128). The same id with the same content is a 200 with `existed: true` and changes nothing, so a retry after a lost response is safe; the same id with other content is `409 id_conflict`. Without `id` the server mints one.

**Reorder is conditional.** `orderVersion` (in `GET /api/state` and in every building response) is a token over the show's item ids in order. `POST .../reorder` with `version` set to the token the page last saw runs one transaction: if the token no longer matches (anyone added, removed or moved an item) it is `409 stale_order` and nothing moves; otherwise it validates and writes. Of N operators holding one token, exactly one wins (a test races four, on SQLite and Postgres). `version` is optional so older clients keep working, but without it the order is set unconditionally. The ids must be exactly the show's items. Items that are on air or have aired keep their relative order and every ready item must stay below them (`409 order_locked`): `go` walks forward from the item on air, so a ready item moved above it could never air.

**Ingest allow-list.** Like the media proxy, ingest only touches files the server was told about, but a file to ingest is not registered yet, so the allowed place is a folder: `VOLT_INGEST_ROOTS`, folders separated by the OS path separator (`;` on Windows, `:` elsewhere). `path` must be absolute; it is resolved (`..` and symlinks followed) and must lie inside a root, compared on path segments (`/inbox-evil` is not inside `/inbox`). Anything else is `403 path_not_allowed` with a fixed message, the same whether or not the file exists and without echoing it. With no root configured every ingest is `503 ingest_disabled`. The quarantine copy goes to `VOLT_QUARANTINE`, default `VOLT_OUT/quarantine`.

**As-run to the millisecond.** Migration 13 adds `item.as_run_in_precise` and `as_run_out_precise` (text, `2026-10-10T20:00:09.123Z`). `advance` writes the second-precision `as_run_in/out` and the millisecond value from one read of the clock (the second is the millisecond text's prefix); a rewind clears both. Each item in the `/rundown` shape gains `asRunInPrecise` and `asRunOutPrecise`; the old `asRunIn` and `asRunOut` are unchanged. A row stamped before the migration, or by a test's injected clock, has no millisecond value, and the precise field then repeats the second so clients can read it without a branch. `advance(..., precise_clock=)` lets a test supply one.

`attach` and `detach` are both one transaction under the show lock with a conditional write, so neither can change the media of an item that went on air (a threaded attach-versus-take test runs on SQLite and Postgres). Neither takes the in-process sequencing lock. There is no soft delete of a show: delete removes it.

## Export (shot spans, slice 6)

`POST /api/exports/nle`. Stateless: nothing is stored and no media is read or moved; the response carries the file. Same auth, JSON and body-size rules as every POST above (64 KB covers 500 span IDs). Design and decisions: [shot-spans.md](shot-spans.md). **The file formats are UNVERIFIED against a real NLE** until the manual import check there is recorded.

Body (unknown fields are `422 bad_input`):

| Field | Meaning |
| --- | --- |
| `format` | `xmeml` (Final Cut Pro 7 XML, version 4) or `edl` (CMX3600). Required. |
| `spans` | Span IDs, 1 to 500, in the order they should be cut. The same span may appear twice. Required. |
| `media` | `hi-res` (default) or `proxy`. |
| `name` | Sequence or title name, 1 to 200 characters. Default `Volt selects`. |
| `start_tc` | Sequence start. Default `01:00:00:00` (`;` when the essence is drop-frame). A `;` makes the sequence drop-frame. |
| `dry_run` | `true` returns verdicts and a preview and no file. Default false. |
| `skip_blocked` | Accepted (a bool) and ignored: nothing blocks an export any more. Kept so older clients are not refused with 422. |

Success: `{ok, format, filename, mime, content, sha256, spans, warnings, durationFrames, rate, startTc, dropFrame}`. `spans` has one entry per requested span: `{id, status: "ready", notCleared?, legalHold?, note?}`. A span on a not-cleared asset is `ready` with `notCleared: true`, one on a legal-hold asset with `legalHold: true`, and both flags when both apply; the `note` says the export is allowed because it leaves the original source untouched and that publish and air stay blocked (one combined note for both), and the response `warnings` carry one sentence per condition. (Owner rulings, `shot-spans.md` open question 2: neither legal hold nor a missing clearance blocks an internal edit; clearance gates delivery.) `sha256` is over the UTF-8 bytes of `content`. `rate` is the exact rate (`"25"`, `"30000/1001"`). A dry run returns `dryRun: true`, `spans`, `warnings`, `preview` (the first 20 lines) and the same totals, and no `content`. Both kinds of response also carry `paths` (`[{asset, location, path, mapped}]`: where each exported essence is as stored and as written) and `pathMapSet` (whether `VOLT_EXPORT_PATH_MAP` is set); `filename` is on dry runs too. Refusals about the whole selection carry their extras at the top level of the body (`rates` for `mixed_rates`). Real runs log the span IDs and `sha256` to the server's stderr.

Refusals (nothing is written): 404 `not_found` (a span); 409 `essence_open` (the chosen essence is still being written); 409 `no_proxy`; 409 `proxy_mismatch` (the proxy records a different rate or start timecode than the hi-res); 409 `not_local` (the location is a URL or not an absolute path); 409 `outside_source` (the span lies outside its essence); 422 `mixed_rates` (body has `rates`); 422 `unsupported_rate`; 422 `bad_input`; 500 `bad_path_map`. Rights never refuse an export.

`VOLT_EXPORT_PATH_MAP` (server environment, read on each request): `D:\iso=\\nas\iso;E:\cam=/Volumes/cam`. A leading prefix of the stored media location is rewritten (segment boundary, longest prefix first, Windows prefixes case-insensitive) when building the file's media path. Unset means paths are written as stored.

## Read endpoints (shot spans, slice 2)

All are `GET`, behind the same auth and host rules as every other data route (bearer token when set; `403 bad_host` on a non-loopback `Host` with no token). Errors use the same `{ok: false, error: {code, reason}}` shape. Unknown query parameters, a repeated parameter and bad values are `422 bad_input`; an unknown asset or span is `404 not_found`. Nothing here writes control-plane state.

| Path | Returns |
| --- | --- |
| `/api/assets/{asset}/spans?kind=&from=&to=&limit=&after=` | `{ok, assetId, rate, start, durationFrames, open, cleared, legalHold, hasProxy, pendingFrames, lastRun, spans, next}`. `kind` is a comma list of `shot`, `clip`, `transcript` (default `shot,clip`, so words never flood the panel). `from` and `to` are timecode labels on the essence's timebase (drop-frame `;` honoured); a span is returned when it overlaps `[from, to)`. Order is `frame_in, id`. `limit` 1 to 500 (default 200). `next` is an opaque cursor for `after`, or `null` on the last page. `422 bad_timecode` for a label that does not exist; `from` and `to` need an asset with a hi-res essence that has a frame rate. |
| `/api/assets/{asset}/transcript?from=&to=&limit=&after=` | `{ok, assetId, words, next, pendingFrames}`: transcript spans in a window, same ordering, window rule and cursor. `limit` 1 to 1000 (default 500). |
| `/api/assets/{asset}/proxy` | The proxy file, streamed. Honours one `Range` (`206` with `Content-Range`; `bytes=a-b`, `a-`, `-n`). A range past the end, malformed, or on an empty file is `416 bad_range` with `Content-Range: bytes */<size>`. Several ranges, a non-`bytes` unit and any request with `If-Range` get the whole file (`200`), which the spec allows. `404 no_proxy` when there is no proxy row or its file is gone. Accepts `?ticket=`. |
| `/media?path=<location>` | The old shell's player. `path` must equal, as a string, the `location` of a registered proxy essence; anything else (an unregistered file, a hi-res, `..`, a missing parameter's value) is `404 not_found`. The string is only compared, never opened. Same `Range` behaviour. |
| `/api/spans/{span}/thumb` | `image/jpeg`, 320 px wide, `Cache-Control: private, max-age=3600`. Cached at `VOLT_OUT/thumbs/<asset>/<span>-<frame>.jpg`; made on first request with one ffmpeg seek on the proxy (frame is `frame_in + max(2, 10% of length)`, kept inside the span). `404 no_proxy`; `502 thumb_failed` (ffmpeg missing, non-zero exit, or no image); `504 thumb_timeout` (15 s limit). A failure or timeout leaves no file behind, so the next request tries again. Accepts `?ticket=`. |
| `/api/media-ticket?asset=&kind=proxy\|thumb&ttl?` | `{ok, url, expires}`. HMAC ticket on the existing media path (`?ticket=`). Default TTL 120 s, max 3600. Signing key is `VOLT_MEDIA_KEY` or `VOLT_TOKEN`. Loopback with no token does not need a ticket. `503 no_media_key` when neither key is set. |
| `/api/audit?target=` | Last 200 audit rows for that `target_id` (newest first): `id, at, actor, action, target_kind, target_id, detail`. |

Span JSON: `id, assetId, kind, origin, label, tcIn, tcOut, frameIn, frameOut, text, runId, blockedBy, usedBy: {items, versions}`. `blockedBy` is `null`, `"not cleared"` or `"legal hold"`, same words as `/api/state`. `lastRun` is the newest `shot_run` row (`job, essence, engine, engineVersion, threshold, minFrames, shotCount, createdAt`) or `null`.

`pendingFrames` counts spans whose `frame_in`/`frame_out` are not yet backfilled (migration 9 leaves old rows NULL until the `span-frames` job runs). Such spans cannot be ordered or windowed, so these routes leave them out and say how many.

The proxy and thumb GETs accept `?ticket=` in place of `Authorization`. The ticket is compared in constant time, is not enough for POST, and Range requests keep working. A stolen or expired ticket, or a ticket for another asset or kind, is `401`.

Limits of these routes: a browser `<video src>` or `<img src>` cannot send a bearer header. The operator page uses the ticket URL for video and thumbs; the blob-download path remains as a fallback if the ticket route 404s. The proxy route reads the file as registered; a proxy still being written (open) is served as far as it has been written. Thumbnails assume the proxy starts at the hi-res's timecode and rate, as the spec does; that is not checked.

## Write endpoints (shot spans, slice 4)

All are `POST`, under the contract and auth above. Every rule lives in `volt/shots.py`, not in the handler or the page; a refusal writes nothing. Spans carry no rights and marking is allowed on any asset (not cleared, legal hold, still recording): an operator has to be able to log footage to decide what to clear. Attach, publish and air keep their gate on the asset. Responses carry the span as in the read API, including `blockedBy`.

| Path | Body | Does | Refusals |
|---|---|---|---|
| `/api/assets/{asset}/spans` | `kind` (`shot` or `clip`), `tc_in`, `tc_out` (timecode labels; `;` or `:` accepted, stored in the essence's own style), `label?` (string or null, 200 max) | Create a span with `origin: manual`. Returns `span`. | 404 `not_found`; 422 `bad_input` (bad kind, missing field, out not after in, label, an asset with no hi-res or no frame rate), `bad_timecode` (does not parse, or a drop-frame label that does not exist), `outside_source` (before the start, or past the end when the length is known); 409 `overlaps_shot` (shots only) |
| `/api/spans/{span}/trim` | `tc_in?`, `tc_out?` (at least one) | Move the in and/or out of a shot or clip. Returns `span`. | 404; 422 `bad_input`, `bad_timecode`, `outside_source`; 409 `overlaps_shot`, `span_in_use` (an item or a version uses it), `not_editable` (a transcript span) |
| `/api/spans/{span}/label` | `label` (required key; string, 200 max, or null to clear) | Rename any span. Allowed while the span is in use. Returns `span`. | 404; 422 `bad_input` |
| `/api/spans/{span}/delete` | none | Delete a shot or clip. Returns `{ok, deleted}`. | 404; 409 `span_in_use`, `not_deletable` (a transcript span) |

A shot's overlap check and its insert, and a trim's checks and its update, are one step (per-asset lock plus the guards in the statement itself), so two operators marking one asset cannot both pass the overlap rule; a test races six overlapping creates and exactly one wins, on SQLite and Postgres.

### Clips from transcript words (slice 5)

`POST /api/assets/{asset}/spans` also takes a second form, in place of `tc_in` and `tc_out`:

```
{"kind":"clip","from_words":["<first word span id>","<last word span id>"],
 "handle_in":0,"handle_out":18,"snap":"shot","label":"..."}
```

The client sends word IDs and never a time; the server loads both words and builds the range (`shots.create_from_words`). `tc_in` is the first word's in minus `handle_in` frames; `tc_out` is the last word's out plus `handle_out` frames. `snap: "shot"` (the only value, or null/absent for none) then widens each end to the shot that holds it: the in back to that shot's start, the out forward to that shot's end; it never narrows, and an end that lies in no shot (a gap) stays put. The snap is applied after the handles, so a handle that carries the out into the next shot makes the clip end at the end of that shot. `text` is the words from the first to the last in transcript order (by `frame_in`, then id), joined by single spaces; `origin` is `transcript`; `kind` must be `clip`. A clip may overlap anything, so there is no `overlaps_shot` here. Handles are whole frames from 0 to 10000 (bool, float, string, negative are `422 bad_input`).

| Refusal | When |
|---|---|
| 404 `not_found` | the asset, or either word ID, does not exist |
| 409 `not_transcript` | a word belongs to another asset, or is not a `transcript` span (a clip, a shot, or a word a speech re-run re-kinded to `clip` because an item or version uses it) |
| 409 `frames_pending` | a word's `frame_in`/`frame_out` are still NULL (the `span-frames` backfill has not placed it); the transcript read leaves such words out, so the page cannot offer them. A word in the middle of the range that is unplaced is left out of `text`, not refused |
| 422 `bad_input` | `from_words` is not exactly two non-empty strings; the last word is before the first; `kind` is not `clip`; `tc_in` or `tc_out` sent with `from_words`; `handle_in`, `handle_out` or `snap` sent without it; a bad handle or snap |
| 422 `outside_source` | after handles and snap the range starts before the source or ends past it. Refused, not clamped |

Same rights as slice 4: marking is allowed on any asset and the response says `blockedBy`. Tests: `control/tests/test_shot_words.py` (SQLite and live Postgres).

`GET /api/state` assets now also carry `hasProxy` (the Library's asset picker sorts on it). They also carry `jobs`: the asset's unfinished jobs only, `[{type, status, error}]` with status `queued`, `running` or `failed` (a finished job is not listed), which the Library's Ingest panel reads for progress.

### Detect, split, merge, indexing (slice 4b)

Same contract and auth as above; rights are never consulted (a legal hold or an uncleared asset is detected, split and merged like any other).

| Path | Body | Does | Refusals |
|---|---|---|---|
| `/api/assets/{asset}/shots/detect` | `threshold?` (above 0, below 1; default 0.13), `min_frames?` (1 to 10000; default 12) | Queue a `shots` job; a runner does the work. Returns `{ok, alreadyDone, job}`; `job` is `{id, status, attempt, error, updatedAt, ageSeconds, threshold, minFrames}`. The same settings are the same job (a finished run: `alreadyDone: true`, nothing queued; a queued or running one: that job). A failed run is queued again. | 404 `not_found`; 409 `essence_open` (wrap first), `detect_busy` (a run with other settings is queued or running; nothing queued); 422 `bad_input`; 503 `engine_missing` (no ffmpeg on the server) |
| `/api/spans/{span}/split` | `at` (timecode, strictly inside the shot) | Two shots tile the old one; the first keeps the id and label, the second is new and unnamed; both `origin: manual`. Returns `{ok, spans: [first, second]}`. | 404; 422 `bad_input`, `bad_timecode`; 409 `span_in_use`, `not_editable` (not a shot), `indexing` (the shot's frames are not backfilled yet), `conflict` (it changed meanwhile) |
| `/api/spans/{span}/merge` | `with` (the shot that starts where this one ends) | The first takes the other's out and the other is deleted, in one transaction. Returns `{ok, span, removed}`. | 404; 422 `bad_input`; 409 `not_adjacent` (a gap, itself, another asset, wrong order), `span_in_use` (either), `not_editable`, `indexing`, `conflict` |
| `/api/assets/{asset}/span-frames` | none | Queue the backfill when spans lack frame columns (`pendingFrames > 0`). Returns `{ok, queued, pendingFrames, job}`; `queued: false` and `job: null` when nothing is pending. One job per asset, ever: if it finished and some labels cannot be placed, `pendingFrames` stays above 0 and nothing is queued again; a failed job is queued again. | 404 |

`GET /api/assets/{asset}/spans` also returns `detect` (the newest detection job as above, with `threshold` and `minFrames`, or `null`) and `indexing` (the `span-frames` job, or `null`). `ageSeconds` is seconds since the job row last changed (for a running job, since it was claimed), computed on the server. A queued job with `error` set is waiting for a retry or for the recording to be wrapped.

## Other codes

`401 unauthorized` (also `ticket_invalid` / expired media ticket), `403 bad_host`, `404 not_found` (also unknown routes, and `GET /rundown?show=<id>` for an unknown show; the body is the same `{ok: false, error: {code, reason}}` shape), `411 length_required`, `413 too_large`, `415 unsupported_media_type`, `409 taking` (go, take, skip, kill, clock while an item of the show is taking), `409 overlaps_shot`/`span_in_use`/`not_editable`/`not_deletable`/`conflict`/`not_adjacent`/`detect_busy`/`indexing`/`essence_open` (shot spans), `503 engine_missing`, `503 no_media_key`, `416 bad_range`, `422 bad_input` (also `bad_timecode`, `outside_source`, `bad_actor`), `502 thumb_failed`, `504 thumb_timeout`, `500 internal` (no detail leaks; the traceback goes to the server's stderr).

## Known limits

- Atomicity is per underlying function. `take_item` commits the advance, then the cues, then a rewind on failure, so a crash of the server process mid-take can leave the rundown advanced with cues unsent. The rewind-on-failure path is covered; crash-mid-take is not.
- Two `serve` processes and the clock serialise on item status `taking`, not on a thread lock. `_SEQ` only orders requests inside one process.
- Dispatch waits on the room with no show lock. Hold, release, attach to a *different* ready item, rights and publish do not wait. A second take or go is `409 taking`.
