# Volt: the remaining build, sequencing plan

> Umbrella plan for [the spec](../specs/2026-10-31-remaining-build-design.md), revised after a builder check against the repo. Each workstream gets its own detailed plan (writing-plans skill) when it starts; this page fixes order, shared interfaces and exit criteria.

**Rule for every workstream:** own branch and PR; TDD with fakes (no network); a historical-shape forward-migration test for the new head on SQLite and Postgres; every new POST classified in `ROUTE_ROLES` and audited; UI follows the state language and CSP rules and adds its colour tokens to both theme sets and the contrast test; suite and CI green before the next starts; its spec states what the tests do not prove.

## Shared interfaces

- Schema head 17 -> 18 (B) -> 19 (D) -> 20 (F) -> 21 (G: `mos_outbox` and a counter row).
- Roles are `operator` and `admin` only; "Producer" and "Operator" are UI layouts, never server roles.
- Payload casing is camelCase (`scheduledAt`, `scheduleState`, `slotSeconds`).
- A room registry (H1) replaces the single `CoreVideoProRoom` in `dispatch.py`, `clock.py`, `serve.py` and `take.py`; `ROOM = "corevideo.pro"` stops being a constant.
- `/api/assets/{id}/restore` already un-deletes a tombstoned asset; the archive route is `/rehydrate`.
- Add the `tzdata` requirement (win32 marker) in D.

## Workstreams, in order

1. **A. Test gaps** (most of the original list already exists; see the spec) — write: a clock restart between claim and fire on Postgres (`test_clock.py`, `VOLT_TEST_PG_DSN`), a restore of the oldest supported schema on Postgres (`test_data_safety.py`). Exit: pass on Postgres, skipped without.
2. **I. Light theme and contrast test** — second token set, Theme control, pre-paint external script, contrast test over both sets, screenshots. Exit: contrast test, axe clean in both themes in each workspace (reload per workspace).
3. **B. Slot length and back time** — migration 18; `POST /api/shows/{id}/slot`; `slotSeconds`; pure `backTimes` with node tests (anchor rules in the spec); header and row UI. Exit: arithmetic, API, UI states, new tokens in both themes.
4. **C. Scheduling endpoints and UI** — `POST /api/items/{id}/schedule`; `scheduledAt`/`scheduleState`/`scheduleNote`; row field and chip; refusals for on air, taking, aired, skipped, killed.
5. **H1. Room registry** — a name-to-client registry used by dispatch, clock, serve and take; behaviour unchanged for `corevideo.pro`; `volt run` clock start condition updated. Exit: existing suite unchanged and green.
6. **D. Relative and recurring schedules** — migration 19; `after_previous` due at previous as-run start + previous planned length + offset; recurrence clone with `zoneinfo` and DST tests; clone-only clock mode without a room; `tzdata`. Exit: frozen-clock tests on both databases, two clocks never clone twice.
7. **E. FCPXML** — `nle.build_fcpxml`, `format: "fcpxml"`, Export-tab radio, golden file, rate matrix. Exit: well-formed; labelled unvalidated.
8. **F. Archive tier** — migration 20; backend cold-class and restore methods (fakes); jobs `archive` (upload, verify, then delete local) and `restore` (rehydrate, download, verify); routes `/archive` and `/rehydrate`; `archived` refusals; Library chips and buttons. Exit: crash-ordering test (never zero copies), refusals, legal hold.
9. **G. MOS sender (Volt as NCS)** — migration 21; `volt/mos_send.py`; outbox; ack matching; connection state; fake device server. Read the 2.8.5 text for the port roles first. Exit: order, restart, reconnect, no blind `roCreate` resend.
10. **H2. HyperDeck adapter** — `volt/hyperdeck.py`, room kind `hyperdeck`, `deck.*` commands, JSON payload, fake deck, fired only on observed transport state, failed deck cue never rewinds the take. Exit: framing, timeout, unreachable-deck tests.

## Review Focus (carried into each detailed plan)

- Anything that can send on air or to a device is off by default and visible when on.
- Date logic across DST and across a midnight boundary.
- Restart at every step of a claim, send, archive or restore: nothing fires twice, nothing is lost, no moment with zero copies of a file.
- Every unvalidated claim worded as unvalidated.

## Open decisions for the owner

Whether a cloned recurring show keeps its cues (default: yes); whether the MOS receiver belongs in this release (default: later, own spec).
