# Volt: the remaining build, sequencing plan

> Umbrella plan for [the spec](../specs/2026-10-31-remaining-build-design.md). Each workstream gets its own detailed plan (writing-plans skill) when it starts; this page fixes order, shared interfaces and exit criteria. Not yet reviewed by a builder-check subagent.

**Rule for every workstream:** own branch and PR; TDD with fakes (no network); forward migration tested on SQLite and Postgres; every new POST classified in `ROUTE_ROLES` and audited; UI follows the state language and CSP rules; suite and CI green before the next starts; its spec states what the tests do not prove.

## Shared interfaces

- Schema head moves 17 -> 18 (B) -> 19 (D) -> 20 (F). G adds a table, `mos_message`, in whichever migration is next when it starts (21).
- Room kinds in `volt/cues.py`/`volt/dispatch.py`: `corevideo.pro` today; H adds `hyperdeck` with its own command set and a fired-on-observation rule.
- Runner job types: F adds `archive` and `restore`; G adds a sender loop that reads an outbox table.
- `schedule_state` and `schedule_note` become part of the `/rundown` item payload in C and are consumed by D.

## Workstreams, in order

1. **A. Hardening** — tests: two runners contending (one claim), clock restart between claim and fire, backup/restore with `--with-media` and `--overwrite`, `verify-storage` (missing, mismatched, empty), restore of every older schema to head. Files: `tests/test_pg_*.py`, `tests/test_backup.py`. Exit: all pass on Postgres via `VOLT_TEST_PG_DSN`, skipped without.
2. **B. Slot length and back time** — migration 18 `show.slot_seconds`; `POST /api/shows/{id}/slot`; `slotSeconds` in `/rundown` and `/api/state`; pure `backTimes(items, seconds, slotSec, startMs)` in `operator.js` with node tests; header `Total x of y` and over/under; back-time column; Producer sets, Operator read-only. Exit: arithmetic tests, API tests, axe clean in three workspaces (reload per workspace).
3. **C. Scheduling endpoints and UI** — `POST /api/items/{id}/schedule`; payload fields; Show row field and chip. Exit: refusal tests for on-air/aired/skipped/killed.
4. **D. Recurring and relative** — migration 19 (`item.schedule_mode`, `schedule_offset_seconds`, `show.recurrence`, `recurrence_of`, `occurrence_on`); clock due-ness for `after_previous`; clock-side clone with idempotence; `zoneinfo` DST tests (spring forward, fall back); two clocks never clone twice. Exit: frozen-clock tests on both databases.
5. **E. FCPXML** — `nle.build_fcpxml`, `format: "fcpxml"` in `parse_request`, Export-tab radio, golden file, rate matrix (25, 29.97 DF, 30, 50, 59.94, 60), escaping. Exit: well-formed, DTD-checked if the DTD may be vendored.
6. **F. Archive tier** — migration 20 (`essence.tier`, `asset.archived_at`); backend `set_storage_class`/`restore`/`restore_state` for S3, Azure, GCS (fakes) and Local; jobs `archive`, `restore`; routes `/api/assets/{id}/archive|restore`; `archived` refusal in take, publish, proxy, export; Library chip and buttons. Exit: state-machine and refusal tests; legal hold blocks archive.
7. **G. MOS sender** — `volt/mos_send.py` (UTF-16BE framing, `roReplace`, `roDelete`, `roStorySend`, `roMetadataReplace`, heartbeat, ack matching), outbox and `mos_message` table, connection state on the room panel, fake NCS socket server in tests. Read the 2.8.5 spec for port roles before choosing 10540 vs 10541; record in `mos.md`. Exit: ordering, restart, reconnect, and no blind retry of `roCreate`.
8. **H. HyperDeck adapter** — `volt/hyperdeck.py`, room kind `hyperdeck`, commands `deck.play|stop|goto|record|record.stop`, `VOLT_HYPERDECK_HOSTS`, fake deck, fired only when transport state shows it. Exit: framing and timeout tests; unreachable deck fails the cue, never hangs.
9. **I. Light theme** — light token set, Theme control (Auto/Dark/Light), contrast test over both sets, re-shot screenshots, brand-guide section after the test passes.

## Review Focus (carried into each detailed plan)

- Anything that can send on air or to a device must be off by default and visible when on.
- Date logic across DST and across a midnight boundary.
- Restart at every step of a claim, send or restore: nothing fires twice, nothing is lost.
- Every unvalidated claim worded as unvalidated.

## Open decisions for the owner

See the spec's Open questions: whether G and H wait, and whether a cloned recurring show keeps its cues.
