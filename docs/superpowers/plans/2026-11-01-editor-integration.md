# Volt editor integration: export verification, then the Premiere Pro panel

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the three NLE exports in real editors with a repeatable check, then ship an Adobe Premiere Pro (UXP) panel that signs in by device code, searches and browses the Library with proxies, places proxies and in/out ranges, round-trips markers as spans, and relinks to hi-res on publish, with the rights gate enforced by the server.

**Architecture:** Server side in `control/volt/` (new `panel_auth.py`, `panel.py`, `editor_check.py`; edits to `serve.py`, `migrations.py`, `store.py`, `schema.sql`, `shots.py`, `media.py`). Panel in a new `panel/premiere/` (CommonJS, Node `node:test`): `core/` pure logic, `adapters/ppro.js` the only file that touches Adobe, `adapters/fake_ppro.js` the test double. Design: `docs/superpowers/specs/2026-11-01-editor-integration-design.md`.

**Tech Stack:** Python `unittest` in `control/tests/` (the `Api` harness of `tests/test_operator_api.py`, `PgCase` for Postgres via `VOLT_TEST_PG_DSN`), Node built-in test runner for the panel (skipped without `node`, as `test_operator_ui.py` does), ffmpeg `testsrc` for fixtures (skipped without ffmpeg). Full suite: `cd control && python -m unittest discover -s tests -v`.

## Global Constraints

- Own branch and PR per task. TDD with fakes. No test touches the network or a real Premiere or Resolve.
- Anything that needs a real editor is written as unvalidated, in the doc it lands in and in the UI where a user could be misled. No claim of "works in Premiere" without a dated, versioned entry in the results table.
- Schema head is 21. The one new migration is 22, numbered with no gap, with a historical-shape forward test on SQLite and Postgres (`tests/test_migrations.py` `PostgresForward`, `tests/historical_shapes.py`).
- Every new POST is in `ROUTE_ROLES` and audited. `tests/test_users.py::Registry.test_every_post_route_has_an_explicit_role` must pass; its value-set assertion is amended once, in Task 4, to allow `open` for exactly `act_panel_device_code` and `act_panel_token`.
- Panel principals are default-deny: an allowlist of routes, enforced in `serve.py`, pinned by a test that walks the real routing tables.
- Server sends no CORS headers and the CSP on Volt's pages does not change. CORS is added only if Task 3 proves the host needs it, behind `VOLT_PANEL_ORIGINS`, exact origins, never a wildcard, never credentials.
- The existing marking and export ruling stands: marking and edit-purpose media are allowed on any asset; publish-purpose hi-res needs cleared and not held.
- Volt pages added here follow the state language, the same-origin CSP (no inline script or style, no external URL), and add colour tokens to both theme sets and the contrast test.
- Python stdlib only on the server. The panel has no runtime npm dependency unless Task 9 records why.

## Review Focus

- A panel token reaching any route off the allowlist, including a GET added later and the legacy `/assets`, `/seek`, `/api/state`.
- Hi-res handed out for an asset that is archived, still open, not local, or (purpose `publish`, which is the default) not cleared or on legal hold; and a client that omits `purpose`.
- Marker retries: a lost response then a resend creating a second span; a marker deleted in Premiere deleting or editing a Volt clip or shot; `clientRef` lost when the user edits the marker name.
- Time mapping: off-by-one at 29.97 drop-frame minute boundaries, media-start versus start-timecode origin in Premiere, a one-frame span, a rate mismatch between item and sequence.
- Unvalidated claims worded as validated (the exports, Adobe call names, CORS and `fetch` behaviour), and the two unauthenticated POSTs being used to fill the table or guess a user code.

---

### Task 1: Verification kit (fixtures, expected numbers, comparer)

**Files:**
- Create: `control/volt/editor_check.py`, `control/tests/test_editor_check.py`
- Modify: `docs/specs/shot-spans.md` ("Manual import check": point to the kit)

**Interfaces:**
- `RATES = ("25", "30000/1001", "60000/1001")`; `@dataclass Fixture: out_dir: Path, assets: dict[str, str], span_ids: list[str], expected: dict`.
- `make_fixture(out_dir: Path, rates=RATES, run=subprocess.run, db_path: str | None = None) -> Fixture`: builds `testsrc` clips (burnt-in timecode, cuts every 2 s, start `01:00:00:00`, or `01:00:00;00` for the drop-frame rate), mints cleared assets in a throwaway DB, creates spans: first clip, one crossing a drop-frame minute, a one-frame span, a span labelled `Q&A "<b>" it's`, and writes `<rate>.xmeml.xml`, `<rate>.edl`, `<rate>.fcpxml` via `nle.export`, plus `expected.json`.
- `expected_for(store, span_ids, rate) -> dict`: `{"rate": "30000/1001", "dropFrame": true, "startTc": "01:00:00;00", "clips": [{"name", "srcInFrames", "srcOutFrames", "tcIn", "tcOut", "recordStartFrames", "order"}]}`, in/out as frames from the start of the file (the xmeml rule).
- `compare(expected: dict, dump: dict) -> list[Verdict]`, `Verdict(clip: int, field: str, ok: bool, expected, got)`. Dump shape: `{"editor", "version", "os", "sequence": {"rate", "startFrames", "clips": [{"name", "srcInFrames", "srcOutFrames", "recordStartFrames", "mediaPath"}]}}`.
- CLI: `python -m volt.editor_check make <dir>` and `compare <expected.json> <dump.json>` (exit 1 on any failed verdict).

- [ ] **Step 1: Write the failing tests** in `test_editor_check.py`. `Expected.test_in_out_equal_span_frames_minus_essence_start` (29.97 DF clip start `01:00:00;00`; the minute-crossing span's `srcInFrames` is hand-computed 1800-frame-minute arithmetic, value written in the test, not derived). `Expected.test_one_frame_span_has_out_equal_in_plus_one`. `Compare.test_identical_dump_passes_every_field`. `Compare.test_off_by_one_in_point_fails_only_that_clip_and_field`. `Compare.test_missing_clip_and_extra_clip_are_reported`. `Compare.test_name_is_compared_after_the_editor_may_truncate` (a name cut to the editor's limit passes only when the dump says `nameLimit`). `Fixture.test_commands_are_built_not_run` (a fake `run` records ffmpeg argv: rate, `testsrc`, `-timecode`). `Fixture.test_real_ffmpeg_makes_three_files_per_rate` (`skipUnless(shutil.which("ffmpeg"))`; each export parses as XML where XML; EDL has 3 events).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_editor_check -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module; reuse `nle.export`, `shots.create`, `timecode` helpers. No network.
- [ ] **Step 4: Run** the new tests and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Editor check: fixtures, expected numbers and comparer`.

---

### Task 2: Manual import runs, recorded (needs a real editor and a person)

**Files:**
- Modify: `docs/specs/shot-spans.md` (replace "Result: not yet recorded." with a results table; update Status and the Not verified list), `docs/roadmap.md`, `control/volt/session/operator/operator.js` (`FORMAT_VERIFIED` -> per-format map), `control/tests/test_operator_ui.py` (line ~188 pin)
- Modify only if a defect is found: `control/volt/nle.py` with a golden file under `control/tests/golden/`

**Interfaces:** `const FORMAT_VERIFIED = {xmeml: false, edl: false, fcpxml: false};` read where `lib-x-msg` and the UNVERIFIED notice are painted (`operator.js` ~2995 to 3005); a format shows the notice until its own entry is true.

- [ ] **Step 1: Generate** `python -m volt.editor_check make D:\scratch\editor-check` on the dev box. Copy to the editor machine; set `VOLT_EXPORT_PATH_MAP` if the media path differs.
- [ ] **Step 2: Manual checklist, per editor and version** (Premiere current and previous release; Resolve current; Final Cut only if a Mac exists). Do the ten steps already in `shot-spans.md` "Manual import check" for each format and each rate, and also: (a) the hostile-label clip's name is intact or sanitised, not an import error; (b) the minute-crossing and one-frame spans' source in/out equal `expected.json` to the frame; (c) media is online on a second machine through the path map; (d) FCPXML imports in Resolve and the rate in the project matches; (e) save the project, reopen, check nothing went offline; (f) Premiere: run "Dump active sequence" from Task 3's dev panel if it exists, save the JSON, and run `python -m volt.editor_check compare expected.json dump.json`, attach the output.
- [ ] **Step 3: Record** one row per editor, version, OS, format, step, pass or fail, what was fixed by hand, who, date. Each defect gets its own commit (golden-file test first, then the `nle.py` fix).
- [ ] **Step 4: Failing test first for the constant:** `test_operator_ui` asserts `FORMAT_VERIFIED` is an object with the three keys, each false until its row says pass. Run `cd control && python -m unittest tests.test_operator_ui -v`; implement; flip a key only for a format with a full pass on record.
- [ ] **Step 5: Verify** the full suite passes. **Commit** `Record NLE import results; FORMAT_VERIFIED per format`.

Not validated by any step here: Avid (recorded as "not tested, EDL only if ever"), editor versions not on the table, audio playback correctness beyond the human check.

---

### Task 3: Premiere UXP spike and the Adobe answers (needs a real Premiere)

**Files:**
- Create: `panel/premiere/spike/manifest.json`, `panel/premiere/spike/index.html`, `panel/premiere/spike/spike.js`, `panel/premiere/spike/stub_server.py`, `docs/specs/editor-integration.md` (the findings, with the Adobe version and date on every line)

**Interfaces:** `stub_server.py` is a throwaway `http.server` that logs method, path, every request header (especially `Origin`, `Authorization`, `Range`), answers `OPTIONS` with 204 and no CORS headers and serves a 5 MB file with `Range`. `spike.js` has one button per question and writes each result to a text area and to a file in the plugin data folder.

- [ ] **Step 1: Read** the eight documentation pages listed in the spec ("Documentation pages the builder must read"). Write the exact URLs and versions read at the top of `editor-integration.md`.
- [ ] **Step 2: Load the spike through the UXP Developer Tool.** Answer and record each, pass or fail with the observed value: (1) minimum Premiere version that loads a UXP panel; (2) does `fetch` to `http://127.0.0.1:<port>` and to an https host work, and is the manifest domain allowlist the gate; (3) does an `OPTIONS` preflight arrive, and what is `Origin`; (4) can a `Range` body of 5 MB be written to a file without holding it all in memory; (5) can a panel open a URL in the system browser; (6) is there secure storage; (7) can it listen on a TCP port (expected no); (8) are project-item in/out and marker times relative to media start or to the file's start timecode (use a file with start `01:00:00:00`); (9) the tick rate and the call to turn ticks into frames at 29.97; (10) import a file into a named bin; (11) set in/out on a project item, then insert and overwrite at a time on a track; (12) create, read, edit and delete a clip marker and a sequence marker, with name, comment, duration and colour; (13) switch a project item to another file (change media path or attach proxy), and what happens to markers and in/out afterwards; (14) does `<video>` play an H.264 mp4 proxy inside the panel; (15) is a menu command available for the dev dump; (16) what the `.ccx` packaging step and any account, fee or certificate requirement turned out to be.
- [ ] **Step 3: Decide** and write at the end of `editor-integration.md`: device code stays or becomes loopback; CORS needed or not; the inline player or thumb-strip decision; which relink call Task 9 uses; the frame origin rule the adapter implements; anything in the spec that was wrong. If any of (7), (12) or (13) fails in a way the design cannot absorb, stop and tell the owner before Task 8.
- [ ] **Step 4: Verify** nothing in `control/` changed. **Commit** `Premiere UXP spike and findings`.

All of this is unvalidated until a person with Premiere does it; the file says so at the top.

---

### Task 4: Panel tokens, device-code routes and the scope gate

**Files:**
- Create: `control/volt/panel_auth.py`, `control/tests/test_panel_auth.py`
- Modify: `control/volt/migrations.py` (22), `control/volt/store.py` (SCHEMA snapshot next to `volt_user`), `control/volt/schema.sql`, `control/volt/serve.py` (`Principal.scope`, `_bearer_principal`, `do_POST` open routes, `ROUTE_ROLES`, `_AUDIT_KIND`, scope check, `/api/whoami`), `control/tests/test_users.py` (role-set assertion), `control/tests/test_migrations.py`, `control/tests/historical_shapes.py`, `docs/specs/operator-api.md`

**Interfaces:**
- Tables: `panel_grant(device_hash PK, user_code UNIQUE, state, label, user_name, scope, created_at, expires_at, last_poll_at)`, `panel_token(id PK, token_hash UNIQUE, user_name, scope, label, created_at, expires_at, last_used_at, revoked_at)`; the migration also adds `span.client_ref TEXT` and the unique index `span_client_ref ON span (asset_id, client_ref)` (used in Task 6).
- `panel_auth.PREFIX = "vpt_"`, `SCOPE = "panel"`, `TOKEN_DAYS = 30`, `CODE_MINUTES = 10`, `INTERVAL = 5`.
- `create_grant(store, label: str, client: str, now: datetime) -> dict` (`deviceCode, userCode, verifyUrl, expiresIn, interval`), `approve(store, user_code, user_name, allow: bool, now) -> dict`, `exchange(store, device_code, now) -> dict` (state, or `token`, `expiresAt`, `scope`, `user` once), `authenticate(store, token, now) -> Optional[users.User]` (extra attribute `scope`, role capped at `operator`, refuses a revoked, expired token or a disabled user), `revoke(store, token_id: str | None, token_hash: str | None)`, `list_tokens(store) -> list[dict]`, `class Limits` (per-client hourly cap 30, pending cap 200, `slow_down`).
- Routes: `POST /api/panel/device-code` (`act_panel_device_code`, `"open"`), `POST /api/panel/token` (`act_panel_token`, `"open"`), `POST /api/panel/approve` (`act_panel_approve`, `"operator"`), `POST /api/panel/revoke` (`act_panel_revoke`, `"operator"`; revokes the presenting panel token, or `{id}` for an admin), `GET /api/panel/tokens` (admin). `PANEL_ALLOW` is one tuple of (method, regex) in `serve.py`.
- Error codes: `authorization_pending`, `slow_down`, `denied`, `expired`, `rate_limited` (429), `forbidden_scope` (403).

- [ ] **Step 1: Write the failing tests** in `test_panel_auth.py`. `DeviceFlow.test_pending_then_approved_returns_token_once_and_a_second_poll_is_expired`. `DeviceFlow.test_token_is_stored_hashed_and_plaintext_never_listed`. `DeviceFlow.test_deny_and_ten_minute_expiry`. `DeviceFlow.test_poll_faster_than_interval_is_slow_down`. `DeviceFlow.test_user_code_is_single_use_and_alphabet_has_no_lookalikes`. `Limits.test_31st_code_from_one_client_in_an_hour_is_429_and_201st_pending_is_429`. `Auth.test_disabled_user_token_is_refused_at_once`. `Auth.test_revoked_and_expired_token_are_401`. `Auth.test_admin_users_panel_token_has_role_operator`. `Scope.test_panel_token_reaches_only_the_allowlist` (iterates `serve.ROUTES` for every POST and a fixed list of GET paths including `/api/state`, `/assets`, `/seek`, `/api/audit`, `/metrics`, `/rundown`; each off-list returns 403 `forbidden_scope`; each on-list route is not 403). `Scope.test_user_token_is_unchanged` (a `vtk_` token still reaches show control). `Routes.test_open_routes_are_exactly_two_and_nothing_else_skips_the_gate`. `Routes.test_every_new_post_is_audited` (rows with actor, action, kind `panel`; open routes use actor `panel`). `Audit.test_approve_row_names_the_user_and_label`. Amend `test_users.py` to allow `open` for exactly the two names. Add `Migration22` to `test_migrations` (historical shape forward on SQLite and Postgres; the new tables and column exist; `span.client_ref` unique with nulls distinct).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_panel_auth tests.test_users tests.test_migrations -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `_bearer_principal`, branch on the `vpt_` prefix before the user-token lookup. `do_POST`: the two open names bypass `_gate()` and the actor logic (actor `panel`), everything else unchanged. Add the scope check after route lookup for a panel principal. `/api/whoami` reports `scope`.
- [ ] **Step 4: Run** the three modules, then the full suite, and `VOLT_TEST_PG_DSN=... python -m unittest tests.test_migrations -v` if a Postgres is available (skipped otherwise, say so in the PR). Expected: PASS.
- [ ] **Step 5: Docs:** add the five routes, the `open` role and the panel-token section to `operator-api.md`. **Commit** `Panel tokens: device-code sign-in and a default-deny scope`.

---

### Task 5: Panel library, detail, rights and hi-res manifest

**Files:**
- Create: `control/volt/panel.py`, `control/tests/test_panel_library.py`
- Modify: `control/volt/serve.py` (GET routes and `PANEL_ALLOW`), `docs/specs/operator-api.md`

**Interfaces:**
- `panel.library(store, query: dict) -> dict` (`items[]` as in the spec: `assetId, show, camera, createdAt, rate, start, durationFrames, open, hasProxy, tier, cleared, legalHold, blockedBy, thumbSpanId, hits[]`; `next`), params `q, show, camera, rights, after, limit` (default 25, max 100; unknown parameter 422 `bad_input`).
- `panel.asset_detail(store, asset_id: str) -> dict` (`media.header` plus `spans` kinds `clip, shot, marker`, `proxy: {available, sizeBytes, filename, container, sha256}`, `hires: {tier, open, blockedBy}`).
- `panel.hires(store, asset_id: str, purpose: str, path_map: str | None) -> dict` (`filename, path, mappedPath, sha256, sizeBytes, rate, start, durationFrames`), raising `PanelError(status, code, reason)`; order of refusals: 404 `not_found`, 409 `archived`, 409 `essence_open`, 409 `not_local`, then for `purpose == "publish"` 409 `not_cleared`, 409 `legal_hold`. Default purpose `publish`; any other value than `edit` or `publish` is 422.
- `panel.rights(store, ids: list[str]) -> list[dict]` (`assetId, blockedBy, canPublish, tier`), at most 200 ids, 422 over.
- Reuses `operator_state._blocked_by`, `nle.parse_path_map`, `nle.map_path`; adds no new words.

- [ ] **Step 1: Write the failing tests.** `Library.test_lists_newest_first_and_hides_deleted_assets`. `Library.test_q_matches_transcript_words_labels_show_and_camera`. `Library.test_blocked_by_uses_the_operator_words` (`legal hold`, `not cleared`, `archived`, `None`). `Library.test_limit_cap_cursor_round_trip_and_bad_cursor_422`. `Library.test_list_has_no_media_urls_or_paths`. `Detail.test_unknown_and_deleted_asset_404`. `Detail.test_marker_spans_included_only_here`. `Hires.test_edit_purpose_allows_uncleared_and_held`. `Hires.test_publish_purpose_refuses_uncleared_then_held` (assert order and codes). `Hires.test_default_purpose_is_publish`. `Hires.test_archived_restoring_open_and_s3_location_are_refused_in_order`. `Hires.test_mapped_path_uses_the_export_path_map_exactly_like_nle`. `Hires.test_path_is_never_returned_to_a_request_without_a_principal` (401). `Rights.test_201_ids_is_422_and_unknown_id_is_reported_not_raised`. `Http.test_panel_token_can_fetch_proxy_with_range_and_a_ticket_still_works` (reuse the `test_tickets.py` setup).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_panel_library -v`. Expected: FAIL.
- [ ] **Step 3: Implement** reads only (no new POST). Add the paths to `PANEL_ALLOW`.
- [ ] **Step 4: Run** the module and the full suite, including `tests.test_panel_auth` (the allowlist test now sees the new routes). Expected: PASS.
- [ ] **Step 5: Record in the PR** the `media._asset_row` finding (no `deleted_at` filter on the shared proxy route) and wait for the owner's answer to open question 4 before touching it. **Commit** `Panel read API: library, detail, rights, hi-res manifest`.

---

### Task 6: Markers as spans

**Files:**
- Modify: `control/volt/shots.py` (`ORIGINS` gains `panel`; a `create_marker` and `update_marker` path; markers outside the overlap rule), `control/volt/media.py` (`ALL_KINDS` gains `marker`, `DEFAULT_KINDS` unchanged), `control/volt/operator_state.py` (confirm `PUBLISH_KINDS` excludes it), `control/volt/serve.py` (`act_panel_markers`, route, `ROUTE_ROLES`, `PANEL_ALLOW`), `docs/specs/operator-api.md`, `docs/specs/shot-spans.md`
- Create: `control/tests/test_panel_markers.py`

**Interfaces:**
- `shots.upsert_marker(store, asset_id: str, *, client_ref: str, span_id: str | None, tc_in: str | None, tc_out: str | None, frame_in: int | None, frame_out: int | None, label: str | None, text: str | None) -> tuple[dict, str]` returning (span, `"created"|"updated"|"unchanged"`); `ShotError` codes reused plus `read_only` for a `clip` or `shot` span.
- `POST /api/assets/{asset}/markers` body `{markers: [{clientRef, spanId?, frameIn, frameOut?, name, comment?, color?}], dryRun?}` (at most 200; `frameOut` omitted means a one-frame point marker stored with `text = "point"`). Response `{ok, results: [{clientRef, spanId, result, code?}]}`. Frames, not timecode strings, so the panel never formats drop-frame labels; the server derives `tc_in` and `tc_out` from the essence timebase (`shots.facts`).
- `act_panel_markers`: `ROUTE_ROLES` `operator`, audited action `markers`, kind `asset`.

- [ ] **Step 1: Write the failing tests.** `Upsert.test_same_batch_twice_creates_nothing_the_second_time` (same `clientRef`, same `spanId` returned). `Upsert.test_lost_response_retry_with_no_span_id_still_dedupes_on_client_ref`. `Upsert.test_update_changes_label_and_times_of_a_marker_only`. `Upsert.test_clip_and_shot_spans_answer_read_only_and_are_unchanged`. `Upsert.test_point_marker_is_one_frame_and_flagged`. `Upsert.test_outside_source_and_label_over_200_are_per_marker_errors_and_the_rest_of_the_batch_lands`. `Upsert.test_marker_ignores_the_overlap_rule_with_shots`. `Upsert.test_rights_do_not_block_marking` (uncleared, held). `Kinds.test_default_spans_listing_hides_markers_and_kind_marker_shows_them`. `Kinds.test_marker_is_not_publishable_or_attachable_to_a_rundown_item`. `Kinds.test_transcript_rerun_and_detect_rerun_leave_markers_alone`. `Http.test_no_endpoint_deletes_a_span_for_a_panel_token` (extends the allowlist walk). `Http.test_audit_row_counts_created_updated`. A Postgres twin of the idempotence test (`PgCase`).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_panel_markers -v`. Expected: FAIL.
- [ ] **Step 3: Read** `shots.create`, `trim`, `delete` and the in-use checks first, and write down in the PR what had to change. Implement; `client_ref` uniqueness decides `created` versus `unchanged`; a unique-violation race resolves to the existing row.
- [ ] **Step 4: Run** the module, `tests.test_shots`, `tests.test_shot_edit_api`, `tests.test_media`, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Markers: a span kind and an idempotent write for the panel`.

---

### Task 7: Approval page

**Files:**
- Create: `control/volt/session/operator/panel-approve.html`, `control/volt/session/operator/panel-approve.js`
- Modify: `control/volt/serve.py` (`STATIC` entries), `control/volt/session/operator/operator.css` and `theme.js` token sets only if a new colour state is needed, `control/tests/test_operator_ui.py` (new class `PanelApprove`)

**Interfaces:** page `/panel/approve?code=XXXXXXXX` reads the code, calls `GET /api/whoami`, shows label, requesting address, user code, the sentence "Approve only if you started this on your own computer", buttons Approve and Deny (two-press for Approve, like other writes), result as a `stateChip`, and for an admin a "Panels" list from `GET /api/panel/tokens` with Revoke. The label is set with `textContent` only.

- [ ] **Step 1: Write the failing tests.** `PanelApprove.test_static_route_serves_with_the_same_csp`. `PanelApprove.test_no_inline_script_style_or_external_url`. `PanelApprove.test_label_is_never_inserted_as_html` (node: feed `<img src=x onerror=1>`, assert text node). `PanelApprove.test_approve_needs_two_presses`. `PanelApprove.test_states_use_icon_word_and_colour` (sprite ids exist). Contrast test covers any new token in both themes.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_operator_ui.PanelApprove -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the page against the existing helpers; no new colours unless unavoidable.
- [ ] **Step 4: Run** the whole file, then manual check: serve the harness, open the page in a browser, run axe-core 4.10.2 at 1280 and 768 px in both themes, and screenshot. Record "0 violations" or the list. Expected: PASS and clean scan.
- [ ] **Step 5: Commit** `Panel approval page`.

---

### Task 8: Panel core and the fake Premiere

**Files:**
- Create: `panel/premiere/core/{api,auth,library,media,markers,outbox,rights,frames}.js`, `panel/premiere/adapters/fake_ppro.js`, `panel/premiere/adapters/CONTRACT.md` (the adapter interface, one paragraph per method), `panel/premiere/tests/*.test.js`, `panel/premiere/package.json` (no dependencies; `"test": "node --test tests"`), `control/tests/test_panel_js.py` (runs `node --test` when node exists)

**Interfaces:**
- `api.createClient({fetch, baseUrl, getToken, onUnauthorized, now, timeouts}) -> {get, post, download(url, dest, {onProgress, signal})}`; HTTPS required unless `127.0.0.1`/`localhost` (throws `insecure_server`).
- `auth.createAuth({client, store, openUrl, sleep}) -> {start(label), poll(), signOut(), state}`; `state` in `signed_out | waiting(userCode, verifyUrl) | signed_in | revoked`.
- `frames.toTicks(frames, rate, tickRate)` and `frames.fromTicks(ticks, rate, tickRate)` with BigInt rational math; `rate` as `"30000/1001"`; a `tickRate` parameter because the spike (Task 3) sets it.
- `markers.fromSpan(span, comment) -> {name, comment, startFrames, durationFrames, color}` embedding `volt:span:<id>`; `markers.toBatch(adapterMarkers) -> [{clientRef, spanId?, frameIn, frameOut?, name, comment?}]`, writing `volt:ref:<id>` into the Premiere marker before sending.
- `outbox.createOutbox({storage, client, sleep, now}) -> {enqueue(batch), flush(), pending()}`; backoff 1 s doubling to 300 s; 4xx moves an entry to `needsAttention`.
- `media.createMediaCache({fs, client, capBytes}) -> {fetchProxy(asset) -> localPath, evict(inUse)}`; file name `volt-<assetId>-proxy.<ext>`; `.part` then rename.
- `rights.verdict(item) -> {word, canEdit, canPublish}` using the server's `blockedBy` words only.
- Adapter contract (the thing `ppro.js` must satisfy): `activeProject()`, `importFiles(paths, binName)`, `findItemsByPathPrefix(prefix)`, `setItemInOut(item, inFrames, outFrames)`, `insertOnSequence(item, atFrames, track)`, `overwriteOnSequence(item, atFrames, track)`, `readItemMarkers(item)`, `writeItemMarker(item, marker)`, `readSequenceMarkers(seq)`, `writeSequenceMarker(seq, marker)`, `changeMediaPath(item, path)`, `sequenceClips(seq)`, `frameRateOf(item)`. Frames are integers at the item's rational rate.

- [ ] **Step 1: Write the failing tests.** `frames.test_29_97_drop_frame_minute_boundaries_round_trip_both_ways` (frames 1799, 1800, 17981, 17982 with expected seconds as rationals written in the test). `frames.test_25_and_59_94_round_trip`. `auth.test_waiting_then_signed_in_stores_token_in_secure_storage_fake`. `auth.test_401_moves_to_revoked_and_keeps_outbox_cache_and_url`. `auth.test_http_server_url_refused_except_loopback`. `api.test_timeout_aborts_and_reports_offline`. `media.test_corrupt_chunk_fails_size_or_hash_check_and_part_file_is_never_imported`. `media.test_resume_uses_range_from_the_part_file_length`. `media.test_eviction_never_deletes_a_path_the_project_references`. `outbox.test_entry_survives_a_restart_and_resends_with_the_same_client_ref`. `outbox.test_5xx_retries_with_backoff_and_4xx_needs_attention`. `outbox.test_offline_keeps_entries_signed_out_keeps_entries`. `markers.test_round_trip_span_to_marker_to_batch_preserves_ids`. `markers.test_user_renames_marker_name_link_survives_via_comment`. `markers.test_marker_removed_in_premiere_is_reported_and_nothing_deletes_the_span`. `rights.test_relink_disabled_for_not_cleared_held_and_archived_and_enabled_for_ready`. `library.test_offline_shows_cached_page_with_age_and_disables_server_actions`. `library.test_newer_search_cancels_the_older_one`. `fake.test_fake_obeys_the_adapter_contract` (a shared contract suite that Task 9 also runs against the real adapter in Premiere). `test_panel_js.py::NodeSuite.test_panel_core_passes` (`skipUnless(NODE)`).
- [ ] **Step 2: Run** `cd panel/premiere && node --test tests`. Expected: FAIL.
- [ ] **Step 3: Implement** core modules and the fake. Nothing in `core/` may `require` an Adobe module, touch the DOM or read global `fetch`; a test greps for it.
- [ ] **Step 4: Run** `node --test tests` and `cd control && python -m unittest tests.test_panel_js -v`. Expected: PASS.
- [ ] **Step 5: Commit** `Premiere panel core and fake adapter`.

---

### Task 9: Premiere adapter, UI, package and the live checklist (needs a real Premiere)

**Files:**
- Create: `panel/premiere/manifest.json`, `panel/premiere/index.html`, `panel/premiere/ui/*.js`, `panel/premiere/adapters/ppro.js`, `panel/premiere/build.js` (reproducible `.ccx`), `docs/premiere-panel.md` (install, sign in, limits, unvalidated list)
- Modify: `docs/specs/editor-integration.md` (findings), `docs/roadmap.md`, `docs/areas.md` if it lists the NLE panel

**Interfaces:** `ppro.js` implements the Task 8 contract using only the call names recorded in Task 3; `ui` renders state from `core` and returns DOM operations. `build.js` produces `volt-premiere-<version>.ccx` from a fixed file list and prints its sha256; the same source gives the same hash.

- [ ] **Step 1: Failing tests first, where possible:** `ui.test_buttons_disabled_for_each_rights_word`, `ui.test_offline_banner_and_read_only_state`, `ui.test_search_renders_no_html_from_server_text`, `build.test_package_is_reproducible_and_lists_only_the_fixed_files`, and the shared adapter contract suite run against `ppro.js` with a stubbed Adobe module (proves call shapes, not behaviour). Run `cd panel/premiere && node --test tests`. Expected: FAIL.
- [ ] **Step 2: Implement** the adapter and UI. Keep every Adobe name inside `ppro.js`.
- [ ] **Step 3: Run** `node --test tests` and the full Python suite. Expected: PASS.
- [ ] **Step 4: Live checklist in real Premiere** (UDT load, Premiere version and OS recorded; each item pass or fail with a screenshot): (1) first run: server URL, device code, browser approval, panel signed in; revoke on the web page and see `Token revoked` within one poll; (2) search "steal" finds the asset and the hit; thumbs show; rights chips match the Volt page for a cleared, an uncleared and a held asset; (3) Add proxy to project: bin created, file imported, name and rate right; (4) Insert and Overwrite a span's in/out at the playhead on V1/A1: the clip's source in/out equal the span's `tcIn`/`tcOut` to the frame, at 25 and 29.97 drop-frame, including the minute-crossing and one-frame spans; (5) write spans as markers: the marker positions equal the span times; (6) add a marker in Premiere, Read markers: one new `marker` span appears in Volt's spans listing (`kind=marker`), pressing it twice creates nothing; kill the network mid-push, restart Premiere, outbox resends once; (7) delete a marker in Premiere: Volt span stays, the panel says so; (8) pull the cable mid-download: resumes; cache cap evicts and tells; (9) Check sequence: blocked assets named; Relink enabled only for publishable ones; relink a cleared clip to hi-res through the path map, the clip stays at the same in/out, markers remain; an archived or open one is refused by the server with its word; (10) a Volt server on https with an internal certificate; plain http to a LAN host refused by the panel; (11) install the `.ccx` by double click on a second machine; note whether an Adobe sign-in, fee or certificate was involved; (12) 1000-result library page: note memory and scroll speed.
- [ ] **Step 5: Write** `docs/premiere-panel.md` with every item above marked verified (version, date) or unvalidated. **Commit** `Premiere panel: adapter, UI, package, live results`.

Not validated until Step 4 is done by a person: everything about Adobe's behaviour, the package install path, and the panel's speed and feel.
