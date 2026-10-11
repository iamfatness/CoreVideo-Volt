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
| `/api/spans/{span}/publish` | none | Render house, OTT and social files (`publish_files`) into `VOLT_OUT`. Returns `versions`. | 404; 409 `not_cleared`; 502 `publish_failed` (versions list shows which kind failed; it did record the failure) |

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

## Other codes

`401 unauthorized`, `403 bad_host`, `404 not_found` (also unknown routes, and `GET /rundown?show=<id>` for an unknown show; the body is the same `{ok: false, error: {code, reason}}` shape), `411 length_required`, `413 too_large`, `415 unsupported_media_type`, `422 bad_input`, `500 internal` (no detail leaks; the traceback goes to the server's stderr).

## Known limits

- Atomicity is per underlying function. `take_item` commits the advance, then the cues, then a rewind on failure, so a crash of the server process mid-take can leave the rundown advanced with cues unsent. The rewind-on-failure path is covered; crash-mid-take is not.
- The in-process lock serialises one server process only. Two servers on one SQLite file can both advance.
- A take holds the sequencing lock while it waits on the room (up to about 10 seconds), so a second take or go waits. Hold and release do not.
