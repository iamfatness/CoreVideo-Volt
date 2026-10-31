# Volt: the remaining build, design

Status: draft for the owner's review. Nothing here is built. Date 2026-10-31.

## Why

The control plane, the Postgres runner, per-user tokens, MOS export and the redesigned operator UI are on `main`. What is left to code is the roadmap's "Next" list plus the gaps found while proving Postgres and the redesign. Live validation is a separate matter: it waits on CoreVideo Pro, a newsroom system, a deck and an NLE, none of which exist for this team yet. This document covers only work that can be built and tested without them. It says plainly, per workstream, what that testing does and does not prove.

## Goals

1. Close every open coding item: slot length and back time, scheduling (recurring and relative, with endpoints and UI), the MOS sender, a hardware playout adapter, an archive tier with restore, a second NLE format, a light theme, and the Postgres paths never exercised.
2. Each item ships behind its own tests, with a software fake of whatever it talks to, so the suite stays hermetic.
3. Each item states its conformance honestly, in its own spec, the way [mos.md](../../specs/mos.md) does.

## Non-goals

- Anything that needs CoreVideo Pro, a real NCS, a real HyperDeck, or a real NLE to prove. Those are validation runs, tracked in [product-readiness](../../product-readiness.md) and [dress-rehearsal](../../dress-rehearsal.md).
- The license choice (the owner's) and third-party notices that depend on it.
- Free-docking panels (the redesign spec ruled them out and nothing has changed).
- Receiving MOS (Volt as the device, its own later spec), AAF, a cloud archive other than the three object stores already supported, and a dashboard for the archive tier.
- Cleanup of the 37 leftover agent worktrees: housekeeping, not code.

## Anti-goals

Failure even if it works: a feature that can fire on air by accident (every new send path is opt-in, off by default and logged); a schema change with no forward migration test on both databases; a protocol client that retries a non-idempotent command; a new operator control that does not follow the state language (icon, word, colour) and the key rules; a claim of conformance that no test or real counterpart supports.

## Constraints

- Stack as today: Python standard library plus the existing optional extras; SQLite and Postgres both (`VOLT_DB`), schema now at 17, migrations numbered with no gaps and tested forward on both. No new runtime dependency except where a workstream says so.
- API additions follow `ROUTE_ROLES`: every new POST is classified, a test fails otherwise. Writes land in the audit log with the token's user.
- The operator UI stays static files under the same-origin CSP (no `http(s)://`, no inline style or script), icons from the sprite, status as icon plus word plus colour, existing keys unchanged.
- A new network client never blocks the HTTP server or the runner's other jobs: it runs in the runner or the clock, with timeouts, bounded retries for idempotent calls only, and a connection state the UI can show.
- Tests use fakes: a fake NCS socket server, a fake HyperDeck, a fake archive-capable backend, a frozen clock. No test touches the network.

## The workstreams

Order is by dependency and risk. C precedes D; H1 (the room registry) precedes D because both change the clock; I precedes the UI-adding workstreams.

### A. Closing the Postgres and clock test gaps (no schema change)

Most of the original list is already covered (checked 2026-10-31): two runners contending on Postgres (`test_postgres_hardening.py`, `PostgresRaces`), every historical schema shape forward on Postgres (`test_migrations.py`, `PostgresForward`), backup and restore with `--with-media`, `--overwrite` and an older schema (`test_data_safety.py`), `verify-storage` with missing, mismatched and empty objects (`test_data_safety.py`), two clocks and a clock restart on SQLite (`test_clock.py`). `docs/operations.md`'s "not run" list is a live-drill gap, not a test gap. What is left to write: a clock restart between claim and fire on Postgres, and a restore of a backup from the oldest supported schema on Postgres. Beyond that, "add a historical-shape forward test for the new head" is an exit criterion of every schema-changing workstream (B, D, F, G), not a separate task.

### B. Slot length and back time (schema 18)

`show.slot_seconds INTEGER NULL`. `POST /api/shows/{id}/slot` with `{seconds: int | null}` (operator role; 1 to 86399; null clears it; audited). `/rundown` and `/api/state` carry `slotSeconds`. UI: the Show screen header shows `Total 12:40 of 15:00`, then `Over 00:00` or `Under 02:20` (signed, red with an alert icon when over, neutral when under); each row gains **back time**, the time of day by which that item must start for the show to end on schedule: slot end minus the lengths of this item and those after it (not killed or skipped). Slot end is the first item's actual start plus `slotSeconds`; before anything has aired it is the first item's `scheduled_at` plus `slotSeconds`, and `--:--` if neither exists. An item of unknown length after this one makes this row `--:--`. Once items have aired, totals use planned lengths for what is left and as-run for what is done. Setting the slot is a control in the Producer workspace layout's build panel and is read-only in the Operator layout; both are the same server role (`operator`), since workspaces are layouts, not permissions. MOS `roEdDur` is unchanged. New colour states are added to both theme sets (see I).

Proves: arithmetic, API, migration, UI states. Does not prove that crews read back time the way the tool shows it.

### C. Scheduling endpoints and UI

`serve.py` does not expose `scheduled_at` today. Add `POST /api/items/{id}/schedule` with `{at: "YYYY-MM-DDTHH:MM:SSZ" | null}` (operator role, audited; D extends the body with `mode` and `offsetSeconds`), and `scheduledAt`, `scheduleState`, `scheduleNote` in each `/rundown` item (camelCase, like `asRunIn`). A schedule field and state chip on the Show row (editable in the Producer layout, read-only in Operator). Refused for an item that is `on air`, `taking`, `aired`, `skipped` or `killed`, with the `code` and `reason` pattern the other routes use. No clock change here.

### D. Recurring and relative schedules (schema 19; needs C)

**Relative.** `item.schedule_mode` in `absolute` (today), `after_previous` and `none`, and `item.schedule_offset_seconds`. Owner ruling 2026-10-31: an `after_previous` item is due at the previous item's as-run **start** plus the previous item's **planned length** plus the offset; it is never due before. (An as-run **end** cannot be the anchor: it is written only when the next item is taken, in `playout.advance`.) A previous item of unknown length counts as zero. Due-ness is computed on each tick; the single conditional UPDATE still claims.

**Recurring.** `show.recurrence TEXT NULL` holds `{"freq": "daily"|"weekly", "days": [0-6], "at": "HH:MM", "tz": "<IANA zone>"}`; no end date (owner ruling: it repeats until the rule is removed). The clock, not the HTTP server, materialises the next occurrence: when `now` is within 24 hours of it and none exists, it clones the show's items (ready, no as-run, spans attached, cues copied) into a new show titled `<title> <date>` with the first item's `scheduled_at` set. `show.recurrence_of` and `show.occurrence_on` make a second tick, and a second clock, a no-op. DST: wall-clock time in the zone; a nonexistent local time moves forward, an ambiguous one takes the first. **Dependency:** Windows has no system tz database, so add the `tzdata` package (with a `sys_platform == "win32"` marker) to the requirements. **Process:** `volt run` skips the clock when `VOLT_ROOM_URL` is unset (`supervisor.py`); recurrence cloning must run without a room, so the clock gets a clone-only mode that is started whenever a recurring show exists.

Proves: date arithmetic, DST edges, idempotence, two clocks not cloning twice. Does not prove that a newsroom wants this shape of recurrence; rules richer than daily or weekly are out.

### E. FCPXML export (second NLE, no schema change)

`nle.build_fcpxml(items, name, start_tc)` beside `build_xmeml`, FCPXML 1.10 (Final Cut Pro, DaVinci Resolve): `resources` with `format` and `asset` elements (file URL through the existing path-map), a `project` with a `sequence` and a `spine` of `asset-clip`s, offsets and durations as rational seconds (`frames*d/n s`), drop-frame start timecodes via `tcFormat="DF"`. `/api/exports/nle` accepts `format: "fcpxml"`; the Library's Export tab gets a third format radio. Golden file, well-formedness and arithmetic tests at 25, 29.97 DF, 30, 50, 59.94 and 60; hostile names escaped as in the existing exports.

Proves: structure against the published FCPXML DTD if the DTD can be fetched into the repo as a test fixture (check its license first; if not, state "structure follows the published grammar, unvalidated"). Does not prove: that Final Cut or Resolve opens it.

### F. Archive tier with restore (schema 20)

Hi-res essence lives on local disk (`essence.location` is a path); only publish uses object storage today. So archiving is an upload, not a storage-class flip. Owner ruling 2026-10-31: **upload, verify, delete the local copy; restore rehydrates and downloads back to the same path.** The archive store is the configured object store (S3, Azure Blob or GCS) in a cold class. Backend protocol gains optional `set_storage_class(key, cls)`, `restore(key, days)` and `restore_state(key) -> online | archived | restoring` (S3 `GLACIER`/`DEEP_ARCHIVE` and `restore_object`, Azure archive tier and rehydration, GCS `ARCHIVE` class where reads are immediate so restore reports `online`); `LocalBackend` and a fake simulate the states. `essence.tier` (`online`, `archiving`, `archived`, `restoring`), `essence.archive_key`, `asset.archived_at`. Runner jobs `archive` (put_file with verification, record the tier, then delete the local file, in that order, so a crash never leaves zero copies) and `restore` (start, poll `restore_state`, download with checksum verification, mark `online`). Routes `POST /api/assets/{id}/archive` and `POST /api/assets/{id}/rehydrate` (the name `restore` is taken: `/api/assets/{id}/restore` un-deletes a tombstoned asset). Operator role, audited. An archived asset's hi-res is refused by take, publish, proxy generation and export with code `archived` and the rehydrate hint; the proxy and thumbnails stay local on purpose so an editor can still browse. Legal hold blocks archive; delete and purge of an archived asset also remove the archived object. UI: Archived and Restoring chips on the Library header, Archive (two-press) and Restore buttons.

Proves: state machine, ordering, refusals, idempotence, per-backend call shapes against fakes. Does not prove real retrieval times or cost.

### G. MOS sender (new tables; needs a MOS device to validate)

Owner ruling 2026-10-31: **Volt is the NCS; build the sender first, the receiver later as its own workstream.** In MOS the NCS sends `roCreate`, `roReplace`, `roDelete`, `roStorySend` and `roMetadataReplace` to a MOS device and the device answers `roAck`; Volt's existing export is already the NCS-side `roCreate`. A client in the runner, opt-in (`VOLT_MOS_DEVICES`, a list of `name=host[:port]`; nothing set means nothing sent), sends those messages framed as MOS requires (UTF-16 big-endian XML in the `<mos>` envelope), with `messageID` from a persisted counter, `roAck` matching, heartbeat, and a connection state (`connecting`, `up`, `down`, `refused`) on the Show screen's room panel. Outbox: a new table `mos_outbox` (device, message, sequence, state, attempts) and a counter row; sends are triggered by a rundown change (item added, removed, reordered, killed, skipped, taken). A lost connection loses nothing and a restart resumes in order. Retry only messages the protocol makes idempotent (`roReplace`, `roMetadataReplace`); a `roCreate` that timed out is reconciled, not resent blind. The builder reads the 2.8.5 text for which of ports 10540 and 10541 the NCS connects to and records it in `mos.md`; it does not trust this document.

Proves: framing, ack matching, outbox order and restart, reconnect, against a fake device socket server. Does not prove acceptance by any real MOS device; fetch the spec's DTD and XSD as fixtures if their license allows.

### H. HyperDeck adapter (no schema change; needs a deck to validate)

A second room kind beside `corevideo.pro`: `room = "hyperdeck"` on a cue, with commands named in the existing dotted style (`deck.play`, `deck.stop`, `deck.goto`, `deck.record.start`, `deck.record.stop`) and a JSON payload `{"deck": "<name>", "clip": <number or name>}`. **Refactor first (H1):** dispatch, the clock, `serve.py` and `take.py` each build one `CoreVideoProRoom` today and `dispatch_item` assumes `ROOM = "corevideo.pro"`; H1 is a small room registry (name to client) used by all four, landing before D touches the clock. **Adapter (H2):** `volt/hyperdeck.py` speaks the Blackmagic HyperDeck Ethernet protocol (TCP 9993, text commands, numbered responses; the builder follows Blackmagic's published document and records its revision). A deck cue is `fired` only when the deck's reported transport state shows it, never on `200 ok`; an error code or timeout is `failed` with the code in `detail`; an unreachable deck is `failed`, never hung; an unknown deck or command is `unsupported`. Configuration: `VOLT_HYPERDECK_HOSTS` as `name=host[:port]`. **Failure rule (owner, 2026-10-31):** a deck cue that fails after the CoreVideo take has fired does not rewind the take; the cue is marked failed, the item stays on air, and the desk sees the failure in red. `volt run` must start the clock for deck-only setups.

Proves: framing and fired-on-observation against a fake deck. Does not prove real-deck timing or clip-index behaviour.

### I. Light theme (no schema change)

A second token set under `:root[data-theme="light"]` and `@media (prefers-color-scheme: light)` unless the user has chosen; a Theme control in the header (Auto, Dark, Light; `localStorage` in try/catch; Dark when storage is blocked); a small same-origin script loaded in `<head>` sets `data-theme` before first paint so there is no flash (the CSP forbids inline script). A contrast test covers both sets (text 4.5:1, status icons 3:1). **Sequenced first among the UI workstreams:** every workstream that adds a colour state (B over-time, C schedule chip, F Archived and Restoring, G connection state) adds its tokens to both sets and to the contrast test, so nothing is retrofitted. Brand colours keep their roles; red stays for live paths; amber and blue get light-surface variants measured, not eyeballed. Screenshots re-shot for both; the brand guide gains a light section only after the test passes.

## Order and size

| Order | Workstream | Size | Why here |
|---|---|---|---|
| 1 | A test gaps | small | No schema; two tests |
| 2 | I light theme and contrast test | small | Later workstreams add tokens to both sets |
| 3 | B slot length and back time | small | Finishes the redesign's open question |
| 4 | C scheduling endpoints and UI | small | D cannot be used without it |
| 5 | H1 room registry | small | Touches the clock; must land before D |
| 6 | D recurring and relative | medium | Needs C and H1; riskiest date logic |
| 7 | E FCPXML | small | Independent |
| 8 | F archive tier | medium | Schema, two job types, download path |
| 9 | G MOS sender | large | Socket client with an outbox |
| 10 | H2 HyperDeck adapter | medium | New room kind on the registry |

Each workstream gets its own plan when it starts (this document's plan, [2026-10-31-remaining-build.md](../plans/2026-10-31-remaining-build.md), sequences them and fixes the interfaces they share). Each is its own branch and PR; CI must pass on both databases before the next starts.

## Left to the builder

Exact column and index definitions; the HTTP error codes beyond the pattern; the wire details of each protocol (read the published document, not this one); the fake servers' internals; the screen layout of each new control within the redesign's rules.

## Calls I made, for the owner to check

- Your answers: HyperDeck first, FCPXML second, archive = upload, verify, delete local, restore back; slot length in seconds; Volt is the MOS NCS with the sender first; relative items due at previous start plus its planned length plus offset; recurrence has no end date; a failed deck cue never rewinds the take.
- Light theme is in and goes early; free docking stays out.
- Recurrence is daily and weekly only, cloned 24 hours ahead with cues copied.
- Skipped and killed items take no time in back time, as in front time.
- FCPXML is "unvalidated" from the start: Apple does not publish a vendorable DTD.
- A builder check against the repo corrected five claims in the first draft (relative-schedule anchor, MOS roles, the `restore` route name, where hi-res lives, and that most of workstream A already exists).

## Open questions

1. Should a cloned recurring show carry last run's cues, or arrive with them cleared? Default here: copied.
2. Does the MOS receiver (Volt as the device) belong in this release or after it? Default: after, as its own spec.
