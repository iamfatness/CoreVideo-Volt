# Operator API

POST endpoints in `control/volt/serve.py` that give the operator UI hands. They only call functions that already exist (`advance`, `hold`, `take_item`, `start_recording`, `publish_files`, and so on). The existing GET routes and the `/rundown` shape (`{hold, items}`) are unchanged.

## Contract

- JSON in, JSON out. `Content-Type: application/json` is required on every POST. A body is optional; an empty body is `{}`. Unknown fields are refused.
- Success: HTTP 200, `{"ok": true, ...}`, always including the changed state (`rundown` in the `/rundown` shape for show and item actions, `asset` for rights and record actions, `versions` for publish). `POST /api/ingest` is `202` with `status: queued` (the runner copies).
- Refusal: `{"ok": false, "error": {"code": "<stable code>", "reason": "<human text>"}, ...}`. A refusal changes nothing. Clients switch on `code`; `reason` is for display.
- One store connection per request. Go, take, skip, kill and reorder decide under the show's lock in the database, so they are ordered across every `serve` process, not only within one. Record start and stop hold a lease row (`exclusive_lease`) for the room; a second caller gets 409 `record_busy` instead of waiting. Hold, release, rights and publish do not wait on them, so HOLD lands while a take is waiting on the room.
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

`X-Volt-Actor` on any POST made with the legacy `VOLT_TOKEN` (or on an open loopback server), 1–100 characters matching `[A-Za-z0-9._:-]`, default `desk`. If the header is absent, body `owner` (take, record, ingest) is used. Invalid: `422 bad_actor` before the action. A 2xx that changed or confirmed state writes one `audit` row (`actor`, `action`, `target_id`). A 401/404/409 writes none. `GET /api/audit?target=<id>` returns the last 200 rows for that target (newest first).

### Per-user tokens and roles

Besides `VOLT_TOKEN` (an admin named `desk`), `Authorization: Bearer <token>` may be a per-user token made by `volt user add`
(table `volt_user`, only a sha256 stored). The user has one role: `operator` or `admin`. Operators can call every route
except those marked admin in `ROUTE_ROLES` (`serve.py`): `POST /api/assets/{id}/delete`, `/api/shows/{id}/delete`,
`/api/spans/{id}/delete`, `/api/versions/{id}/unpublish`, `/api/assets/{id}/shots/detect`, and the user routes below. A
per-user token's audit actor is its user name; `X-Volt-Actor` and body `owner` do not change it.

| Route | Role | Notes |
|---|---|---|
| `GET /api/whoami` | any | `{ok, name, role, legacy, groups, effectiveRole, via, displayName}` plus `csrf` for a cookie session only. `via` is `legacy`, `token` or `session`; `role` and `effectiveRole` are the same value (direct role raised by groups). |
| `POST /api/session/start` | viewer, bearer only | `{}` -> `{ok, csrf}` and `Set-Cookie`. `401 bearer_required` without a user token (a cookie cannot mint a session), `409 not_a_user` for the legacy `VOLT_TOKEN`, `409 public_url_required` off loopback without `VOLT_PUBLIC_URL`. Audited as `login`. |
| `POST /api/session/end` | viewer | Ends the caller's cookie session and clears the cookie (`{ok, ended}`; `ended` is false for a bearer). Audited as `logout`. |
| `POST /api/users/{name}/end-sessions` | admin | `{ok, ended}`: how many sessions were removed. Audited as `session_end`. |
| `GET /auth/providers` | none | `{ok, providers: [{id, label}]}`; `404` when `VOLT_OIDC_CONFIG` is not set. **Unvalidated against a real provider.** |
| `GET /auth/login?provider=&next=` | none | `302` to the provider with PKCE (S256), `state`, `nonce`; sets the `volt_flow` cookie (HttpOnly, `SameSite=Lax`, `Path=/auth`, 10 minutes). `next` must be a same-site path, else `/`. `422 bad_provider`, `422 bad_input`, `503 oidc_unavailable`. Unvalidated against a real provider. |
| `GET /auth/callback?code=&state=` | none | Success: `302` to `next` with the session cookie and `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Refusal: a fixed-text HTML error page (`400` flow_invalid, provider_error, oidc_token_invalid, oidc_alg_unsupported; `403` not_provisioned, user_disabled; `503` oidc_unavailable), no session, audited `login_failed`. Success is audited `login`. Unvalidated against a real provider. |
| `POST /api/groups/{name}/claims` | admin | `{provider, value, remove?}`: map a provider group-claim value to this group (`404`, `422 bad_input`). Audited `group_claims`. `GET /api/groups` lists each group's `claims`. |
| `POST /api/users/{name}/identity` | admin | `{email}` (null clears): the address sign-in links by. `409 email_taken` (compared without case), `404`, `422 bad_input`. Audited `user_identity`. |
| `GET /api/shows/{show}/access` | operator who can view the show | `{ok, show, access: [{subject_kind, subject, level}]}`; `403 forbidden_show` for anyone not on a restricted show (the answer never lists who is), `404`. |
| `POST /api/shows/{show}/access` | admin | `{subject_kind: user\|group, subject, level: view\|edit\|run\|null}` (null removes): `422 bad_input`, `422 no_such_user`/`no_such_group`, `404`. A show with no rows is open; the first row restricts it to the listed people and groups plus every admin; removing the last reopens it. Audited `show_access` with the subject and level. `POST /api/groups/{name}/delete` is `409 group_in_use` while a row names the group. |
| `GET /api/users` | admin | `{ok, users: [{name, role, created_at, disabled_at}]}`; never tokens |
| `POST /api/users` | admin | `{name, role}` -> `{ok, user, token}`; the token is shown only here |
| `POST /api/users/{name}/disable` | admin | immediate; the last enabled admin is `409 last_admin` unless `VOLT_TOKEN` is set |
| `POST /api/users/{name}/enable` | admin | `{ok, user}`; the user's token and groups work again on the next request |
| `POST /api/users/{name}/rotate` | admin | `{ok, user, token}`; the old token stops working at once; `409 user_disabled` for a disabled user |
| `POST /api/items/{item}/lease` | operator, `edit` on the show | `{clientId, renew?}`: take the edit lease on an item, or renew your own (the same call). A lease lasts 45 s and the page renews every 15 s. `409 leased` names the holder (`holder`, `expiresAt`, `secondsLeft`); `409 lease_lost` when `renew: true` and the lease is gone; `409 not_editable` for a past item; `404`; `422 bad_input`; `503 collab_unavailable`. Audited as `lease_acquire` when a lease begins, not on each renew. |
| `POST /api/items/{item}/lease/release` | operator, `edit` | `{clientId}`: free your own tab's lease (idempotent; `{ok, released}`). Audited as `lease_release`, only when something was freed. |
| `POST /api/items/{item}/lease/takeover` | operator, `edit` | `{clientId}`: take a live lease from its holder; `409 not_leased` when nobody holds it. The old holder's next write or renew is `lease_lost`, and they are notified. Audited as `lease_takeover` with the previous holder. |
| `POST /api/presence` | viewer, `view` on the named show | `{clientId, show?, view}` where `view` is `show`, `library` or `item:<id>`: one tab's heartbeat, sent every 10 s, live for 30 s, at most 10 tabs per user. Never audited (a named exception, like a lease renew). `/api/state` carries `presence`, `leases` and `collab: {available}`. |
| `GET /api/comments?kind=&target=` | viewer, `view` on the show | Threads on an item or a show (`kind` is `item`, `show` or `asset`; asset comments are admin-only for now). Bodies are returned to people who can view the show and are rendered as text. `people` is the mention roster (enabled users who can view the show), given to every role including a viewer. |
| `POST /api/comments` | viewer, `view` on the show | `{kind, target, body, parent?}`: a comment (1 to 4000 characters, no control characters) or a one-level reply. `@name` mentions an enabled user who can view the show (at most 20). `429 rate_limited` past 30 a minute; `422 reply_depth`; `409 parent_deleted`. Audited as `comment_add` with ids only, never the body. |
| `POST /api/comments/{id}/resolve` | operator, `edit` | `{resolved: true\|false}` on the first comment of a thread (`422 not_a_thread`, `409 deleted`). Audited `comment_resolve`. |
| `POST /api/comments/{id}/delete` | viewer (author) or admin, `view` | Soft delete; the body is blanked. `403 forbidden_role` for anyone else. Audited `comment_delete`. |
| `GET /api/notifications[?unread=1]` | any signed-in user | Your own in-app notifications (mention, reply, lease taken), newest first, at most 100, only for shows you can still view. `unread` is the count. |
| `POST /api/notifications/read` | viewer | `{ids: [...]}` (at most 200) or `{all: true}`: mark your own read; another user's ids change nothing. Audited `notifications_read` with a count. |

Other operator routes: `POST /api/mos/{id}/resolve` (`id` is `device:show`; `{action: created|retry}`, operator with `run` on the show) answers a show whose `roCreate` got no acknowledgement; `POST /api/devices/safe-mode` (`{on, reason?}`, operator) stops Volt sending to any device but CoreVideo Pro. Both are audited.

Errors: `403 forbidden_role`, `401 unauthorized` (also a disabled or rotated-out token), `422 bad_name`/`bad_role`, `409
user_exists`, `404 no_such_user`, `503 auth_unavailable` (the user table could not be read; the request is refused, not let through).
Once any user row exists a token is required for everything but the static shell, even with no `VOLT_TOKEN`.

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
| `/api/record/start` | `owner?`, `camera?`, `show?` | Ask the room to record; mint only once output is observed (`start_recording`). Returns `asset`, `minted`, `essence`. | 503 `no_room`; 502 `room_refused` (nothing minted); 409 `record_busy` (another start or stop holds the room's lease); 422 |
| `/api/record/stop` | `asset` | Stop and wrap only on completed + finalized (`finish_recording`). Returns `checksum`, `asset`. | 404; 503; 502 `not_wrapped` (nothing wrapped); 409 `record_busy`; 422 |
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

Each edit is one transaction under the show's lock (the lock `go`, `take`, `skip` and `kill` also take, in any process, on SQLite and Postgres) and ends in a write that restates its condition (`WHERE status = 'ready'`), so an edit that races a take gets one outcome. They take no process-wide lock, so editing stays possible while a take waits on the room. Only a `ready` item can be edited; once an item is on air or has aired, the as-run log is its record.

| Path | Body | Does | Refusals |
| --- | --- | --- | --- |
| `POST /api/shows` | `title` (1 to 200), `id?` | Create a show. Returns `show: {id, title}`, `existed`. | 409 `id_conflict` (the id is another show's); 422 |
| `POST /api/shows/{show}/items` | `slug` (1 to 200), `script?` (20000 max), `id?` | Append an item (status `ready`, no media). Returns `item`, `existed`. | 404; 409 `id_conflict`; 422 |
| `POST /api/items/{item}/rename` | `slug?`, `script?` (at least one) | Rename. | 404; 409 `not_editable` (not ready); 422 |
| `POST /api/items/{item}/remove` | none | Delete an item that has not aired, with its cues. Allowed for `ready`, `killed`, `skipped` with no as-run. `kill` keeps the item in the list as killed; this takes it out. Returns `removed`. | 404; 409 `not_removable` (on air or aired) |
| `POST /api/items/{item}/attach` | `span` | (existing) attach a clip. | see above |
| `POST /api/shows/{show}/delete` | none | Delete a show that has not played: its `ready`, `killed` and `skipped` items and their cues, then the show row, in one transaction under the show lock (the one go and take hold). Spans, assets and versions are not touched. Refused when any item is on air or has aired (status `on air` or `aired`, or an as-run time): `409 show_not_deletable` with `items` (id and status of each), nothing changed. Returns `deleted`, `removedItems`, `removedCues`. A repeat is `404`. | 404; 409 `show_not_deletable`; 422 |
| `POST /api/items/{item}/detach` | none | Take the span off a ready item. A repeat is a 200 with `wasAttached: false`. | 404; 409 `not_attachable` |
| `POST /api/items/{item}/cues` | `command`, `room?` (only `corevideo.pro`), `payload?` (2000 max) | Add a cue at the end of the item's cues. Returns `cue` (`id, room, command, payload, status, adapter, verified, failure`). `warning` when there is no adapter (a take stops with `cue_unsupported`) or when there is an adapter that is not live-proven (a take still runs it). | 404; 409 `not_editable`; 422 `bad_command` (not in `cues.COREVIDEO_COMMANDS`; body lists `commands`), `bad_input` |
| `POST /api/cues/{cue}/remove` | none | Remove a cue that has not fired, from a ready item. Returns `removed`, `item`. | 404; 409 `not_editable`, `cue_fired` |
| `GET /api/items/{item}/cues` | none | The item's cues in order, plus `commands`. | 404 |
| `GET /api/cue-commands` | none | `commands: [{room, command, adapter, verified}]`. `adapter` is true when `dispatch.HANDLERS` has a handler (today `show-input.take`, `overlay.take`, `record.start`, `stream.start`). `verified` is true when that handler has been proven against a live core and recorded in rooms.md (today `overlay.take`, `record.start`, `stream.start`). `show-input.take` has an adapter but stays unverified until a live Program take is recorded. A cue with no adapter (`audio.snapshot`, `record.arm`) can be stored and armed, but a take stops at it with `cue_unsupported`. | |
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

## MOS export (rundown)

`GET /api/shows/{show}/mos[?cues=1&mos_id=&ncs_id=]`. Read-only and stateless: same auth, host and error rules as every other read; nothing is written, audited or sent. Returns the show as one MOS `roCreate` message in `content` (UTF-8 XML), with `{ok, format: "mos-roCreate", mosVersion, validated: false, conformance, filename, mime, show, sha256, stories, items, durationSeconds, includeCues, warnings}`. Design, element mapping and the conformance statement: [mos.md](mos.md). **Structurally follows MOS 2.8.5 roCreate as documented; unvalidated against an NCS or the MOS XSD/DTD.**

| Parameter | Meaning |
| --- | --- |
| `cues` | `1`/`true` adds each item's cues as namespaced `mosExternalMetadata`. Default off. |
| `mos_id` | `mosID` of this media object server (default `volt.mos`). Letters, digits, `_ . : -`, 1 to 128. |
| `ncs_id` | `ncsID` of the receiving system (default `ncs.unset`). Same rule. |

Refusals: `404 not_found` (unknown show), `422 bad_input` (unknown, repeated or malformed parameter). The `{show}` segment is the show id. The CLI (`volt mos export <show> [--out file] [--db] [--cues] [--mos-id] [--ncs-id]`) also accepts an exact title.

## Read endpoints (shot spans, slice 2)

All are `GET`, behind the same auth and host rules as every other data route (bearer token when set; `403 bad_host` on a non-loopback `Host` with no token). Errors use the same `{ok: false, error: {code, reason}}` shape. Unknown query parameters, a repeated parameter and bad values are `422 bad_input`; an unknown asset or span is `404 not_found`. Nothing here writes control-plane state.

| Path | Returns |
| --- | --- |
| `/api/assets/{asset}/spans?kind=&from=&to=&limit=&after=` | `{ok, assetId, rate, start, durationFrames, open, cleared, legalHold, hasProxy, pendingFrames, lastRun, spans, next}`. `kind` is a comma list of `shot`, `clip`, `transcript` (default `shot,clip`, so words never flood the panel). `from` and `to` are timecode labels on the essence's timebase (drop-frame `;` honoured); a span is returned when it overlaps `[from, to)`. Order is `frame_in, id`. `limit` 1 to 500 (default 200). `next` is an opaque cursor for `after`, or `null` on the last page. `422 bad_timecode` for a label that does not exist; `from` and `to` need an asset with a hi-res essence that has a frame rate. |
| `/api/assets/{asset}/transcript?from=&to=&limit=&after=` | `{ok, assetId, words, next, pendingFrames}`: transcript spans in a window, same ordering, window rule and cursor. `limit` 1 to 1000 (default 500). |
| `/api/assets/{asset}/proxy` | The proxy file, streamed. Honours one `Range` (`206` with `Content-Range`; `bytes=a-b`, `a-`, `-n`). A range past the end, malformed, or on an empty file is `416 bad_range` with `Content-Range: bytes */<size>`. Several ranges, a non-`bytes` unit and any request with `If-Range` get the whole file (`200`), which the spec allows. `404 no_proxy` when there is no proxy row or its file is gone. Accepts `?ticket=`. |
| `/media?path=<location>` | The old shell's player. `path` must equal, as a string, the `location` of a registered proxy essence; anything else (an unregistered file, a hi-res, `..`, a missing parameter's value) is `404 not_found`. The string is only compared, never opened. Same `Range` behaviour. |
| `/api/spans/{span}/thumb` | `image/jpeg`, 320 px wide, `Cache-Control: private, max-age=3600`. Cached at `VOLT_OUT/thumbs/<asset>/<span>-<frame>.jpg`; made on first request with one ffmpeg seek on the proxy (frame is `frame_in + max(2, 10% of length)`, kept inside the span). `404 no_proxy`; `502 thumb_failed` (ffmpeg missing, non-zero exit, or no image); `504 thumb_timeout` (15 s limit). A failure or timeout leaves no file behind, so the next request tries again. Accepts `?ticket=`. |
| `/api/media-ticket?asset=&kind=proxy\|thumb&ttl?` | `{ok, url, expires}`. HMAC ticket on the existing media path (`?ticket=`). Default TTL 120 s, max 3600. Signing key is `VOLT_MEDIA_KEY` or `VOLT_TOKEN` (a users-only server needs `VOLT_MEDIA_KEY`). Loopback with no token does not need a ticket. `503 no_media_key` when neither key is set. |
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

With `VOLT_TENANCY=on` every route first resolves the tenant from `Host`: unknown host is the plain 404 of an unknown route, a suspended tenant is `403 tenant_suspended` (except `/health`), an unreadable registry is `503 tenant_unavailable`. `GET /api/state`'s `room` block comes from a shared observation at most `VOLT_ROOM_OBSERVE_TTL` (1 s) old per `serve` process, and carries `observedAgeMs` (how old, in milliseconds); the rest of the payload is unchanged. Responses are HTTP/1.1 with `Content-Length` and the connection is kept alive; a POST refused before its handler reads the body has the body read and dropped (so the socket stays in step); only an oversize or chunked one answers `Connection: close`. `GET /api/state` assets now also carry `hasProxy` (the Library's asset picker sorts on it). They also carry `jobs`: the asset's unfinished jobs only, `[{type, status, error}]` with status `queued`, `running` or `failed` (a finished job is not listed), which the Library's Ingest panel reads for progress. `taking` is the id of the rundown item with status `taking`, or `null`.

### Detect, split, merge, indexing (slice 4b)

Same contract and auth as above; rights are never consulted (a legal hold or an uncleared asset is detected, split and merged like any other).

| Path | Body | Does | Refusals |
|---|---|---|---|
| `/api/assets/{asset}/shots/detect` | `threshold?` (above 0, below 1; default 0.13), `min_frames?` (1 to 10000; default 12) | Queue a `shots` job; a runner does the work. Returns `{ok, alreadyDone, job}`; `job` is `{id, status, attempt, error, updatedAt, ageSeconds, threshold, minFrames}`. The same settings are the same job (a finished run: `alreadyDone: true`, nothing queued; a queued or running one: that job). A failed run is queued again. | 404 `not_found`; 409 `essence_open` (wrap first), `detect_busy` (a run with other settings is queued or running; nothing queued); 422 `bad_input`; 503 `engine_missing` (no ffmpeg on the server) |
| `/api/spans/{span}/split` | `at` (timecode, strictly inside the shot) | Two shots tile the old one; the first keeps the id and label, the second is new and unnamed; both `origin: manual`. Returns `{ok, spans: [first, second]}`. | 404; 422 `bad_input`, `bad_timecode`; 409 `span_in_use`, `not_editable` (not a shot), `indexing` (the shot's frames are not backfilled yet), `conflict` (it changed meanwhile) |
| `/api/spans/{span}/merge` | `with` (the shot that starts where this one ends) | The first takes the other's out and the other is deleted, in one transaction. Returns `{ok, span, removed}`. | 404; 422 `bad_input`; 409 `not_adjacent` (a gap, itself, another asset, wrong order), `span_in_use` (either), `not_editable`, `indexing`, `conflict` |
| `/api/assets/{asset}/span-frames` | none | Queue the backfill when spans lack frame columns (`pendingFrames > 0`). Returns `{ok, queued, pendingFrames, job}`; `queued: false` and `job: null` when nothing is pending. One job per asset, ever: if it finished and some labels cannot be placed, `pendingFrames` stays above 0 and nothing is queued again; a failed job is queued again. | 404 |

`GET /api/assets/{asset}/spans` also returns `detect` (the newest detection job as above, with `threshold` and `minFrames`, or `null`) and `indexing` (the `span-frames` job, or `null`). `ageSeconds` is seconds since the job row last changed (for a running job, since it was claimed), computed on the server. A queued job with `error` set is waiting for a retry or for the recording to be wrapped.

## Typed metadata (schema and per-asset values)

Design: `docs/superpowers/specs/2026-11-01-search-indexing-design.md`. Same auth and error shape as above. Request keys are camelCase.

| Path | Role | Body / params | Does | Refusals |
|---|---|---|---|---|
| `GET /api/schema` | any | `show?` | `{ok, fields: [{key, label, type, showName, required, multi, position, retired, terms: [{term, retired}]}]}`: active fields, global plus the named show's | 422 `bad_input` (unknown parameter) |
| `POST /api/schema/fields` | admin | `key, label, type` (`text longtext number date bool enum`), `show?`, `required?`, `multi?` | Create a field; if `key` exists, edit it (`label`, `type`, `required`, `multi`; `show` cannot change). Returns `{ok, created, field}` | 422 `bad_input`; 409 `type_locked` (an asset holds a value), `field_exists` (retired key) |
| `POST /api/schema/fields/{key}/retire` | admin | none | Hide the field; stored values are kept | 404 `not_found` |
| `POST /api/schema/fields/{key}/terms` | operator | `terms` (list) | Add terms to an enum's vocabulary (a retired term is revived). Returns `{ok, added, field}` | 404; 422 `bad_input` (not an enum, over 500 terms) |
| `POST /api/schema/fields/{key}/terms/retire` | admin | `term` | A retired term stays valid on values already stored and is refused on new ones | 404; 422 `not_in_vocabulary` |
| `GET /api/assets/{asset}/metadata` | any | none | `{ok, assetId, values: {key: [..]}, missing: [required keys with no value]}` | 404 |
| `POST /api/assets/{asset}/metadata` | operator | `key, values` (list; `[]` clears) | Replace one field's values in one transaction. Returns `{ok, asset, key, values, missing}`. The audit row's `detail` is `{key, old, new}` | 404; 422 `unknown_field`, `type_mismatch`, `not_in_vocabulary`, `required_field`, `bad_input` |

Limits: text 1,000 characters, longtext 10,000, number finite within 1e15, date `YYYY-MM-DD`, 60 active fields, 500 terms per enum, 20 values per multi field. Required means incomplete, not refused at ingest.

## Search, saved searches and bins

Design: `docs/superpowers/specs/2026-11-01-search-indexing-design.md`. Reads need any valid token; the POSTs are operator. Request keys are camelCase. A saved search or a bin that someone else has not shared is invisible to you (`404 not_found`), and only its owner or an admin changes or deletes it (`403 forbidden_role`).

| Path | Role | Body / params | Does | Refusals |
|---|---|---|---|---|
| `GET /api/search` | any | `q` (words, "quoted phrases", `-word`; the last bare word is a prefix), `scope` (`all transcript labels metadata`), `filter` (URL-encoded JSON, at most 4 KB), `facets` (comma list), `sort` (`relevance recent show`), `group` (`asset hit`), `include` (`proposed`), `limit` (1 to 100, default 25), `after` | `{ok, assets \| hits, total, facets, next, degraded}`. An asset entry has its show, camera, room, owner, retention, `cleared`, `legalHold`, `blockedBy` (as the spans route), `archived`, `incomplete`, `metadata`, `score` and up to 3 `hits`; a hit is `{kind, spanId \| labelId, tcIn, tcOut, frameIn, frameOut, snippet, status, confidence, score}`. Deleted assets are excluded; archived ones are included and flagged; a file location is never returned. Face labels are absent until confirmed unless `include=proposed`; rejected labels never match. A filter naming a person (`label.person`) writes one `search_person` audit row per person id. `degraded: true` means SQLite has no FTS5 and the answer came from LIKE | 422 `bad_input` (unknown or repeated parameter, bad filter, bad cursor, `limit` over 100, `sort=relevance` or `group=hit` without `q`) |
| `GET /api/searches` | any | none | `{ok, searches: [{id, name, owner, shared, query, createdAt, updatedAt}]}`: yours and every shared one | |
| `POST /api/searches` | operator | `name` (1 to 100), `query` (a search request object, no `after`), `shared?` | Save a search. Returns `{ok, search}`. Run it by passing its `query` to `GET /api/search` | 422 `bad_input` (also 200 saved searches already) |
| `POST /api/searches/{id}/delete` | operator | none | Delete one | 404 `not_found`; 403 `forbidden_role` (shared, not yours, not admin) |
| `GET /api/bins` | any | none | `{ok, bins: [{id, name, owner, shared, createdAt, count}]}` | |
| `GET /api/bins/{id}` | any | none | `{ok, bin, members: [{assetId, spanId, position, addedBy, addedAt, gone, kind, label, tcIn, tcOut, assetDeleted}]}`. `gone: true` means the span was rewritten away; the member stays | 404 |
| `POST /api/bins` | operator | `name`, `shared?` | Create a bin. Returns `{ok, bin}` | 422 `bad_input` (also 200 bins already) |
| `POST /api/bins/{id}/add` | operator | `assetId`, `spanId?` | Add an asset, or one of its spans. Adding what is there is not an error: `{ok, added, member, bin}` | 404; 403; 422 `bad_input` (span not on that asset); 409 `bin_full` (5,000 items) |
| `POST /api/bins/{id}/remove` | operator | `assetId`, `spanId?` | Take an item out; the asset is untouched. `{ok, removed, bin}` | 404; 403 |
| `POST /api/bins/{id}/delete` | operator | none | Delete the bin and its membership | 404; 403 |

## Other codes

`401 unauthorized` (also `ticket_invalid` / expired media ticket), `403 bad_host`, `403 forbidden_role`, `404 not_found` (also unknown routes, and `GET /rundown?show=<id>` for an unknown show; the body is the same `{ok: false, error: {code, reason}}` shape), `411 length_required`, `413 too_large`, `415 unsupported_media_type`, `409 taking` (go, take, skip, kill, clock while an item of the show is taking), `409 overlaps_shot`/`span_in_use`/`not_editable`/`not_deletable`/`conflict`/`not_adjacent`/`detect_busy`/`indexing`/`essence_open` (shot spans), `503 engine_missing`, `503 no_media_key`, `416 bad_range`, `422 bad_input` (also `bad_timecode`, `outside_source`, `bad_actor`), `502 thumb_failed`, `504 thumb_timeout`, `500 internal` (no detail leaks; the traceback goes to the server's stderr).

## Known limits

- Atomicity is per underlying function. `take_item` commits the advance, then the cues, then a rewind on failure, so a crash of the server process mid-take can leave the rundown advanced with cues unsent. The rewind-on-failure path is covered; crash-mid-take is not.
- Two `serve` processes and the clock serialise on item status `taking`, not on a thread lock. There is no in-process sequencing lock.
- Dispatch waits on the room with no show lock. Hold, release, attach to a *different* ready item, rights and publish do not wait. A second take or go is `409 taking`.

## Newsroom scripts

Stories, the prompter, the wire inbox, templates, the archive and search. Bodies reject unknown fields; errors are `{ok:false, error:{code, reason}}` with extras (such as `current`) at the top level. Full behaviour and what is unvalidated: [newsroom-scripts.md](newsroom-scripts.md).

| Route | Role | Notes |
| --- | --- | --- |
| `GET /api/items/{item}/story` | any | Slug, script (private notes removed for a viewer), `rev`, `storyStatus`, approval record, `stats`, `revisions`, `events`. |
| `POST /api/items/{item}/script` | operator | `{script?, slug?, baseRev}`. 409 `stale_script` (with `current`), `not_editable`, `story_killed`. |
| `POST /api/items/{item}/story-status` | operator | `{status, baseRev, note?}`. 409 `bad_transition`, 403 `not_approver` (not on `VOLT_APPROVERS`, or the shared token while it is set), 403 `own_story` (with `VOLT_APPROVERS` set: you saved the current revision, so another approver must approve it). |
| `POST /api/items/{item}/float` | operator | `{floated}`. 409 `not_floatable`. |
| `POST /api/shows/{show}/reading-pace` | operator | `{wpm: 60..400 or null}`. |
| `GET /api/shows/{show}/prompter` | any, or a prompter ticket for that show | Feed: items with read and instruction blocks, `version`, `current`, `wpm`. No notes. |
| `POST /api/shows/{show}/prompter-ticket` | operator | `{ttlSeconds?}` (60 to 43200, default 14400). Returns `url`, `expiresAt`. 503 `no_media_key`. |
| `GET /api/shows/{show}/script?format=text` | any | Plain read text. |
| `GET /api/wire?state=&limit=&cursor=` | any | Inbox (`new`, `taken`, `dismissed`, `all`), feeds with last ok and last error. |
| `POST /api/wire/{id}/take` | operator | `{show, slug?, position?}`. 409 `already_taken`. |
| `POST /api/wire/{id}/dismiss` | operator | 409 `already_taken` once taken. |
| `GET /api/templates` | any | List (no bodies). |
| `POST /api/templates` | operator | `{kind: story or show, name, fromItem or fromShow or body}`. |
| `POST /api/templates/{id}/instantiate` | operator | Story: `{show, position?}`. Show: `{title}`. |
| `POST /api/templates/{id}/delete` | admin | |
| `GET /api/stories?q=&status=&show=&from=&to=&limit=&cursor=` | any | Live and archived stories, newest first, `archived` on each row. |
| `POST /api/items/{item}/copy` | operator | `{show, position?}`; `archive:<id>` copies an archived story. |

`/rundown` items gain `storyStatus`, `scriptRev`, `words`, `readSeconds`, `floated` and `lengthSource`, and the top level `readWpm`; the script text is never in `/rundown`.
