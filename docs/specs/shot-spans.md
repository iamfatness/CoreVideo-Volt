# Shot spans and the NLE panel

Status: design only. Nothing in this file is built. Owner rule applies: the mockups in [docs/mockups/shot-spans.html](../mockups/shot-spans.html) are shown and approved before any code. The mockup is the screen; this file is what sits under it.

Roadmap and areas both list "shot spans" and "one NLE panel" as next for MAM. A second NLE is later and out of scope here.

## What exists today

Read from the tree on 2026-10-12, not assumed.

- A span is a row: `id, asset_id, kind, tc_in, tc_out, text, created_at`. It belongs to an asset, not to an essence. Its times are timecode labels on source timecode. The only kind in code is `transcript` (one row per word; `library.index_words`, `speech.py`). `data-model.md` already names the other kinds: marker, subclip, selected highlight.
- Everything that reads spans filters by kind where it matters: search is `kind = 'transcript'` (`library.search`), and a speech re-run deletes only `kind = 'transcript'` (`speech.py`). New kinds therefore do not leak into search and survive a transcript re-run.
- Nothing filters `GET /api/state`. `operator_state.spans()` returns up to 200 spans of every kind, ordered by `tc_in, id`, so once an asset has a transcript the first 200 rows are already words. Shot spans would crowd out what that list is for (publishable spans with versions). The panel must not use it; the list needs a kind filter (see Compatibility).
- A span is referenced by `item.span_id` and by `version.span_id` (both foreign keys). The rundown holds span IDs, not paths. `POST /api/items/{item}/attach` accepts any span ID; `POST /api/spans/{span}/publish` cuts any span from the hi-res at its timecode. So a shot span or a clip span can already be attached and published with no change.
- Timecode arithmetic is `volt/timecode.py`: exact rational rates, drop-frame when the essence start has a `;`. Spans are written with the same label style as their essence.
- The essence row has `timecode_start` and `frame_rate` but no length. There is no thumbnail anywhere.
- The old shell calls `/media?path=...` to play a proxy. I found no `/media` route in `serve.py`. Playing a proxy in the panel needs a real route (below). Not run; this is from reading `serve.py` and grepping it.
- The operator UI has four screens (Show, Record, Rights, Publish). Library search lives only in the old shell (`control/session/index.html`).

## Definitions

- **Shot span** (`kind = 'shot'`): one cut-to-cut run of a single camera essence. Shots of one asset never overlap each other. They normally tile the essence but need not (a manual shot list may leave gaps).
- **Clip span** (`kind = 'clip'`): an operator selection, a subclip or highlight. It may cover part of a shot or several shots, may overlap anything, and is what gets attached, published or exported.
- **Transcript span** (`kind = 'transcript'`): unchanged.
- A point marker (one frame, with a note) is not part of this design; see open question 7. `tc_out > tc_in` always holds.

Both new kinds are the same row shape as today with four additions. No new object is added to the frozen data model, which is the point: shots and clips are spans.

## Data model

Additions to `span` (migration 9, all nullable so every existing row stays valid):

| Column | Type | Meaning |
|---|---|---|
| `label` | TEXT | Operator name ("Bench reaction"). Null for unnamed detected shots; the UI shows "Shot N". |
| `origin` | TEXT | `detect`, `manual`, `transcript`. Null on old transcript rows. |
| `run_id` | TEXT | Job ID of the detection run that made it. Null unless `origin = 'detect'`. |
| `frame_in`, `frame_out` | INTEGER | The span's in and out as whole frames from midnight on the essence's exact rate, `tc_to_frames` of the labels. Written for every new span. Exists so range queries, ordering, overlap checks and the transcript view do not compare timecode strings. |

Additions to `essence`: `duration_frames` INTEGER (null = unknown). Needed to refuse a mark outside the source at create time instead of at publish time, and to draw the timeline. Set at ingest and wrap; for an open essence it is null and the clamp is lower-bound only.

New table `shot_run` (provenance, parallel to `asr_run`): `job_id` (PK, references job), `asset_id`, `essence_id`, `engine`, `engine_version`, `threshold`, `min_frames`, `shot_count`, `created_at`, `settings` (JSON text). Same idea as `asr_run`: a re-run with a different setting is a new recorded run.

New job type `shots`, key `shots-v1:<hash of engine, version, threshold, min_frames>`. Same configuration on the same asset is a no-op; a change is a new run.

Indexes (raw statements, both backends): `span(asset_id, kind, frame_in)`.

Rules, enforced in code in one place (`volt/shots.py`, new) not in the page:

1. `tc_out > tc_in`; both are valid labels for the essence (drop-frame labels that do not exist are refused); `tc_in >= essence timecode_start`; if `duration_frames` is known, `tc_out <= start + duration`.
2. A shot may not overlap another shot of the same asset (`overlaps_shot`). A clip may overlap anything.
3. A span that a rundown item or a version uses cannot be deleted or have its in/out changed. Label may change. (`span_in_use`.) This protects a published version from being detached from the frames it was cut from.
4. Detection replaces only its own previous output: spans with `origin = 'detect'` and no item or version using them. Manual shots, clips and detected shots that are in use are never touched; the run report lists how many were kept.

Rights: spans carry no rights; the asset does (data-model). Marking and detection are allowed on any asset, cleared or not, because an operator has to be able to log footage to decide what to clear. Export, publish and air are gated on the asset exactly as today (`Store.assert_publishable`: cleared and not on legal hold). This is a proposal; see open question 2.

Observation, not part of this work: `speech.py` deletes all `transcript` spans of an asset on a re-run, and `item.span_id` and `version.span_id` reference spans. A transcript span that an item uses would block that delete on Postgres (or orphan the reference on SQLite if foreign keys are off). I did not run this. Worth a test before anyone attaches a word span to an item and re-runs transcription.

## How shot spans are created

### 1. Manual marks

On the player: `I` sets in, `O` sets out, `Enter` adds a clip. The position is a frame number from the player, sent to the server as a timecode label; the server converts, validates (rules above) and stores. A shot is made by choosing "as shot" in the add dialog, or by Split on an existing shot at the playhead. Typed timecode is accepted for assets with no proxy.

Marking works on an open (still recording) asset against the growing proxy. It is the one live-show use: mark moments while the show is on air. Out of range cannot be checked there (no duration), so the clip is re-validated at wrap; a clip past the end is flagged, not deleted.

### 2. Detection

`POST /api/assets/{asset}/shots/detect` enqueues a `shots` job. The runner handler, `shots`, beside `proxy`, `asr`, `publish`:

- Runs only on a closed essence. An open one is deferred (not failed), like transcription.
- Reads the proxy when its frame rate and timecode start equal the hi-res's, otherwise the hi-res, so frame numbers map one to one. This check is cheap and fails loud; it replaces trusting that the proxy builder preserves them. (I have not checked that it does.)
- Finds cuts with ffmpeg's scene score: `select='gt(scene,T)'` with `showinfo`, parsing `pts_time` to frames at the essence's exact rate. ffmpeg is already a hard dependency (`deliverables.py`, `chunking.py` silencedetect). Stdlib only, no new package.
- Settings: `threshold` (default 0.30, recorded), `min_frames` (default 12; a shot shorter than this is merged into the previous one so a flash frame is not a shot).
- Writes shots tiling the essence from its start to its end, then one thumbnail per shot (below). Commits spans per run in one transaction; a failed run changes nothing.
- Result row in `shot_run`. The page shows "Last detection ... threshold ... N shots" from it.

The scene threshold is a guess from common practice. It has not been measured on any footage. Slice 3 begins with a measurement on real iso recordings (a camera cut feed and a single-camera iso) and the default is set from that, the way `speech-scale.md` set VAD. Expect detection to be wrong on dissolves, graphics wipes and strobe; the Split, Merge and nudge operations exist for that, and detected shots are labelled Detected so nobody mistakes them for edited ones.

An iso recording of one fixed camera has few real cuts. Detection helps most on program-out recordings and on ingested cards. For an iso, manual marks and transcript clips are the main path.

### 3. From transcript words

The transcript view lists word spans for an asset. The operator selects a word range (mouse drag, or keyboard: arrows move the cursor, Shift extends, Enter commits). The page sends the first and last word span IDs, not times:

```
POST /api/assets/{asset}/spans  {"kind":"clip","from_words":["<first span id>","<last span id>"],
                                 "handle_in":0,"handle_out":18,"snap":"shot","label":"..."}
```

The server loads both words, refuses if they are not transcript spans of this asset or are out of order, and builds `tc_in` from the first word's in and `tc_out` from the last word's out, plus handles in frames. `snap: "shot"` extends the in to the start of the shot containing it and the out to the end of the shot containing it (never shrinks). `text` is the words joined by single spaces; `origin = 'transcript'`.

Why the server computes: the word times live in the store, a stale page cannot send a time that does not match the words, and the same code path serves a future non-browser client.

Accuracy to design around, from `library.md`: word starts are good to about a quarter second (worst 0.24 s measured on one TTS clip, first word snapped to segment start), word ends are looser. The mockup therefore shows frame handles and the shot cuts inline in the transcript, and says the words are not frame accurate. The numbers are from synthetic speech and `tiny.en`; nothing is known about real speakers.

## API

Style follows `operator-api.md`: JSON in, JSON out, `{ok: true, ...}` with the changed state, or `{ok: false, error: {code, reason}}` and nothing changed; POST needs `Content-Type: application/json`; unknown fields refused; token as today; no CORS. Reads are GETs behind the same token gate as `/api/state`. IDs in the path.

Span JSON (camelCase, like `operator_state`): `id, assetId, kind, origin, label, tcIn, tcOut, frameIn, frameOut, text, runId, blockedBy, usedBy: {items, versions}`. `blockedBy` is `null`, `"not cleared"` or `"legal hold"`, same words as `operator_state`.

Reads:

| Path | Returns |
|---|---|
| `GET /api/assets/{asset}/spans?kind=shot,clip&from=<tc>&to=<tc>&limit=&after=` | `spans` ordered by `frame_in, id`, `next` cursor. Default limit 200, max 500. Also returns the asset facts the panel header needs: `rate`, `start`, `durationFrames`, `open`, `cleared`, `legalHold`, `hasProxy`, `lastRun` (the `shot_run` summary or null). |
| `GET /api/assets/{asset}/transcript?from=<tc>&to=<tc>&limit=` | word spans in a window, ordered by `frame_in`, with `next`. Paged, because an hour of speech is about ten thousand rows. |
| `GET /api/spans/{span}/thumb` | `image/jpeg`. Generated on first request if missing (below). 404 `no_proxy` when there is nothing to cut it from. `Cache-Control: private, max-age=3600`. |
| `GET /api/assets/{asset}/proxy` | The proxy file with HTTP `Range` support, so the player can seek. The asset ID picks the file; the client never sends a path. 404 `no_proxy`. Replaces the missing `/media`. |

Writes (all POST):

| Path | Body | Does | Refusals |
|---|---|---|---|
| `/api/assets/{asset}/spans` | `kind` (`shot`, `clip`), then either `tc_in` + `tc_out`, or `from_words` as above; `label?`, `handle_in?`, `handle_out?`, `snap?` | Create a span. Returns `span`. | 404; 422 `bad_input`, `bad_timecode`, `outside_source`; 409 `overlaps_shot`, `not_transcript` |
| `/api/spans/{span}/trim` | `tc_in?`, `tc_out?` | Change the in and/or out. | 404; 422; 409 `overlaps_shot`, `span_in_use` |
| `/api/spans/{span}/label` | `label` (string, 200 max, null clears) | Rename. Allowed on a span in use. | 404; 422 |
| `/api/spans/{span}/split` | `at` | Cut one shot into two at a frame. Returns both. Shots only. | 404; 422 `bad_timecode`; 409 `span_in_use` |
| `/api/spans/{span}/merge` | `with` (the next shot's ID) | Join two adjacent shots of one asset. | 404; 409 `not_adjacent`, `span_in_use` |
| `/api/spans/{span}/delete` | none | Delete a shot or clip. Transcript spans are refused. | 404; 409 `span_in_use`, `not_deletable` |
| `/api/assets/{asset}/shots/detect` | `threshold?`, `min_frames?` | Enqueue a `shots` job. Returns `job`. A repeat with the same settings returns the existing run (`alreadyDone: true`). | 404; 409 `essence_open` (hint to try after wrap), `no_proxy`; 422 |
| `/api/exports/nle` | below | Build the interchange file. | below |

Attach and publish stay where they are: the panel calls `/api/items/{item}/attach` and `/api/spans/{span}/publish` with the span ID. Delete and trim of a span refuse with `span_in_use` while those reference it.

Serialisation: split, merge, trim, delete and detect's write step take the same per-asset lock the way `Store.transaction(lock)` does for rundown position inserts, so two operators marking the same asset see the overlap rule hold. Create of a clip needs no lock. (Single server process only, as with `_SEQ`; same caveat as `operator-api.md`.)

### Thumbnails

Not control-plane state. A thumbnail is a cache file under `VOLT_OUT/thumbs/<asset>/<span>-<frame>.jpg`, where `<frame>` is the frame chosen. The chosen frame is `frame_in + max(2, 10% of length)` capped inside the span, so it is past the cut and not a transition blend. The key includes the frame, so a trim naturally makes a new file and the old one is just garbage. The `shots` job writes them for the shots it creates; the `thumb` route makes a missing one on demand (one ffmpeg seek on the proxy, 15 s limit, 320 px wide). Losing the directory loses nothing. Size is bounded by span count; no retention policy in slice 1. Whether to put thumbnails through the storage backend is open question 8.

CSP already allows `img-src 'self' data:`; no change.

## Compatibility with what is built

- `operator_state.spans()` gets a kind filter (or a kind argument) in slice 1, so `/api/state` is not flooded. Which kinds the Publish screen lists today is itself undecided: it currently lists transcript word spans, up to 200. Changing that is a behaviour change to a screen that exists; the filter must be decided with the owner (open question 9), not slipped in.
- `search` and speech re-runs are already kind-scoped.
- Publish and attach take any span ID and need no change. A clip span publishes three versions like a word span does.
- Postgres: migration 9 uses the neutral types; `frame_in`/`frame_out` are INTEGER (a 32-bit integer holds over a year of frames at 59.94 fps from midnight, far past any one essence).

## Migration 9

Per `migrations.py`: add the columns to `store.SCHEMA` and `control/schema.sql` in the same order (the parity test checks it), and append one `Migration(9, ...)`. Not implemented here.

```
Migration(9, "span shot columns, essence duration, shot_run table",
          columns=(("span", "label", "TEXT"), ("span", "origin", "TEXT"), ("span", "run_id", "TEXT"),
                   ("span", "frame_in", "INTEGER"), ("span", "frame_out", "INTEGER"),
                   ("essence", "duration_frames", "INTEGER")),
          sqlite=("CREATE INDEX IF NOT EXISTS span_asset_kind_frame ON span (asset_id, kind, frame_in)",),
          postgres=("CREATE INDEX IF NOT EXISTS span_asset_kind_frame ON span (asset_id, kind, frame_in)",))
```

`shot_run` is a new table, so base-table creation makes it; the entry records the version, as 4 and 7 did.

Needs, beyond the numbered entry:

- **Backfill of `frame_in`/`frame_out` for existing transcript spans.** The migration framework runs statements and `ADD COLUMN`; it cannot compute `tc_to_frames` per row. Proposal: a runner job `span-frames` (key `span-frames-v1`, idempotent per asset) that fills the columns from each asset's essence rate and start, queued lazily the first time the panel opens an asset with NULL frames. Until it finishes, the transcript endpoint falls back to ordering by the label string. Alternative: do not add frame columns, order by string. That is fine for a single-day timecode and wrong across midnight rollover and across mixed drop/non-drop labels; I recommend the columns, but it is the heaviest part of the migration. Open question 3.
- **`duration_frames` backfill** for existing closed essences: one ffprobe per essence in a one-off job, or leave null and let the clamp be lower-bound only for old assets. Proposal: leave null, fill at ingest and wrap from now on, and fill an old asset when detection first runs on it.
- Add `test_migrations.py` coverage: a historical schema shape (see `historical_shapes.py`) at version 8 migrates forward, on SQLite and Postgres.

## The NLE panel

### Where it lives

The mockup puts it on the operator UI as a fifth screen, Library, key `5`, between Publish and the end of the nav. This is the owner's call (open question 1). The brand guide names Library as one of three product screens, and the operator UI has no library screen today. The alternative, a panel inside Show, was not mocked: the Show screen is the live one and its keys (Space is TAKE) must not share a screen with a player that wants play/pause.

Screens, in the mockup:

1. Shot list: asset header, player with a timeline showing shot boundaries and clips, the table of shots and clips (thumbnail, name, origin, in, out, length, transcript snippet, state), a selection tray, a span editor.
2. Mark from transcript: transcript with cuts inline, a word cursor, handles, snap to shot, thumbnails at in and out.
3. Selection to export: per-span verdict, options, a preview of the first lines of the file.
4. Empty and error states.

Selection is client state: a list of span IDs in order, kept in memory and in `sessionStorage` like the token (gone when the tab closes). It is not a stored object. A saved sequence or bin would be a new object in the frozen data model and is deliberately not proposed (open question 5).

Keys, Library screen only: `I` mark in, `O` mark out, `Enter` add clip, `,` and `.` step a frame, `[` and `]` previous and next shot, `K` play and pause, Space toggles the focused row's checkbox, `5` opens the screen. Keys must be scoped per screen: Space is TAKE on Show.

State is never colour only: every status is a glyph and a word; focus rings are 2 px; the one spinner stops under `prefers-reduced-motion`. Same as `operator-ui.md`.

### Hand-off to the NLE

Premiere is the one NLE. Basis: `data-model.md` already lists "a Premiere media path" as an external ref for an asset. I have not verified that the owner's NLE is Premiere (open question 4).

The panel hands a selection to the NLE as a file. Two formats:

| Format | Why | Limits |
|---|---|---|
| Final Cut Pro 7 XML (`xmeml` v4) | Premiere imports it (File > Import). Carries clip names, media paths, in and out, comments, frame rate, drop-frame flag, several tracks. Plain XML, built with `xml.etree` (stdlib). | Written from my knowledge of the format. Not imported into Premiere by me. |
| CMX3600 EDL | Lowest common denominator; any NLE and most conform tools read it. Text, stdlib. | Cuts only. 8-character reel names. No media paths. One rate per file. |

AAF is not proposed. Writing AAF needs a third-party library (pyaaf2) or a lot of hand-built container code; the repo is stdlib-first. Revisit if the xmeml route fails in practice.

Contents of the xmeml file:

- One sequence at the rate of the first span's essence. `timebase` and `ntsc` from the exact rate (25 fps: 25/FALSE; 29.97: 30/TRUE with `displayformat` DF for a drop-frame start; 23.976: 24/TRUE).
- One video track (V1) with one `clipitem` per span in selection order, butted end to end from `start_tc` (default `01:00:00:00`, as sequences conventionally start). Audio tracks mirror the essence's channel count from `essence.audio_layout`, linked to the clip.
- Per clipitem: `name` (label, else "Shot N" or the asset's camera and in point), `in` and `out` as frames from the start of the file, `file` with `pathurl`, `rate`, and the source `timecode` string, `comments` carrying the asset ID and span ID, and the transcript text if any. The span ID in the comment is the return path: a future NLE-side tool can map back to the span.
- `in` frames are `tc_to_frames(tc_in) - tc_to_frames(essence.timecode_start)` on the exact rate, using `volt/timecode.py`. Drop-frame is handled by the same function the publish path uses.
- One `file` element per essence, referenced by id after its first use (the xmeml convention).

CMX3600: `TITLE:` and `FCM:` header, one event per span: event number, reel (asset ID, upper-cased, cut to 8), `V`, `C`, source in, source out, record in, record out; `* FROM CLIP NAME:` and `* COMMENT:` lines for label and span ID. Audio events when the layout has audio (`AA`).

Media choice: `hi-res` (default) or `proxy`. A proxy export lets an editor cut on small files and relink to hi-res; the file's `file/name` still says which it is.

Paths: an NLE machine rarely sees the server's `D:\iso`. A server setting `VOLT_EXPORT_PATH_MAP` ("`D:\iso=\\nas\iso;...`") rewrites a prefix when building `pathurl`. No mapping means paths are written as stored. Nothing here moves or copies media.

`POST /api/exports/nle`:

```
{"format":"xmeml"|"edl", "spans":["<id>", ...], "media":"hi-res"|"proxy",
 "name":"Final Four Watch selects", "start_tc":"01:00:00:00",
 "dry_run":false, "skip_blocked":false}
```

- Up to 500 spans. Order is the order sent. Stateless: nothing is stored, the response carries the file.
- `dry_run: true` returns `spans: [{id, status: "ready"|"blocked", blockedBy?, reason?}]`, `warnings`, and a `preview` of the first lines, with no file. The panel calls it every time the selection or options change.
- A real run with any blocked span and no `skip_blocked` is refused 409 `blocked` with the same per-span list. With `skip_blocked: true` the blocked spans are left out and named in a comment in the file and in `warnings`.
- Success: `{ok: true, format, filename, mime, content, sha256, spans: [...], warnings: [...], durationFrames, rate}`. The page saves `content` through a Blob download. JSON out keeps the contract of `operator-api.md`; an export of 500 spans is a few hundred KB of text.
- Refusals: 404 `not_found` (a span ID); 409 `blocked` (not cleared, legal hold; reason words as `operator_state`); 409 `essence_open` (a span's hi-res is still growing: export proxy or wait for wrap); 409 `not_local` (the media location is not a file path an NLE can open); 409 `no_proxy`; 422 `mixed_rates`; 422 `bad_input` (empty list, over 500, bad format). Over 500 spans is 422 `bad_input`.
- Slice 4 refuses a selection of mixed frame rates. xmeml allows per-clip rates and the NLE would conform, but whether Premiere does so cleanly is exactly what I have not seen. See open question 6.

Export rights gate, proposed: refuse a span whose asset is not cleared or is on legal hold, because the file is a means to a published cut outside Volt's own publish gate. This is the strict reading and the reverse of the transcription gate, which lets legal hold through. Open question 2.

No record of exports is kept in slice 4 (the server logs the span IDs and sha256 to stderr). If the owner wants an audit trail it is a small `nle_export` table, a later slice.

## Acceptance

- Manual mark: create a clip from a proxy, restart the server, it is listed in order with the right timecode. A drop-frame asset's labels use `;` and skip labels that do not exist.
- Words: select "steal" through the next sentence, get a clip whose `tc_in` equals the first word's `tc_in` and out the last word's `tc_out`, plus handles. Two words from different assets are refused.
- Detection: run on a clip with known cuts (generate one with ffmpeg `testsrc` segments of known length, at 25 fps and at 29.97 drop-frame); every cut found within one frame, none invented on a static clip. A second run with the same settings creates nothing. A manual shot and an attached detected shot survive a re-run with a different threshold.
- Overlap: a shot overlapping another is refused with `overlaps_shot` and nothing changes.
- In use: a span on a rundown item or with a version cannot be trimmed or deleted.
- Export: a three-span selection yields an xmeml whose `in`/`out` equal the spans' frame offsets from the essence start, exact at 29.97 drop-frame (arithmetic test on the file, no NLE needed); the EDL's source in/out are the span labels. A non-cleared span blocks the export unless `skip_blocked`.
- Real NLE: import the xmeml and the EDL into Premiere on a real machine and check that clip names, in and out and media link are right. Manual check, written down with the Premiere version. Until it is done the xmeml format is unverified, and slice 4 is not called done.
- Migration 9: a version-8 database upgrades on SQLite and Postgres; frames backfill is idempotent.

## Not verified (this work)

- Nothing was run. The only executable check is the accessibility scan of the mockup (`docs/mockups/shot-spans.html`): axe-core 4.x in Chrome, run on all four mockup tabs at 1280 and 768 px, rules `wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa, best-practice`: 0 violations. axe also reports "needs review" items it cannot decide (glyph characters outside the BMP, buttons whose visible text is a symbol, a table scrolled sideways at 768); I computed the contrast of every text and background pair by hand and all are above 4.5:1 (lowest 5.0). A screen reader and a keyboard-only pass were not done. The mockup tabs implement arrow-key tab switching; nothing else in it is interactive.
- The xmeml and EDL structure is from memory of the formats. No file was written or imported.
- No ffmpeg scene detection was run, so the threshold, the min-frames guard and the claim that it finds cuts within a frame are untested.
- That the proxy keeps the hi-res's frame count and start timecode is assumed and guarded, not checked.
- The `/media` gap and the `operator_state` span flooding come from reading code, not from running the server.
- The proposed transcript-span delete hazard in `speech.py` is from reading the code and the schema, not from running it.
- The mockups use made-up footage, names and numbers (Final Four Watch, iso-3, Okonkwo come from existing docs and mockups) and drawn placeholder frames.

## Open questions for the owner

1. **Where does it live?** A fifth operator screen, Library (mocked), or inside an existing screen? Brand direction has Library as a proof; the operator UI has none.
2. **Rights on export, and on marking.** Proposal: marking and detection on any asset; export refused for not cleared and for legal hold. Is legal hold a block for an internal edit, or only for publish and air as `data-model.md` words it ("a held asset cannot air or publish")?
3. **Frame columns.** Add `frame_in`/`frame_out` with a lazy backfill job (proposed), or compare timecode strings and skip the backfill?
4. **Which NLE?** Premiere assumed from the external ref line. Is there a person who will do the manual import check, and on which Premiere version? Is a panel inside the NLE (a UXP panel that talks to this API) wanted later, or only a hand-off file?
5. **Saved selections.** Selection is per tab and disappears. Does the show need a saved bin or named selects list? That is a new object and needs a data-model decision.
6. **Mixed frame rates in one export.** Refuse (proposed), or write per-clip rates and let the NLE conform?
7. **Point markers.** Does anyone need a single-frame marker with a note, as in a live "mark this" key? Proposed as a later clip with equal in and out, not in slice 1.
8. **Thumbnail home.** Local cache directory (proposed) or through the storage backend so a second machine can show them?
9. **What the Publish screen lists.** Today it gets up to 200 spans of any kind, which means words. Should it list clips and shots instead, and not words?
10. **Detection on iso.** One fixed camera has few cuts. Should detection be offered on an iso at all, or hidden unless the camera is a program feed?
11. **Who may delete or re-detect.** There are no operator roles in the API today; any token holder can. Is that still the rule?
12. **AAF.** Needs a third-party package. Acceptable if xmeml fails, or never?

## Proposed build slicing

Small PRs, each shippable on its own, each with tests. The mockup is approved before slice 1.

1. **Schema and spans core.** Migration 9, `store.SCHEMA` and `schema.sql` parity, `volt/shots.py` with create, trim, label, delete, overlap and in-use rules, `frame_in/out` written for new spans, `duration_frames` at ingest and wrap, the kind filter on `operator_state.spans()` (decision 9 first), and the backfill job. No UI, no detection. Tests: rules, drop-frame labels, historical-shape migration, Postgres.
2. **Read API and media.** `GET .../spans`, `.../transcript`, the proxy route with Range, the thumbnail route with on-demand generation. Tests with ffmpeg testsrc.
3. **Detection job.** `shots` handler, `shot_run`, thumbnails at the end of the job, split and merge. Begins with a measurement of threshold on real footage; the default and the claim in this spec are set from it. Tests: known-cut clips at 25 and 29.97 DF.
4. **Library screen: shot list and marking.** The shot list, player, marks, selection tray, empty and error states, built on the approved mockup, with the operator-UI test harness fake data. Axe scan on the built page, not only the mockup.
5. **Transcript marking.** Word cursor, `from_words`, handles, snap. Tests on the server rule and the keyboard path.
6. **NLE export.** `volt/nle.py` (xmeml and EDL builders), `POST /api/exports/nle`, the export screen, path map setting. Arithmetic tests. Closed only after the manual Premiere import check is recorded.

Slices 1 to 3 need no UI decision beyond question 9. Slice 6 can start in parallel with 4 and 5 once slice 1 lands, since export takes span IDs. Not in this list: AAF, a second NLE, saved bins, point markers, an NLE-side panel.
