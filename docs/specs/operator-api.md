# Operator API

POST endpoints in `control/volt/serve.py` that give the operator UI hands. They only call functions that already exist (`advance`, `hold`, `take_item`, `start_recording`, `publish_files`, and so on). The existing GET routes and the `/rundown` shape (`{hold, items}`) are unchanged.

## Contract

- JSON in, JSON out. `Content-Type: application/json` is required on every POST. A body is optional; an empty body is `{}`. Unknown fields are refused.
- Success: HTTP 200, `{"ok": true, ...}`, always including the changed state (`rundown` in the `/rundown` shape for show and item actions, `asset` for rights and record actions, `versions` for publish).
- Refusal: `{"ok": false, "error": {"code": "<stable code>", "reason": "<human text>"}, ...}`. A refusal changes nothing. Clients switch on `code`; `reason` is for display.
- One store connection per request. Sequencing writes (go, take, skip, kill, attach, reorder, record start/stop) run one at a time in the server process. Hold, release, rights and publish do not wait on them, so HOLD lands while a take is waiting on the room.
- No CORS headers are ever sent. JSON-only POSTs mean a browser page on another origin cannot send one without a preflight, which is never answered.
- Request body limit 64 KiB (`413 too_large`). Chunked bodies are refused (`411`).

## Auth

| Bind | Token | Behaviour |
| --- | --- | --- |
| loopback (`127.0.0.0/8`, `::1`, `localhost`) | unset | No auth. Requests whose `Host` is not loopback get `403 bad_host` (DNS rebinding guard). |
| loopback | `VOLT_TOKEN` set | Bearer token required. |
| anything else | unset or under 16 chars | The server refuses to start. |
| anything else | `VOLT_TOKEN`, 16+ chars | Bearer token required. |

`Authorization: Bearer <VOLT_TOKEN>`, compared in constant time (`hmac.compare_digest`). Missing or wrong: `401 unauthorized` with `WWW-Authenticate: Bearer`, checked before routing. When a token is set it also guards the GET data routes (`/assets`, `/rundown`, `/seek`); only the static shell at `/` stays open.

Environment: `VOLT_HOST` (default `127.0.0.1`), `VOLT_PORT` (default 8765), `VOLT_TOKEN`, `VOLT_DB`, `VOLT_OUT` (publish output directory, default `control/out`), `VOLT_ROOM_URL` and `VOLT_ROOM_TOKEN` (the CoreVideoPro room; unset means room-backed routes answer `503 no_room`).

## Endpoints

IDs are path segments. All are `POST`.

| Path | Body | Does | Refusals |
| --- | --- | --- | --- |
| `/api/shows/{show}/go` | none | Advance the rundown (`advance`). Returns `target`, `previous`, `passed`, `over`, `rundown`. No room, no cues. | 404 `not_found`; 409 `held`; 409 `not_cleared`; 409 `no_media` (next item has no span) |
| `/api/shows/{show}/take` | `owner?` | Advance and dispatch the new item's cues to the room (`take_item`). Returns `advanced`, `item`, `results`, `rundown`. | 404; 409 `held`; 409 `not_cleared`; 409 `no_media`; 503 `no_room`; 409 `cue_failed` or `cue_unsupported` (see below); 422 |
| `/api/shows/{show}/hold` | `reason?` (string, 500 max) | Hold the rundown. Holding twice keeps the first hold (`alreadyHeld: true`). | 404; 422 |
| `/api/shows/{show}/release` | none | Release the hold. Releasing a show that is not held is a 200 with `wasHeld: false`. | 404 |
| `/api/shows/{show}/reorder` | `items`: every item id of the show, once each | Set the order. | 404; 422 `bad_input` (missing, duplicate or foreign ids) |
| `/api/items/{item}/skip` | none | Mark a ready item skipped. | 404; 409 `not_skippable` (on air or already past) |
| `/api/items/{item}/kill` | none | Kill a ready item. The asset stays. | 404; 409 `not_killable` |
| `/api/items/{item}/attach` | `span` | Attach a span to a ready item. | 404 (item or span); 409 `not_attachable`; 422 |
| `/api/record/start` | `owner?`, `camera?`, `show?` | Ask the room to record; mint only once output is observed (`start_recording`). Returns `asset`, `minted`, `essence`. | 503 `no_room`; 502 `room_refused` (nothing minted); 422 |
| `/api/record/stop` | `asset` | Stop and wrap only on completed + finalized (`finish_recording`). Returns `checksum`, `asset`. | 404; 503; 502 `not_wrapped` (nothing wrapped); 422 |
| `/api/assets/{asset}/cleared` | `cleared`: boolean | Set the cleared flag. | 404; 422 (must be a real boolean) |
| `/api/assets/{asset}/legal-hold` | `held`: boolean | Set the legal hold. A held asset cannot air or publish. | 404; 422 |
| `/api/spans/{span}/publish` | none | Render house, OTT and social files (`publish_files`) into `VOLT_OUT`. Returns `versions`. | 404; 409 `not_cleared`; 409 `publish_busy` (another writer holds a version; nothing was rendered by this call); 502 `publish_failed` (versions list shows which kind failed; it did record the failure) |
| `/api/exports/nle` | `format`, `spans`, `media?`, `name?`, `start_tc?`, `dry_run?`, `skip_blocked?` | Build an xmeml or EDL file from span IDs (`volt/nle.py`). Returns `content`, `sha256`, `spans`, `warnings`. See [Export](#export-shot-spans-slice-6). | 404; 409 `blocked`, `essence_open`, `no_proxy`, `proxy_mismatch`, `not_local`, `outside_source`; 422 `mixed_rates`, `unsupported_rate`, `bad_input`; 500 `bad_path_map` |

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
| `skip_blocked` | `true` leaves blocked spans out and names them in the file and in `warnings`. Default false. |

Success: `{ok, format, filename, mime, content, sha256, spans, warnings, durationFrames, rate, startTc, dropFrame}`. `spans` has one entry per requested span: `{id, status: "ready"|"blocked", blockedBy?, reason?}`, `blockedBy` being `"not cleared"` or `"legal hold"`. `sha256` is over the UTF-8 bytes of `content`. `rate` is the exact rate (`"25"`, `"30000/1001"`). A dry run returns `dryRun: true`, `spans`, `warnings`, `preview` (the first 20 lines) and the same totals, and no `content`. Real runs log the span IDs and `sha256` to the server's stderr.

Refusals (nothing is written): 404 `not_found` (a span); 409 `blocked` (not cleared or legal hold; body has `spans` with the verdict of each); 409 `essence_open` (the chosen essence is still being written); 409 `no_proxy`; 409 `proxy_mismatch` (the proxy records a different rate or start timecode than the hi-res); 409 `not_local` (the location is a URL or not an absolute path); 409 `outside_source` (the span lies outside its essence); 422 `mixed_rates` (body has `rates`); 422 `unsupported_rate`; 422 `bad_input`; 500 `bad_path_map`. Rights are checked first, then the rest on the spans that would be exported.

`VOLT_EXPORT_PATH_MAP` (server environment, read on each request): `D:\iso=\\nas\iso;E:\cam=/Volumes/cam`. A leading prefix of the stored media location is rewritten (segment boundary, longest prefix first, Windows prefixes case-insensitive) when building the file's media path. Unset means paths are written as stored.

## Read endpoints (shot spans, slice 2)

All are `GET`, behind the same auth and host rules as every other data route (bearer token when set; `403 bad_host` on a non-loopback `Host` with no token). Errors use the same `{ok: false, error: {code, reason}}` shape. Unknown query parameters, a repeated parameter and bad values are `422 bad_input`; an unknown asset or span is `404 not_found`. Nothing here writes control-plane state.

| Path | Returns |
| --- | --- |
| `/api/assets/{asset}/spans?kind=&from=&to=&limit=&after=` | `{ok, assetId, rate, start, durationFrames, open, cleared, legalHold, hasProxy, pendingFrames, lastRun, spans, next}`. `kind` is a comma list of `shot`, `clip`, `transcript` (default `shot,clip`, so words never flood the panel). `from` and `to` are timecode labels on the essence's timebase (drop-frame `;` honoured); a span is returned when it overlaps `[from, to)`. Order is `frame_in, id`. `limit` 1 to 500 (default 200). `next` is an opaque cursor for `after`, or `null` on the last page. `422 bad_timecode` for a label that does not exist; `from` and `to` need an asset with a hi-res essence that has a frame rate. |
| `/api/assets/{asset}/transcript?from=&to=&limit=&after=` | `{ok, assetId, words, next, pendingFrames}`: transcript spans in a window, same ordering, window rule and cursor. `limit` 1 to 1000 (default 500). |
| `/api/assets/{asset}/proxy` | The proxy file, streamed. Honours one `Range` (`206` with `Content-Range`; `bytes=a-b`, `a-`, `-n`). A range past the end, malformed, or on an empty file is `416 bad_range` with `Content-Range: bytes */<size>`. Several ranges, a non-`bytes` unit and any request with `If-Range` get the whole file (`200`), which the spec allows. `404 no_proxy` when there is no proxy row or its file is gone. |
| `/media?path=<location>` | The old shell's player. `path` must equal, as a string, the `location` of a registered proxy essence; anything else (an unregistered file, a hi-res, `..`, a missing parameter's value) is `404 not_found`. The string is only compared, never opened. Same `Range` behaviour. |
| `/api/spans/{span}/thumb` | `image/jpeg`, 320 px wide, `Cache-Control: private, max-age=3600`. Cached at `VOLT_OUT/thumbs/<asset>/<span>-<frame>.jpg`; made on first request with one ffmpeg seek on the proxy (frame is `frame_in + max(2, 10% of length)`, kept inside the span). `404 no_proxy`; `502 thumb_failed` (ffmpeg missing, non-zero exit, or no image); `504 thumb_timeout` (15 s limit). A failure or timeout leaves no file behind, so the next request tries again. |

Span JSON: `id, assetId, kind, origin, label, tcIn, tcOut, frameIn, frameOut, text, runId, blockedBy, usedBy: {items, versions}`. `blockedBy` is `null`, `"not cleared"` or `"legal hold"`, same words as `/api/state`. `lastRun` is the newest `shot_run` row (`job, essence, engine, engineVersion, threshold, minFrames, shotCount, createdAt`) or `null`.

`pendingFrames` counts spans whose `frame_in`/`frame_out` are not yet backfilled (migration 9 leaves old rows NULL until the `span-frames` job runs). Such spans cannot be ordered or windowed, so these routes leave them out and say how many.

Limits of these routes: a browser `<video src>` or `<img src>` cannot send a bearer header, so with `VOLT_TOKEN` set a page has to fetch the bytes itself (or a later slice adds a short-lived signed URL). The proxy route reads the file as registered; a proxy still being written (open) is served as far as it has been written. Thumbnails assume the proxy starts at the hi-res's timecode and rate, as the spec does; that is not checked.

## Other codes

`401 unauthorized`, `403 bad_host`, `404 not_found` (also unknown routes, and `GET /rundown?show=<id>` for an unknown show; the body is the same `{ok: false, error: {code, reason}}` shape), `411 length_required`, `413 too_large`, `415 unsupported_media_type`, `416 bad_range`, `422 bad_input`, `502 thumb_failed`, `504 thumb_timeout`, `500 internal` (no detail leaks; the traceback goes to the server's stderr).

## Known limits

- Atomicity is per underlying function. `take_item` commits the advance, then the cues, then a rewind on failure, so a crash of the server process mid-take can leave the rundown advanced with cues unsent. The rewind-on-failure path is covered; crash-mid-take is not.
- The in-process lock serialises one server process only. Two servers on one SQLite file can both advance.
- A take holds the sequencing lock while it waits on the room (up to about 10 seconds), so a second take or go waits. Hold and release do not.
