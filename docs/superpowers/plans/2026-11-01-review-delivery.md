# Volt review, approval and delivery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Time-coded review with approvals, no-login share links for outside reviewers, captions, automated QC, validated presets, and idempotent delivery to S3 and SFTP, all gated by the rights check, with nothing public posted without a second confirming call.

**Architecture:** New modules beside the existing ones in `control/volt/` (`review.py`, `shares.py`, `captions.py`, `qc.py`, `presets.py`, `delivery.py`), runner job types registered in `runner.default_handlers`, routes in `serve.py`, static pages in `control/volt/session/operator/` and a new `control/volt/session/review/`. Each task that adds tables adds one migration (the next free number at build time; append to `migrations.MIGRATIONS` and to `schema.sql`).

**Tech Stack:** Python standard library, `unittest` in `control/tests/`, ffmpeg and ffprobe on PATH (tests that need them skip when absent, as `test_publish.py` does), optional paramiko for SFTP (lazy import), node for the UI tests as in `test_operator_ui.py`.

**Spec:** `docs/superpowers/specs/2026-11-01-review-delivery-design.md`. Read it first; this plan does not repeat its reasoning.

## Global Constraints

- Tests use fakes and never the network. Postgres tests run only with `VOLT_TEST_PG_DSN` and skip otherwise; every migration gets a historical-shape forward test on SQLite and Postgres (`tests/test_migrations.py`, `historical_shapes.py`).
- Every new staff POST is classified in `serve.ROUTE_ROLES` and audited through `Store.append_audit`. Reviewer POSTs are in a separate table with its own classification test and are not in `ROUTES` (so `tests/test_users.py` keeps its meaning).
- The rights gate (`Store.assert_publishable`, or its rights half for shares) runs on every review render, share (create and each request), caption export, QC, delivery create, confirm and again immediately before the transfer.
- UI: static files, same-origin CSP, no `http(s)://`, no inline style or script, state as icon plus word plus colour, tokens added to both theme sets and the contrast test, axe clean in both themes. Follow `docs/brand-guide.md` and `docs/specs/operator-ui.md`.
- No credential in the database, the audit log or a log line. Credentials come from the environment.
- Anything that needs a real account or server (SFTP, YouTube, Vimeo, a CDN) is worded "unvalidated" in code comments, docs and UI.
- Existing behaviour stays: `publish.KINDS`, `deliverables.PROFILES` arguments, `POST /api/spans/{span}/publish`, the ticket scheme for assets, `assert_publishable` for existing callers.
- Own branch and PR per task; the full suite and CI (both databases) green before the next task starts.

## Review Focus

1. **Exposure through a share.** Any response, header, error or timing that lets a reviewer reach another span, asset, filename, path, staff name, rights state or a bearer route; a token in a path, query, log line or `Referer`.
2. **Rights drift.** A share or delivery that proceeds after a hold, a revocation of "cleared", a delete or an archive that happened after the link or request was made; a gate checked only at creation.
3. **Double effects.** A retry, a crash between upload and recording, two runners or two confirm clicks that deliver twice, post twice, render twice, or leave a half object under its final name.
4. **False green.** QC `pass` for a check that did not run, an approval that survives a changed cut, a "validated" claim backed only by a fake, a caption rule that silently drops a cue.
5. **Unbounded input.** A preset or profile field reaching ffmpeg unvalidated, a comment or name without a size cap, a reviewer route with no rate or count bound, a path outside `VOLT_REVIEW_DIR`.

---

### Task 1: Review cuts

**Files:**
- Create: `control/volt/review.py`, `control/tests/test_review_cut.py`
- Modify: `control/volt/migrations.py`, `control/volt/schema.sql`, `control/volt/store.py` (`SCHEMA` snapshot), `control/volt/runner.py` (handler `review_render`), `control/volt/serve.py` (routes, `ROUTE_ROLES`), `control/volt/shots.py` (a span used by a `review` row counts as in use, as for `version`), `control/volt/retention.py` (purge deletes owned cuts)

**Interfaces:**
- Produces: table `review` (spec section 2). `review.request(store, span_id, actor) -> Review` (idempotent per revision; new revision only when `tc_in`/`tc_out` changed or `new=True`); `review.render(store, review_id, out_dir, runner=subprocess.run) -> Path` (cut from the hi-res with the same frame arithmetic as `deliverables.render`, writes `*.part.mp4` then renames); `review.review_dir() -> Path` (`VOLT_REVIEW_DIR`, else `<VOLT_OUT>/review`); `review.cut_path(review) -> Path` (resolved, must be inside `review_dir()` else `ReviewError("outside")`); `ReviewError(code, reason)`. Runner type `review_render`, key `review:<span>:<revision>`. Routes `POST /api/spans/{span}/review`, `GET /api/spans/{span}/reviews`, `GET /api/reviews/{id}`, `GET /api/reviews/{id}/media`.
- Consumes: `deliverables` timecode helpers, `Store.assert_publishable`, `Store.enqueue`.

- [ ] **Step 1: Write the failing tests** in `test_review_cut.py`: `test_request_is_idempotent_per_revision` (twice gives one row, `revision == 1`); `test_trim_requires_new_revision` (change `span.tc_out`, request again, `revision == 2`, old row `superseded`); `test_uncleared_or_held_refused` (`not_cleared`); `test_cut_frame_count_matches_span` (ffmpeg, skip if absent: `ffprobe` frame count equals `span.frame_out - span.frame_in`); `test_render_failure_leaves_no_file` (fake runner returns code 1: no `.part`, status `failed`, `error` set); `test_cut_path_must_be_inside_review_dir` (a row whose `render_path` is `../x` raises); `test_span_with_review_cannot_be_deleted_or_trimmed`; `test_purge_deletes_owned_cut_and_keeps_comment_history_marker`; `test_routes_classified` (`ROUTE_ROLES["act_review_request"] == "operator"`); `test_audit_row_written`.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_review_cut -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration (also create the table in `schema.sql` for Postgres), `review.py`, the handler (`Defer` while the hi-res is archived, via `needs_hi_res`), the routes and the span-in-use rules.
- [ ] **Step 4: Run** the new file, `tests.test_migrations`, `tests.test_users`, then the whole suite `python -m unittest discover -s tests`. Expected: PASS (Postgres cases skip without a DSN).
- [ ] **Step 5: Commit** `Review cuts: table, render job, routes`.

### Task 2: Comments and approvals (staff)

**Files:**
- Create: `control/tests/test_review_comments.py`
- Modify: `review.py`, `migrations.py`, `schema.sql`, `store.py`, `serve.py`, `docs/specs/operator-api.md`

**Interfaces:**
- Produces: tables `review_comment`, `approval`. `review.add_comment(store, review_id, *, body, frame=None, frame_out=None, parent=None, internal=False, author=Author) -> dict` where `Author(kind: "user"|"guest", name: str, share_id: str|None)`; `review.edit_comment(store, comment_id, author, body)`; `review.delete_comment(store, comment_id, author, is_admin)`; `review.set_resolved(store, comment_id, resolved: bool, actor)`; `review.request_approval(store, review_id, actor, assignees, note)`; `review.decide(store, review_id, author, state, note, approvers: list[str])`; `review.current_approval(store, review_id) -> dict` with `{state, valid, actor, at, stale_reason}`; `review.timecode_of(review, frame) -> str` (through `volt/timecode.py`, never stored). Routes and error codes from spec section 3 (`review_not_ready`, `thread_depth`, `is_reply`, `not_author`, `bad_transition`, `no_request`, `stale_review`, `not_approver`).

- [ ] **Step 1: Write the failing tests:** `test_comment_frame_bounds` (frame outside `0..frames-1` is 422); `test_reply_depth_is_one` (reply to a reply is `thread_depth`); `test_resolve_only_top_level_and_replies_follow`; `test_delete_blanks_body_keeps_row_and_replies`; `test_edit_only_by_author_body_only`; `test_internal_flag_roundtrip`; `test_timecode_matches_drop_frame_label` (29.97 DF, comment at frame 1800 reads the right `;` label); `test_approval_state_machine` (table of allowed and refused transitions, each refusal is `bad_transition`); `test_decision_requires_request` (`no_request`); `test_approval_stale_after_trim` (valid becomes false, `stale_reason == "span_changed"`); `test_decision_on_old_revision_refused`; `test_approvers_list_enforced` (`VOLT_APPROVERS=alice`, bob gets `not_approver`); `test_all_posts_in_route_roles_and_audited`; `test_postgres_forward` (skipped without DSN).
- [ ] **Step 2: Run** `python -m unittest tests.test_review_comments -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** One transaction per write; comment text stored as text; length 1 to 2000.
- [ ] **Step 4: Run** the file, `tests.test_migrations`, `tests.test_users`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Review comments and approvals`.

### Task 3: Share links and the reviewer API

**Files:**
- Create: `control/volt/shares.py`, `control/tests/test_shares.py`
- Modify: `migrations.py`, `schema.sql`, `store.py`, `tickets.py` (add share tickets), `serve.py` (reviewer table, `--review-only`, `STATIC` entries, headers), `docs/tls.md`, `docs/specs/operator-api.md`

**Interfaces:**
- Produces: table `share`. `shares.create(store, review_id, *, label, capability, expires_in, password, max_comments, actor, now) -> (Share, token)` (token shown once; stored as sha256; expiry over `VOLT_SHARE_MAX_DAYS` is `bad_input`); `shares.resolve(store, token, now) -> Share` raising `ShareError("not_found"|"link_gone"|"password_required"|"unavailable"|"locked")`; `shares.unlock(store, share, password, now) -> (unlock_token, expires)`; `shares.revoke(store, share_id, actor)`; `shares.check_unlock(secret, share, unlock_token, now) -> bool`. `tickets.issue_share(secret, share_id, kind, ttl, now) -> (token, exp)` and `tickets.verify_share(secret, share_id, kind, token, now)` use a distinct message prefix (`share|<id>|<kind>|<exp>`) so an asset ticket never verifies as a share ticket and the reverse. `serve.REVIEW_ROUTES` with handlers `act_review_unlock`, `act_review_comment`, `act_review_decision` and `REVIEW_ROUTES_CAPS = {"act_review_unlock": "view", "act_review_comment": "comment", "act_review_decision": "decide"}`; GETs `/api/review/view`, `/api/review/media`; POSTs `/api/review/ticket`. Header `X-Volt-Share` carries the token. Staff routes `POST /api/reviews/{id}/shares`, `POST /api/shares/{id}/revoke`, `GET /api/reviews/{id}/shares`, `GET /api/shares` (admin). Flag `volt serve --review-only` / `VOLT_REVIEW_ONLY=1`.

- [ ] **Step 1: Write the failing tests:** `test_token_never_stored_in_clear` (scan every column of the `share` and `audit` tables); `test_unknown_malformed_and_revoked_are_indistinguishable_by_shape` (unknown 404, revoked 410 `link_gone`, expired 410, bodies carry no id); `test_expiry_uses_frozen_clock`; `test_expiry_cannot_exceed_max`; `test_revoke_takes_effect_on_next_request_including_media_range`; `test_view_response_has_no_asset_span_path_filename_or_staff_name` (walk the JSON for forbidden keys and values, and assert staff authors read `Production`); `test_internal_comments_hidden_from_reviewer`; `test_capability_matrix` (view cannot comment, comment cannot decide); `test_password_lockout_persists_across_restart` (5 failures, `locked_until` stored, 429 `locked`); `test_comment_rate_and_cap` (11th post in a minute is 429; `max_comments` is 409 and holds with a fresh limiter); `test_hold_after_share_creation_blocks_next_request` (403 `unavailable`, no reason text); `test_archived_asset_does_not_block_existing_cut_review`; `test_media_ticket_bound_to_share` (a ticket from share A fails on share B; an asset ticket fails here); `test_media_path_outside_review_dir_refused`; `test_review_only_mode_404s_every_other_route` (including `/api/state`, `/health` detail, every staff route); `test_reviewer_routes_all_classified` (fails if a handler is missing from `REVIEW_ROUTES_CAPS`); `test_reviewer_writes_audited_as_guest_actor`; `test_no_media_key_refuses_share` (503); `test_response_headers` (`Referrer-Policy: no-referrer`, `Cache-Control: no-store`, CSP unchanged).
- [ ] **Step 2: Run** `python -m unittest tests.test_shares -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Password hashing with `hashlib.scrypt`; compare with `hmac.compare_digest`; limiter keyed by share id; do not read `X-Forwarded-For`.
- [ ] **Step 4: Run** the file, `tests.test_tickets`, `tests.test_users`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Share links and the reviewer API`.

### Task 4: Review UI (reviewer page and operator tab)

**Files:**
- Create: `control/volt/session/review/index.html`, `review.js`, `review.css`, `control/tests/test_review_ui.py`
- Modify: `operator/index.html`, `operator.js`, `operator.css`, `serve.py` (`STATIC`), `docs/specs/operator-ui.md`, `docs/brand-guide.md` (light/dark tokens only if new), `control/tests/operator_harness.py` (scenario `review`)

**Interfaces:**
- Produces: `STATE` entries `approval_none`, `approval_requested`, `approval_approved`, `approval_changes`, `approval_stale`, `comment_open`, `comment_resolved`, `share_live`, `share_expired`, `share_revoked`, each with icon, word and class. Operator: a Review tab (`#lib-review`) with player, thread list, comment box, approval chip, Share disclosure, share list with two-press Revoke; keys `C` and `R`, active only on that tab and never in a field. Reviewer page: `reviewer.js` exports pure functions `parseToken(hash)`, `commentTick(frame, frames)`, `renderThread(comment)` (uses `textContent` only) for node tests.

- [ ] **Step 1: Write the failing tests** (`unittest` with `_node` as in `test_operator_ui.py`): `test_parse_token_from_fragment_only`; `test_no_inline_style_script_or_external_url_in_review_files`; `test_comment_body_never_inserted_as_html` (a body of `<img onerror=...>` renders as text); `test_every_icon_exists_in_sprite`; `test_review_state_specs_have_icon_word_class`; `test_reviewer_page_contains_no_asset_or_staff_strings`; `test_keys_inert_in_fields_and_off_review_tab`; `test_share_link_shown_once` (the copy control disappears after the first render); `test_contrast_tokens_in_both_themes`.
- [ ] **Step 2: Run** `python -m unittest tests.test_review_ui tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement** both pages. Reviewer page: single column, 360 px safe, no staff names, name remembered in `localStorage` inside try/catch.
- [ ] **Step 4: Browser check** with the harness `review` scenario: axe-core 4.10.2 in both themes on the reviewer page at 360 px and the operator Review tab; record results in `operator-ui.md`. Run the full suite. Expected: PASS and no violations.
- [ ] **Step 5: Commit** `Review UI: reviewer page and operator tab`.

### Task 5: Captions: model, cue builder, WebVTT and SRT

**Files:**
- Create: `control/volt/captions.py`, `control/tests/test_captions.py`, `control/tests/golden/captions/`
- Modify: `migrations.py`, `schema.sql`, `store.py`, `serve.py`, `operator.js` (Captions tab), `docs/specs/operator-api.md`

**Interfaces:**
- Produces: tables `caption_track`, `caption_cue`. `captions.Rules` (dataclass: `chars_per_line=42, lines=2, cps=17.0, min_ms=833, max_ms=7000, gap_frames=2`), `captions.build(store, asset_id, span_id, lang, rules) -> track_id`, `captions.violations(cues, rules, rate) -> list[Violation(idx, rule, severity)]`, `captions.edit(store, track_id, edits) -> list[Violation]`, `captions.split(store, track_id, idx, at_frame)`, `captions.merge(store, track_id, idx)`, `captions.to_vtt(track, cues, rate, origin) -> str`, `captions.to_srt(...) -> str`. Times are computed from integer frames and exact rates (`fractions.Fraction`), never floats. Routes `POST /api/assets/{asset}/captions`, `POST /api/captions/{track}/cues`, `GET /api/captions/{track}/export?format=vtt|srt`.

- [ ] **Step 1: Write the failing tests:** `test_cues_group_words_by_sentence_then_length`; `test_line_break_balances_two_lines_under_limit`; `test_reading_speed_warn_and_fail_thresholds`; `test_min_duration_extends_into_silence_not_next_cue`; `test_gap_rule_two_frames`; `test_times_exact_at_29_97_and_50_fps` (known frame to `HH:MM:SS.mmm` table); `test_origin_zero_at_span_in`; `test_vtt_golden_file` and `test_srt_golden_file`; `test_vtt_escapes_amp_lt_gt`; `test_empty_transcript_is_no_transcript_409`; `test_not_cleared_export_refused`; `test_rebuild_makes_new_track_never_overwrites_edits`; `test_edit_returns_violations_not_exception`; `test_no_cue_dropped_silently` (every input word appears in some cue); `test_vtt_parses_in_browser` (headless browser check from the harness, skipped without one).
- [ ] **Step 2: Run** `python -m unittest tests.test_captions -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Read the WebVTT text at https://www.w3.org/TR/webvtt1/ first and record the revision in `docs/specs/captions.md` (new spec file, with what is proven and what is not).
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Captions: model, builder, WebVTT and SRT`.

### Task 6: Captions: TTML, EBU-TT-D and burn-in

**Files:**
- Modify: `captions.py`, `presets.py` (burn-in field, after Task 8; until then a guarded stub), `deliverables.py` (subtitles filter argument, behind the preset), `preflight.py`, `control/tests/test_captions.py`, `docs/specs/captions.md`

**Interfaces:**
- Produces: `captions.to_ttml(track, cues, rate, origin) -> str` (IMSC1 text profile, media-time clock values), `captions.to_ebu_tt_d(...) -> str` (raises `CaptionError("has_violations", ...)` when any `fail` remains), `deliverables.burnin_filter(srt_path, font) -> str` (escapes the path for the filter graph), preflight check `ffmpeg-subtitles-filter`.

- [ ] **Step 1: Write the failing tests:** `test_ttml_is_well_formed_and_escaped` (hostile text with `& < > "`); `test_ttml_times_match_vtt_times`; `test_ebu_tt_d_required_namespaces_and_timebase`; `test_ebu_tt_d_refuses_with_violations`; `test_ebu_tt_d_validates_against_schema` (runs only if the schema was vendored after a licence check; otherwise the test asserts the docs say "unvalidated"); `test_burnin_filter_escapes_path_and_rejects_unlisted_font`; `test_burnin_refused_without_libass` (fake preflight result); `test_burnin_never_changes_caption_track`.
- [ ] **Step 2: Run** `python -m unittest tests.test_captions -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Read EBU Tech 3380 first; record the version in `captions.md`.
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Captions: TTML, EBU-TT-D, burn-in`.

### Task 7: Automated QC

**Files:**
- Create: `control/volt/qc.py`, `control/tests/test_qc.py`, `control/tests/golden/qc/` (recorded real ffmpeg stderr files)
- Modify: `migrations.py`, `schema.sql`, `store.py`, `runner.py` (type `qc`), `serve.py`, `operator.js` (QC panel on Publish), `docs/specs/qc.md` (new)

**Interfaces:**
- Produces: tables `qc_run`, `qc_finding`. `qc.Profile` (dataclass of every threshold), `qc.load_profiles(path=None) -> dict[str, Profile]` (built-ins `r128`, `a85`; `VOLT_QC_PROFILES` adds or overrides; unknown keys and out-of-range values raise `QcConfigError`), parsers `parse_ebur128(text) -> Loudness`, `parse_blackdetect(text) -> list[Range]`, `parse_silencedetect(text)`, `parse_freezedetect(text)`, `qc.evaluate(measurements, profile, duration) -> list[Finding]`, `qc.run(store, span_id, preset, profile_name, runner=subprocess.run) -> run_id`, `qc.key(span_id, preset, profile) -> str` including a hash of the thresholds. Routes `POST /api/spans/{span}/qc`, `GET /api/spans/{span}/qc`.

- [ ] **Step 1: Write the failing tests:** one per parser against recorded stderr (`test_parse_ebur128_golden`, `..._blackdetect_`, `..._silencedetect_`, `..._freezedetect_`); `test_loudness_pass_inside_tolerance_and_fail_outside` for both profiles with the spec's numbers; `test_true_peak_over_limit_fails`; `test_black_at_head_and_tail_ignored_within_edge_seconds`; `test_unparseable_output_is_not_run_and_verdict_incomplete`; `test_ffmpeg_nonzero_is_not_run_never_pass`; `test_no_audio_stream_loudness_not_run`; `test_same_config_same_key_is_noop_changed_threshold_new_run`; `test_run_marked_stale_after_trim`; `test_profile_file_validation_rejects_unknown_key`; `test_require_qc_flag_blocks_delivery_missing_and_failed` (wired in Task 9, asserted there); `test_rights_gate_on_qc`; `test_real_ffmpeg_sine_at_known_level` (generated `sine` plus `lavfi`, skip without ffmpeg: integrated loudness within 1 LU of a calculated value).
- [ ] **Step 2: Run** `python -m unittest tests.test_qc -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Confirm each filter's option names against the installed ffmpeg and record the ffmpeg version in `qc.md`; read EBU R 128 and ATSC A/85 before naming the built-in profiles.
- [ ] **Step 4: Run** the file, `tests.test_runner`, the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `QC runs: loudness, black, silence, freeze`.

### Task 8: Presets

**Files:**
- Create: `control/volt/presets.py`, `control/tests/test_presets.py`
- Modify: `migrations.py`, `schema.sql`, `store.py`, `deliverables.py` (build arguments from a validated spec; `PROFILES` kept for the built-ins), `publish.py` (`kinds` argument), `serve.py`, `operator.js` (Presets list and admin form), `docs/specs/publish.md`

**Interfaces:**
- Produces: table `preset` seeded with `house`, `ott`, `social` as `builtin`. `presets.validate(spec: dict) -> list[str]`, `presets.to_ffmpeg(spec) -> tuple[list[str], str | None, str]` (video args, `-vf`, audio rate; same shape as `PROFILES` values), `presets.get(store, name) -> dict`, `presets.create(store, name, spec, actor)`. `deliverables.render(store, span, kind, out_dir)` looks up `kind` as a preset name and still accepts the three built-ins unchanged. `publish.publish(store, span_id, kinds=KINDS, ...)` and `publish_files(..., kinds=KINDS)`. Routes `POST /api/presets`, `POST /api/presets/{name}/disable`, `GET /api/presets`.

- [ ] **Step 1: Write the failing tests:** `test_builtins_byte_identical_to_profiles` (for each of the three, `to_ffmpeg(spec) == PROFILES[name]`); `test_unknown_key_rejected`; `test_crf_and_bitrate_exclusive`; `test_ranges_enforced` (parameterised: crf 52, height 10, audio channels 3); `test_prores_requires_mov`; `test_injection_strings_rejected` (a value `"; -f lavfi"`, a codec `"libx264 -vf x"`); `test_name_pattern_and_collision_with_builtin`; `test_builtin_cannot_be_disabled_409`; `test_custom_preset_publishes_as_version_kind` (fake renderer: a `version` row with `kind == "proxy-hd"`); `test_default_publish_still_makes_exactly_three`; `test_admin_only_routes`; `test_font_must_be_in_allow_list` (burn-in, with Task 6).
- [ ] **Step 2: Run** `python -m unittest tests.test_presets tests.test_publish tests.test_deliverables -v`. Expected: FAIL (new tests only).
- [ ] **Step 3: Implement.** The three built-in commands must not change by a single argument.
- [ ] **Step 4: Run** the three files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Presets: validated, beyond house, ott and social`.

### Task 9: Delivery core and the S3 prefix adapter

**Files:**
- Create: `control/volt/delivery.py`, `control/tests/test_delivery.py`
- Modify: `migrations.py`, `schema.sql`, `store.py`, `runner.py` (type `deliver`), `serve.py`, `operator.js` (Deliveries panel and the confirm page), `docs/specs/delivery.md` (new), `docs/specs/storage.md` (pointer)

**Interfaces:**
- Produces: tables `delivery_target`, `delivery`. `delivery.Target` protocol (`check`, `push`, `exists`), `delivery.S3Target(config, backend)` over `storage.S3Backend` with its own prefix, `delivery.FakeTarget` (in `tests/`, records calls, injectable failures), `delivery.create(store, span_id, preset, target_name, actor) -> Delivery` (`pending_confirm` for `public` targets, with `confirm_code`), `delivery.confirm(store, delivery_id, code, actor, now)`, `delivery.run(store, delivery_id, target) -> None` (re-checks the rights gate, QC and approval flags, renders or finds the version, `exists` then `push`, records `remote_key`, `sha256`), `delivery.remote_key(asset, span, preset) -> str` (derived from content, never the delivery id), `DeliveryConfig(Permanent)`. Env `VOLT_REQUIRE_APPROVAL`, `VOLT_REQUIRE_QC`, `VOLT_CONFIRM_TTL`.

- [ ] **Step 1: Write the failing tests:** `test_create_same_target_version_twice_returns_same_row`; `test_second_run_after_success_makes_no_transfer` (fake counts `push` calls); `test_crash_after_upload_before_record_resumes_without_reupload` (fake `exists` true with equal size and hash); `test_different_bytes_at_key_are_replaced`; `test_rights_hold_between_create_and_run_refuses_permanent`; `test_hold_between_confirm_and_transfer_refused` (gate runs immediately before `push`); `test_public_target_creates_pending_confirm_and_queues_nothing`; `test_confirm_wrong_code_422_and_expired_code_refused`; `test_confirm_needs_admin_role`; `test_confirm_twice_second_is_not_pending_409`; `test_public_failure_after_possible_post_is_not_auto_retried` (fake raises a timeout after `push` returned; status `failed`, message "may have posted", runner does not re-queue); `test_private_transient_error_retries_with_backoff`; `test_auth_error_is_permanent`; `test_require_approval_gate_counts_staff_decision_only` (guest approval does not satisfy; stale approval does not satisfy); `test_require_qc_blocks_missing_and_failed`; `test_credentials_never_in_db_or_audit`; `test_s3_target_uses_own_prefix_and_existing_verification` (fake client); `test_flags_default_off_change_nothing_for_publish`; `test_postgres_forward`.
- [ ] **Step 2: Run** `python -m unittest tests.test_delivery -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Put "unvalidated against real S3 for delivery prefixes beyond the publish bucket" in `delivery.md`.
- [ ] **Step 4: Run** the file, `tests.test_storage`, `tests.test_runner`, the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Delivery: targets, idempotent runs, S3 prefix, public confirm`.

### Task 10: SFTP adapter and the platform interface

**Files:**
- Modify: `control/volt/delivery.py`, `control/tests/test_delivery.py` (new class `SftpTarget`), `docs/specs/delivery.md`, `docs/operations.md`
- Create: `control/volt/platforms.py`, `control/tests/test_platforms.py`

**Interfaces:**
- Produces: `delivery.SftpTarget(config, client_factory)` (client injected; paramiko imported lazily only when the factory is the default; absent paramiko gives `DeliveryConfig("not_installed")`), `.part` upload, size verification, `posix_rename` when the client offers it else remove then rename, mandatory host-key fingerprint from config (mismatch is `Permanent host_key_mismatch`). `platforms.PlatformClient` protocol (`upload(path, title, privacy) -> remote_ref`, `find(remote_ref) -> RemoteInfo | None`), `platforms.FakePlatform`, and `delivery.PlatformTarget(kind, client)`; the `youtube` and `vimeo` kinds are registered but report "not verified against the live service" and have no real client in this repository.

- [ ] **Step 1: Write the failing tests:** `test_sftp_uploads_to_part_then_renames` (call order recorded on the fake); `test_sftp_final_name_never_visible_before_rename`; `test_sftp_size_mismatch_fails_and_removes_part`; `test_sftp_host_key_mismatch_permanent_and_no_connection_data_sent`; `test_sftp_unknown_host_key_is_refused_not_accepted`; `test_sftp_credentials_from_env_only`; `test_sftp_missing_paramiko_reports_not_installed`; `test_sftp_retry_is_idempotent` (remote file with equal size is skipped); `test_platform_target_requires_confirm` (platform kinds default `public`); `test_platform_retry_asks_find_first` (fake `find` true means no second upload); `test_platform_labels_unvalidated_in_api_and_ui_strings`; `test_no_network_import_at_module_load` (importing `delivery` and `platforms` opens no socket and imports no third-party module).
- [ ] **Step 2: Run** `python -m unittest tests.test_delivery tests.test_platforms -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** A real YouTube or Vimeo client is out of scope: write only the protocol, the fake and the documentation of what a builder with an account must verify (resumable upload, quota, processing state, privacy defaults).
- [ ] **Step 4: Run** both files and the full suite on SQLite, then with `VOLT_TEST_PG_DSN` on Postgres. Expected: PASS.
- [ ] **Step 5: Commit** `Delivery: SFTP adapter, platform interface (unvalidated)`.

---

## After the last task

Update `docs/roadmap.md` ("In the tree") and `CHANGELOG.md`; add "What is proven and what is not" to each new spec file (`captions.md`, `qc.md`, `delivery.md`) with the same honesty as `mos.md`; open the live validation list in `docs/product-readiness.md` (a real SFTP server, a real caption ingest, a phone reviewer, a certified loudness meter, a platform account). Do not describe any of them as done.
