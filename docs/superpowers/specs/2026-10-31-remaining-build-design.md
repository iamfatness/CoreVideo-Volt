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
- Receiving MOS (Volt as the NCS), AAF, a cloud archive other than the three object stores already supported, and a dashboard for the archive tier.
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

Order is by dependency and risk. A and B do not depend on the others; D needs C; the rest are independent.

### A. Hardening the Postgres and storage paths (no schema change)

Tests and small fixes for what the 2026-10-30 Postgres run did not exercise: two runners contending for one job (exactly one claims), a clock restart between claim and fire (the item is not fired twice and not lost), `volt backup`/`restore` with `--with-media` and `--overwrite`, `verify-storage` against a fake backend with a missing, a mismatched and an empty object, and restore of a backup taken at an older schema (every historical shape forward to 17, then to the new head). These run under `VOLT_TEST_PG_DSN` and are skipped without it, as the other Postgres tests are. A real managed cloud Postgres is a validation run and stays out.

### B. Slot length and back time (schema 18)

`show.slot_seconds INTEGER NULL`. `POST /api/shows/{id}/slot` with `{seconds: int | null}` (operator role; 1 to 86399; null clears it). `/rundown` and `/api/state` carry `slotSeconds`. UI: the Show screen header shows `Total 12:40 of 15:00`, then `Over 00:00 / Under 02:20` (signed, red when over, neutral when under, icon plus word); each rundown row gains **back time**, the time of day by which that item must start for the show to end on schedule, computed from the slot end (the first item's actual start plus `slotSeconds`) minus the lengths of this item and those after it, not killed or skipped; `--:--` when unknown. A producer sets the slot from the Producer layout's build panel; the Operator layout shows it read-only. MOS `roEdDur` is unchanged (it stays the sum of spans).

Proves: arithmetic, API, migration, UI states. Does not prove: that crews read back time the way the tool shows it; the redesign spec records that colour conventions for over/under are not standardised.

### C. Scheduling endpoints and UI

`serve.py` does not expose `scheduled_at` today. Add `POST /api/items/{id}/schedule` with `{at: "YYYY-MM-DDTHH:MM:SSZ" | null}` (operator role, audited), `schedule_state` and `schedule_note` in `/rundown` items, and a schedule field and state chip on the Show screen's row (Producer layout; read-only in Operator). Refused for an item that is on air, aired, skipped or killed, with the `code` and `reason` pattern the other routes use. No clock change here.

### D. Recurring and relative schedules (schema 19; needs C)

Relative: `item.schedule_mode` in `absolute` (today), `after_previous` and `none`, and `item.schedule_offset_seconds`. An `after_previous` item is due when the previous item's as-run out exists and `now >= as_run_out + offset`; it is never due before. The clock gains no new claim rule: due-ness is computed, the single conditional UPDATE still takes it.

Recurring: a show can repeat. `show.recurrence TEXT NULL` holds a small JSON rule `{"freq": "daily"|"weekly", "days": [0-6], "at": "HH:MM", "tz": "Europe/London"}` (IANA zone; Python `zoneinfo`; no new dependency). The clock, not the HTTP server, materialises the next occurrence: when `now` is within 24 hours of the next occurrence and none exists, it clones the show's current items (ready, no as-run, cues copied, span attached) into a new show titled `<title> <date>` with the first item's `scheduled_at` set. Cloning is idempotent: `show.recurrence_of` and `show.occurrence_on` make a second tick a no-op. Daylight-saving days use the zone's wall-clock time; a nonexistent local time moves forward, an ambiguous one takes the first.

Proves: the date arithmetic, DST edges, idempotence, two clocks not cloning twice. Does not prove: that a newsroom wants this shape of recurrence; a rule more complex than daily or weekly is out.

### E. FCPXML export (second NLE, no schema change)

`nle.build_fcpxml(items, name, start_tc)` beside `build_xmeml`, FCPXML 1.10 (Final Cut Pro, DaVinci Resolve): `resources` with `format` and `asset` elements (file URL through the existing path-map), a `project` with a `sequence` and a `spine` of `asset-clip`s, offsets and durations as rational seconds (`frames*d/n s`), drop-frame start timecodes via `tcFormat="DF"`. `/api/exports/nle` accepts `format: "fcpxml"`; the Library's Export tab gets a third format radio. Golden file, well-formedness and arithmetic tests at 25, 29.97 DF, 30, 50, 59.94 and 60; hostile names escaped as in the existing exports.

Proves: structure against the published FCPXML DTD if the DTD can be fetched into the repo as a test fixture (check its license first; if not, state "structure follows the published grammar, unvalidated"). Does not prove: that Final Cut or Resolve opens it.

### F. Archive tier with restore (schema 20)

Cold storage through the object stores already supported. Backend protocol gains optional `set_storage_class(key, cls)`, `restore(key, days)` and `restore_state(key) -> online | archived | restoring`; S3 maps to `GLACIER`/`DEEP_ARCHIVE` and `restore_object`, Azure to the archive access tier and rehydration, GCS to `ARCHIVE` class (immediate access, so restore is a no-op reporting `online`). `LocalBackend` and a test fake simulate the states. `essence.tier` (`online`|`archiving`|`archived`|`restoring`) and `asset.archived_at`. Two runner job types, `archive` (moves hi-res essence, verifies by stat, then records the tier) and `restore` (starts a restore, polls `restore_state`, marks `online`). `POST /api/assets/{id}/archive` and `/restore` (operator role, audited). An archived asset's hi-res is refused by take, publish, proxy and export with `archived` and the restore hint; the proxy and thumbnails stay online by design so an editor can still browse. Legal hold blocks archive. UI: a state chip on the Library header (Archived, Restoring) and an Archive or Restore button with two-press arming for archive.

Proves: the state machine, refusals, job idempotence, per-backend call shapes against fakes. Does not prove: real retrieval times or cost; those depend on the provider and the tier.

### G. MOS sender (no schema change; needs an NCS to validate)

A client in the runner, opt-in (`VOLT_MOS_NCS=host`), that sends the messages Volt already knows how to build plus the ones it needs: `roCreate` (exists), `roReplace`, `roDelete`, `roStorySend`, `roMetadataReplace` and `heartbeat`, framed as MOS requires (UTF-16 big-endian XML, `<mos>` envelope, `messageID` counter persisted in `mos_message`, a new table, so ids survive a restart), with ack handling (`roAck`) and a connection state (`connecting`, `up`, `down`, `refused`) shown on the Show screen's room panel. Sends are triggered by a rundown change (item added, removed, reordered, killed, skipped, taken) through an outbox row, so a lost connection loses nothing and a restart resumes; nothing is sent while the sender is off. Ports follow the 2.8.5 roles for a MOS device talking to an NCS; the builder reads the spec text before choosing which of 10540 and 10541 is which, and records the answer in `mos.md`. Retries only for messages the protocol defines as idempotent (`roReplace`, `roMetadataReplace`); a `roCreate` that timed out is reconciled by asking the NCS, not resent blind.

Proves: framing, ack matching, outbox ordering and restart, reconnect with backoff, against a fake NCS socket server. Does not prove: acceptance by any real NCS (the 2.8.5 DTD and XSD in sections 8 and 9 of the spec are the best offline check; fetch them as fixtures if their license allows).

### H. HyperDeck adapter (no schema change; needs a deck to validate)

A second room kind beside `corevideo.pro`: `room = "hyperdeck"` on a cue, with commands `deck.play`, `deck.stop`, `deck.goto` (clip number or name) and `deck.record`, `deck.record.stop`. `volt/hyperdeck.py` speaks the Blackmagic HyperDeck Ethernet protocol (TCP 9993, line text commands, numbered responses; the builder follows Blackmagic's published protocol document and records its revision in the spec). The adapter honours the existing dispatch contract: a cue is `fired` only when the deck's reported transport state shows it (`play` then `transport info` reports `play`), never on the `200 ok`; a timeout or an error code makes it `failed` with the code in `detail`; a deck that is not reachable makes the cue `failed`, not hung. Configuration: `VOLT_HYPERDECK_HOSTS` as `name=host[:port]` pairs; the cue payload names the deck. Unknown deck or command is `unsupported`. The take rewind rules in [playout.md](../../specs/playout.md) apply unchanged.

Proves: protocol framing and the fired-on-observation rule against a fake deck. Does not prove: real-deck timing, clip-index behaviour on different models, or behaviour on a deck mid-record.

### I. Light theme (no schema change)

A second token set for `operator.css` under `:root[data-theme="light"]` and `@media (prefers-color-scheme: light)` unless the user has chosen, a Theme control in the header (Auto, Dark, Light, kept in `localStorage` with try/catch, Dark when storage is blocked), and a contrast test over both sets (text 4.5:1, status icons 3:1 on their surfaces). Brand colours keep their roles; red stays for live paths; amber and blue get light-surface variants measured, not eyeballed. Screenshots re-shot for both. The brand guide gains a light section only after the contrast test passes.

## Order and size

| Order | Workstream | Size | Why here |
|---|---|---|---|
| 1 | A hardening | small | No schema, protects everything after it |
| 2 | B slot length and back time | small | Finishes the redesign's one open question |
| 3 | C scheduling endpoints and UI | small | D cannot be used without it |
| 4 | D recurring and relative | medium | Needs C; the riskiest date logic |
| 5 | E FCPXML | small | Independent; mirrors the existing export |
| 6 | F archive tier | medium | Schema and two job types |
| 7 | G MOS sender | large | Socket client with an outbox |
| 8 | H HyperDeck adapter | medium | New room kind |
| 9 | I light theme | small | Last because it touches every screen's contrast |

Each workstream gets its own plan when it starts (this document's plan, [2026-10-31-remaining-build.md](../plans/2026-10-31-remaining-build.md), sequences them and fixes the interfaces they share). Each is its own branch and PR; CI must pass on both databases before the next starts.

## Left to the builder

Exact column and index definitions; the HTTP error codes beyond the pattern; the wire details of each protocol (read the published document, not this one); the fake servers' internals; the screen layout of each new control within the redesign's rules.

## Calls I made, for the owner to check

- Hardware first is a HyperDeck, FCPXML is the second NLE, the archive tier is the object stores' cold classes, slot length is seconds (your answers, 2026-10-31).
- Light theme is in, free docking stays out.
- Recurrence is daily and weekly only, materialised by cloning into a new show 24 hours ahead; no cron-style rules.
- The MOS sender is outbox-driven and opt-in; sending nothing is the default.
- Everything unvalidated against real equipment is labelled so in its spec, and none of it runs by default.

## Open questions

1. Is a MOS sender to a real NCS likely in the first year? If not, G and H can move to the end of the list or wait; neither blocks a beta.
2. For recurrence, should a cloned show keep the previous run's cues exactly, or arrive with cues cleared for the producer to re-arm? The default here is cues copied.
