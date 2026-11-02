# Volt: search, metadata and AI indexing, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Typed, validated asset metadata; one faceted search across transcript, metadata and AI labels; saved searches and bins; a pluggable, resumable AI indexing job with human review; privacy controls for face data; and a harness that measures all of it at 10,000 assets.

**Architecture:** New modules in `control/volt/`: `metadata.py`, `searchindex.py`, `indexing.py`, `people.py`. Routes in `serve.py`. A derived `search_doc` table with an FTS5 table (SQLite) or a `tsvector` column (Postgres). A runner handler `ai-index` and a `search` rebuild handler. The engine is a Protocol with a fake and a child-process `CommandEngine`; no model library is imported by the control plane. UI in the three static operator files.

**Tech Stack:** Python stdlib, SQLite and Postgres (`VOLT_TEST_PG_DSN`), `unittest` in `control/tests/`, the node-driven `test_operator_ui.py` harness, axe-core as in the UI slices.

**Spec:** `docs/superpowers/specs/2026-11-01-search-indexing-design.md`. Nothing in it is built. Read it first; this plan does not repeat the rules.

## Global Constraints

- Run tests from `control/`: `python -m unittest <module> -v`; full suite `python -m unittest discover -s tests`; Postgres `VOLT_TEST_PG_DSN=<dsn> python -m unittest <module>`. Every task that changes the schema passes `tests.test_migrations` (forward from every shape in `historical_shapes.py`, SQLite and Postgres) and the SCHEMA/`schema.sql` parity test.
- Migration numbers are `migrations.LATEST + 1` at the time of writing the task (head is 21 on 2026-11-01). Other work may take numbers first: never hard-code one, never renumber a shipped entry, no gaps. `_TYPES` gains `REAL` and `BLOB` in Task 1 (Postgres `DOUBLE PRECISION`, `BYTEA`).
- Every new table with an `asset_id` is added to the explicit delete list in `retention.purge_asset` in the same task that creates it, with a test.
- Every new POST is added to `serve.ROUTE_ROLES` in the task that adds it, with an `assertEqual(serve.ROUTE_ROLES[...])` test, and appears in the audit through `_AUDIT_KIND` / `_audit_fields`.
- SQL: `?` placeholders, `TRUE`/`FALSE` not `0`/`1`, timestamps as `YYYY-MM-DDTHH:MM:SSZ` text, per-asset writes under `store.transaction("<name>:<asset>")`, no `INSERT OR` / `ON CONFLICT` forms the pg adapter cannot take (follow `Store.enqueue`).
- No test touches the network, a GPU, ffmpeg-dependent behaviour without `skipUnless`, or a real model. The frame source is an injected callable.
- UI: no `innerHTML` in new code, no `http(s)://`, `style=`, `<style>`, inline script; ids, `data-fk` and existing keys kept; state is icon plus word plus colour; tokens in both themes.
- Anything needing a real model or a cloud account is documented as unvalidated in the same task's doc edit.

## Review Focus

- A face name or label shown, searched, returned or logged as fact when unconfirmed, or surviving an erase (templates, labels, `search_doc` rows, facet counts, audit detail, API bodies).
- A frame, template, name or label sent to a remote engine without the explicit named configuration; faces sent to a remote engine at all; the audit row missing before the first frame.
- The derived index disagreeing with its sources: stale after a transcript replace, a review change, a tombstone or a purge; a rebuild that duplicates; a rejected label coming back on re-run; a bin or label pointing at a deleted span.
- The job under stress: memory and disk floors, archived or open essence, lease lost mid-batch, restart at every batch boundary, runaway label count, hostile engine output (huge, out of range, wrong kind, markup in text).
- The two backends diverging: FTS5 vs `tsvector` syntax, prefix and diacritics, boolean and timestamp handling, a text query that scans instead of using the index, a migration that works on one.

---

### Task 1: Metadata model and validation

**Files:**
- Create: `control/volt/metadata.py`, `control/tests/test_metadata.py`
- Modify: `control/volt/migrations.py` (`_TYPES`, new `Migration` for `field_def`, `field_term`, `asset_meta_value`), `control/volt/store.py` (`SCHEMA`), `control/volt/schema.sql`, `control/volt/retention.py` (`purge_asset` list), `control/tests/historical_shapes.py` only if the parity test asks

**Interfaces:**
- Produces: `class MetaError(Exception)` with `status`, `code`, `reason`; `FIELD_TYPES = ("text","longtext","number","date","bool","enum")`; `define_field(store, key, label, type, *, show_name=None, required=False, multi=False, actor="desk") -> dict`; `retire_field(store, key) -> None`; `add_terms(store, key, terms) -> list[str]`; `retire_term(store, key, term) -> None`; `list_schema(store, show_name=None) -> list[dict]` (active fields with terms); `validate(store, asset_id, key, values) -> list[tuple]` (typed rows `(ord, v_text, v_num, v_date)`); `set_values(store, asset_id, key, values, actor) -> dict` returning `{"old": [...], "new": [...]}`; `get_values(store, asset_id) -> dict` with `values` and `missing`.
- Codes: `bad_input`, `field_exists`, `type_locked`, `unknown_field`, `type_mismatch`, `not_in_vocabulary`, `required_field`.

- [ ] **Step 1: Write the failing tests** (`MetadataModel`): `test_define_and_list_field`; `test_key_pattern_refused` (`Bad-Key`, empty, 41 chars -> `bad_input`); `test_type_locked_after_first_value` (`field_exists` for a repeat key, `type_locked` for a type change after a value); `test_number_date_bool_enum_text_validated` (NaN, `1e16`, `2026-02-30`, wrong enum term, 1,001-char text each refuse with the right code); `test_enum_retired_term_valid_on_old_values_refused_on_new`; `test_show_scoped_field_unknown_for_other_show` (`unknown_field`); `test_multi_cap_20`; `test_required_cannot_be_cleared` (`required_field`) and `test_missing_lists_required_unfilled`; `test_active_field_cap_60`; `test_purge_removes_meta_rows` (`retention.purge_asset` leaves no `asset_meta_value` row and, on Postgres, no FK error); `test_set_replaces_atomically` (a failing second value leaves the first set intact). A `Migration` test in `test_migrations.py` style: a database at the previous head gains the three tables, on SQLite and (skipped without DSN) Postgres.
- [ ] **Step 2: Run** `python -m unittest tests.test_metadata tests.test_migrations -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the tables (SCHEMA, schema.sql, one Migration with per-backend statements and the two indexes), `_TYPES` additions, `metadata.py`, and the purge entries.
- [ ] **Step 4: Run** the same command, then the full suite, then both again with `VOLT_TEST_PG_DSN`. Expected: PASS.
- [ ] **Step 5: Commit** `Metadata: typed fields, vocabularies, validation, migration`.

### Task 2: Schema and metadata API

**Files:**
- Modify: `control/volt/serve.py` (`ROUTES`, `ROUTE_ROLES`, `_AUDIT_KIND`, `_audit_fields`, `_route` GETs), `docs/specs/operator-api.md`
- Test: `control/tests/test_metadata_api.py` (new; follow `test_schedule_api.py` for the server fixture)

**Interfaces:**
- Produces `GET /api/schema`, `POST /api/schema/fields` (admin: `act_schema_field`), `POST /api/schema/fields/{key}/retire` (admin: `act_schema_retire`), `POST /api/schema/fields/{key}/terms` (operator: `act_schema_terms`), `POST /api/schema/fields/{key}/terms/retire` (admin: `act_schema_term_retire`), `GET /api/assets/{id}/metadata`, `POST /api/assets/{id}/metadata` (operator: `act_asset_metadata`, body `{key, values}`). `MetaError` maps to `ApiError(status, code, reason)`. The metadata route audits `detail` as JSON of old and new values.

- [ ] **Step 1: Write the failing tests** (`SchemaApi`): `test_roles` (every `act_*` above has the stated role in `ROUTE_ROLES`; an operator token gets `forbidden_role` on the admin ones); `test_unclassified_post_is_admin_only`; `test_set_metadata_roundtrip_and_missing`; `test_errors_carry_codes` (`unknown_field`, `type_mismatch`, `not_in_vocabulary`, `required_field` as 422, `type_locked` as 409); `test_audit_row_has_old_and_new`; `test_unknown_body_keys_refused` (the `_unknown_keys` rule).
- [ ] **Step 2: Run** `python -m unittest tests.test_metadata_api -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the routes and audit mapping; document them.
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Metadata: schema and asset metadata routes`.

### Task 3: Search index and query engine

**Files:**
- Create: `control/volt/searchindex.py`, `control/tests/test_search.py`
- Modify: `control/volt/migrations.py` (new `Migration`: `search_doc` plus FTS5 table and triggers on SQLite, `tsvector` column and GIN index on Postgres), `store.py`, `schema.sql`, `retention.py`, `preflight.py` (FTS5 present or a warning)

**Interfaces:**
- Produces: `class SearchError(Exception)` (`code`, `reason`, `status`); `rebuild_asset(store, asset_id) -> dict` (transcript passages, `meta` row, label rows; idempotent); `rebuild_all(store) -> int`; `passages(words) -> list` (cut at gap over 1.5 s, 25 words or 12 s); `parse_text(q) -> Terms` (terms, phrases, negations, last-term prefix); `parse_filter(obj) -> Filter` (validated, refuses unknown field or op); `search(store, request: dict, principal=None) -> dict` returning `{assets|hits, facets, next, degraded}`; `encode_cursor(key, id)` / `decode_cursor(raw)` refusing foreign cursors; `fts_available(store) -> bool`.
- Weights: meta 1.5, confirmed label 1.2, transcript 1.0, proposed label 0.8 x confidence; face labels absent unless `confirmed` or `include=proposed`.

- [ ] **Step 1: Write the failing tests** (`SearchModel`, `SearchParity`, `SearchPlans`): `test_passages_cut_rules`; `test_transcript_hit_returns_first_word_span_and_in_point`; `test_reindex_does_not_duplicate` (the library acceptance line); `test_rebuild_from_sources_matches_live` (truncate `search_doc`, `rebuild_all`, same results); `test_tombstoned_asset_excluded_archived_included_flagged`; `test_text_parser` (phrase, `-term`, prefix, hostile `" OR ) * NEAR(` -> terms or `bad_input`, never a backend error); `test_filter_grammar_refusals`; `test_facets_count_over_filtered_set_with_other`; `test_keyset_paging_no_gap_no_repeat` (60 assets, limit 7); `test_foreign_cursor_refused`; `test_face_proposed_hidden_by_default`; `test_rejected_never_matches`; `test_blocked_by_shape_matches_spans_route`; `test_fallback_like_when_fts_missing` (patch `fts_available`, `degraded` true); `test_sqlite_plan_uses_fts` (`EXPLAIN QUERY PLAN` names the FTS table, no scan of `span`); Postgres-only `test_pg_plan_uses_gin`; `test_backends_return_same_asset_and_hit_sets` (one fixture run on both, skipped without DSN).
- [ ] **Step 2: Run** `python -m unittest tests.test_search -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, adapter (one class per backend behind `search`), passage builder and rebuild.
- [ ] **Step 4: Run** the file on SQLite, on Postgres, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Search: derived passage index, FTS5 and tsvector, faceted query`.

### Task 4: Search routes, saved searches and bins

**Files:**
- Modify: `control/volt/serve.py`, `control/volt/migrations.py` (`saved_search`, `bin`, `bin_member`), `store.py`, `schema.sql`, `retention.py` (`bin_member`), `docs/specs/operator-api.md`
- Create: `control/volt/collections.py`; Test: `control/tests/test_search_api.py`

**Interfaces:**
- Produces: `GET /api/search` (params `q scope filter facets sort group include limit after`; `filter` is URL-encoded JSON up to 4 KB); `GET/POST /api/searches`, `POST /api/searches/{id}/delete` (`act_search_save`, `act_search_delete`, operator); `GET /api/bins`, `GET /api/bins/{id}`, `POST /api/bins` (`act_bin_create`), `.../add` (`act_bin_add`), `.../remove` (`act_bin_remove`), `.../delete` (`act_bin_delete`), all operator. `collections.add_member(store, bin_id, asset_id, span_id=None, actor) -> dict`, `members(store, bin_id) -> list` (`gone: true` when a span no longer exists). A face-by-person clause writes audit `search_person` with the person id only.

- [ ] **Step 1: Write the failing tests** (`SearchApi`, `SavedSearches`, `Bins`): `test_search_route_shape_and_unknown_params_refused`; `test_limit_over_100_refused`; `test_saved_search_owner_and_shared` (another operator can run a shared one, cannot delete it; admin can); `test_saved_search_runs_through_search_not_around_it`; `test_limits_200_searches_200_bins_5000_members` (`bin_full`); `test_bin_add_idempotent`; `test_bin_survives_span_rewrite` (detect or transcript replace deletes the span; member reads `gone`, nothing raises); `test_purge_removes_bin_members`; `test_person_search_writes_audit_without_name`; `test_roles_in_route_roles`.
- [ ] **Step 2: Run** `python -m unittest tests.test_search_api -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, the full suite, and with `VOLT_TEST_PG_DSN`. Expected: PASS.
- [ ] **Step 5: Commit** `Search: routes, saved searches, bins`.

### Task 5: Labels, the engine interface and review

**Files:**
- Create: `control/volt/indexing.py`, `control/tests/test_labels.py`
- Modify: `migrations.py` (`label`, `index_run`, `index_batch`), `store.py`, `schema.sql`, `retention.py` (all three), `serve.py` (label routes), `searchindex.py` (label rows)

**Interfaces:**
- Produces dataclasses `Frame(index, seconds, path)`, `Detection(kind, value, confidence, seconds, box=None, person_id=None)`; `class IndexEngine(Protocol)` with `describe()`, `analyse(frames, kinds, people=())`, `enrol(image, box=None) -> bytes`; `class FakeIndexEngine(script, remote=False, fail_on_call=None)`; `clean_detections(raw, batch_seconds, gallery_ids) -> (list[Detection], dict counts)` (drops out-of-range, over-long, unknown-kind, out-of-batch, foreign-person); `merge(detections, every_seconds) -> list` (same kind, value_norm, person within two intervals, confidence max); `write_labels(store, asset_id, run, labels) -> dict` (the replace rule: delete earlier `proposed` engine labels only, suppress overlap with `rejected`); `review(store, label_id, action, actor, value=None, person_id=None) -> dict`; `add_manual(store, asset_id, kind, value, frame_in, frame_out, actor) -> dict`. Routes: `GET /api/assets/{id}/labels`, `POST /api/assets/{id}/labels` (`act_label_add`), `POST /api/labels/{id}/review` (`act_label_review`), both operator; `review` actions `confirm reject reopen correct`; `correct` keeps `engine_value`.

- [ ] **Step 1: Write the failing tests** (`CleanOutput`, `WriteRule`, `Review`): `test_hostile_detections_dropped_and_counted` (confidence 7, NaN, 201-char value, `<script>` kept as inert text, unknown kind, time outside batch); `test_merge_consecutive_samples_one_label_max_confidence`; `test_rerun_replaces_proposed_only` (confirmed, rejected and manual labels untouched); `test_rejection_survives_rerun_and_suppresses` (`suppressed` count on the run); `test_label_cap_keeps_highest_confidence`; `test_correct_keeps_engine_value`; `test_confirm_reject_reopen_cycle`; `test_manual_label_outside_source_refused` (`outside_source`); `test_drop_frame_labels` (29.97 DF asset, label tc skips nonexistent labels via `shots.Facts.label`); `test_purge_removes_label_run_batch_rows`; `test_label_review_updates_search_doc_in_same_transaction`; `test_review_audit_has_ids_not_values_of_people`; `test_routes_roles`.
- [ ] **Step 2: Run** `python -m unittest tests.test_labels -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, the full suite, Postgres. Expected: PASS.
- [ ] **Step 5: Commit** `Indexing: label model, engine interface, review`.

### Task 6: The ai-index job

**Files:**
- Modify: `control/volt/runner.py` (`Config` fields and `from_env`, `default_handlers`, a `Periodic`, the `asr` handler enqueues `search`), `control/volt/indexing.py` (`request_index`, `run_index`, memory probe), `control/volt/serve.py` (`act_asset_index`), `docs/specs/runner.md`
- Test: `control/tests/test_index_job.py`, `control/tests/test_runner.py` (config parse)

**Interfaces:**
- Produces: `indexing.request_index(store, asset_id, config) -> (Job, already_done)`; `indexing.run_index(store, asset_id, engine, config, *, job_id, claimed_by, frames=extract_frames, available_memory=available_memory, run=subprocess.run) -> dict` (returns `{"lost": True}` like `shots.detect`); `indexing.available_memory() -> int | None`; `indexing.index_key(engine_fingerprint, kinds, every, floors) -> str` (`ai-v1:<hash>`); `runner.index_pending_task(config)`; `Config.index_*` from `VOLT_INDEX_ENGINE`, `VOLT_INDEX_KINDS`, `VOLT_INDEX_EVERY_SECONDS`, `VOLT_INDEX_MAX_FRAMES`, `VOLT_INDEX_BATCH`, `VOLT_INDEX_MIN_CONF`, `VOLT_INDEX_MIN_FREE_MEM_MB`, `VOLT_INDEX_MAX_LABELS`, `VOLT_INDEX_DIR`, `VOLT_INDEX_SCAN_EVERY`. Handler outcomes: no engine -> `Permanent`; open essence, missing or open proxy, low disk, low memory -> `Defer`; cold hi-res with a missing proxy -> `Defer` with the rehydrate hint; engine error -> retry; a faces-only gate failure -> run proceeds without faces and says so in `index_run.settings`.

- [ ] **Step 1: Write the failing tests** (`IndexJob`, `Limits`, `Resume`): `test_no_engine_is_permanent_failure`; `test_open_essence_and_open_proxy_defer`; `test_archived_asset_with_local_proxy_is_indexed_and_hi_res_never_read` (fake extractor asserts it is only given the proxy path); `test_archived_asset_missing_proxy_defers_with_hint`; `test_low_disk_defers` (patch `disk.free_bytes`); `test_low_memory_defers_between_batches`; `test_unreadable_memory_does_not_block`; `test_sample_interval_widens_to_frame_cap`; `test_batches_bound_scratch` (never more than `batch` frame files exist at once; all gone after); `test_crash_after_batch_resumes_at_next_batch` (fake engine `fail_on_call=3`; second run calls the engine only for unfinished batches); `test_changed_configuration_discards_saved_batches`; `test_lost_lease_writes_nothing` (claim taken by another runner mid-run); `test_same_config_is_noop_new_config_new_run`; `test_uncleared_asset_skips_faces_only`; `test_enqueues_search_job_after_write`; `test_asr_handler_enqueues_search`; `test_pending_task_enqueues_at_most_50_oldest_first_and_is_off_without_engine`; `test_route_codes` (`index_disabled`, `essence_open`, `no_proxy`).
- [ ] **Step 2: Run** `python -m unittest tests.test_index_job -v`. Expected: FAIL.
- [ ] **Step 3: Implement**; the real frame extraction (ffmpeg, batch) is behind the injected callable and has one `skipUnless(ffmpeg)` test on a generated clip.
- [ ] **Step 4: Run** the file, `tests.test_runner`, the full suite, Postgres. Expected: PASS.
- [ ] **Step 5: Commit** `Indexing: ai-index job with floors, resume and chaining`.

### Task 7: People, face gate, erase

**Files:**
- Create: `control/volt/people.py`, `control/tests/test_people.py`
- Modify: `migrations.py` (`person`, `person_template`), `store.py`, `schema.sql`, `retention.py` (`person_template` is not asset-bound: erase is the only delete), `serve.py`, `indexing.py` (gallery assembly), `searchindex.py`

**Interfaces:**
- Produces: `people.add(store, display_name, basis, review_by=None, actor) -> dict` (basis 10 to 500 chars); `people.enrol(store, person_id, asset_id, frame, box, engine, extract_frame) -> dict` (raises `AmbiguousFace`; the image is deleted, only the template and `engine_fingerprint` are stored); `people.set_enabled(store, person_id, enabled, actor)`; `people.erase(store, person_id, actor) -> dict` (one transaction: templates, `face` labels, `search_doc` rows, name replaced by a fixed placeholder, `erased_at`); `people.gallery(store, engine_fingerprint) -> list[(id, template)]`; `people.list_people(store) -> list[dict]` (never the template). Routes per the spec; `POST /api/people` and `/enrol` operator, `/erase` admin; `faces_disabled` 503 when `face` is not in `VOLT_INDEX_KINDS`.

- [ ] **Step 1: Write the failing tests** (`People`, `Erase`, `GalleryRules`): `test_basis_required`; `test_enrol_stores_template_not_image` (no file left in scratch; no API field returns bytes); `test_ambiguous_face_422_without_box`; `test_gallery_excludes_disabled_erased_and_stale_templates`; `test_engine_change_marks_needs_reenrolment`; `test_erase_leaves_nothing` (scan every table's text columns, the FTS table and every audit `detail` for the person's name and for the template bytes; none found); `test_erase_counts_and_face_facets_drop`; `test_audit_detail_uses_ids_never_names` (enrol, disable, erase, confirm on a face); `test_disable_hides_labels_keeps_rows`; `test_erase_is_admin_only`; `test_unconfirmed_face_not_in_default_search`; `test_run_audit_row_counts_only`; `test_unmatched_faces_never_stored` (fake engine reports 3 unmatched; no row holds a box or crop, `index_run.unmatched_faces == 3`).
- [ ] **Step 2: Run** `python -m unittest tests.test_people -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, the full suite, Postgres. Expected: PASS.
- [ ] **Step 5: Commit** `People: opt-in face gallery, enrolment from a frame, erase`.

### Task 8: CommandEngine, the remote guard, reference worker

**Files:**
- Create: `control/volt/index_command.py`, `tools/index-worker/README.md`, `tools/index-worker/worker.py`, `docs/specs/indexing-engine.md`, `control/tests/test_index_command.py`, `control/tests/fake_index_worker.py`
- Modify: `control/volt/runner.py` (engine construction), `docs/specs/runner.md`

**Interfaces:**
- Produces: `CommandEngine(cmd, timeout=120.0, remote=False, name="command")` implementing `IndexEngine` over JSON lines (`{"op":"describe"}`, `{"op":"analyse","frames":[{"index","seconds","path"}],"kinds":[...],"people":[...]}`, `{"op":"enrol",...}`; one JSON reply per line); `guard_remote(engine, kinds, env) -> kinds` (raises `Permanent`-mapped `RemoteRefused` unless the adapter name is in `VOLT_INDEX_REMOTE_OK`; always strips `face`; people never passed); `build_engine(config)` reading `VOLT_INDEX_ENGINE` (`command`, `cloud:<name>`). The reference worker has a registry of per-kind backends and ships only a stub backend; real backends and model licences are the builder's to choose and to mark unvalidated in `tools/index-worker/README.md`.

- [ ] **Step 1: Write the failing tests** (`CommandProtocol`, `RemoteGuard`): `test_roundtrip_with_fake_worker_script` (a Python script in `tests/` answers the protocol); `test_timeout_kills_child_and_raises`; `test_child_crash_is_retryable_not_runner_crash`; `test_malformed_reply_is_engine_error`; `test_stdout_noise_does_not_desync`; `test_remote_without_name_refused_before_any_frame_read`; `test_remote_never_gets_face_or_templates`; `test_remote_run_audit_row_written_before_first_frame` (order checked with a fake that records calls); `test_remote_flag_recorded_on_index_run`; `test_describe_must_declare_remote` (a worker omitting it is treated as remote).
- [ ] **Step 2: Run** `python -m unittest tests.test_index_command -v`. Expected: FAIL.
- [ ] **Step 3: Implement**; write the protocol into `docs/specs/indexing-engine.md` including "unvalidated: no real model has run".
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Indexing: command engine, remote guard, reference worker skeleton`.

### Task 9: Library UI: Find, Details, Index review, People

**Files:**
- Modify: `control/volt/session/operator/index.html` (tabs, panels, sprite symbols), `operator.js`, `operator.css`, `docs/specs/operator-ui.md`, `docs/brand-guide.md` (only if a token is added), `control/tests/operator_harness.py` (scenarios `find`, `review`, `details`, `people`)
- Test: `control/tests/test_operator_ui.py` (new classes `FindUi`, `ReviewUi`, `DetailsUi`)

**Interfaces:**
- Produces: tabs `lib-tab-find`, `lib-tab-details`, `lib-tab-review` in `#lib-tabs` (arrow keys already move between tabs); `STATE.proposed`, `STATE.confirmed`, `STATE.rejected`, `STATE.incomplete`, `STATE.cloud`; sprite ids `search tag person ocr bin`; pure functions above the node early-return: `buildSearchRequest(ui) -> object`, `parseSearchResponse(json)`, `reviewKeyAction(key, state) -> action|null`. Keys (Library only, never in a field): `/` focus search, `C` confirm, `R` reject, `E` correct, Up/Down rows, Enter seek.

- [ ] **Step 1: Write the failing tests:** `test_state_specs_have_icon_word_class_and_sprite_id`; `test_sprite_ids_exist`; `test_no_innerhtml_added` (count in `operator.js` equals the count at the start of the task, 3); `test_build_search_request_encodes_filter_and_include_off_by_default`; `test_review_keys_ignored_in_inputs_and_other_screens`; `test_label_text_rendered_as_text` (a label `<img src=x onerror=1>` through the render function yields a text node under node); `test_proposed_face_reads_possible_match_not_a_name`; `test_no_confirm_all_control`; `test_incomplete_chip_and_required_word`; both-theme token presence and the contrast test over the new tokens; `test_csp_clean` (no `http(s)://`, `style=`, inline script).
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Then, with the harness scenarios, read each new tab at 1920, 1366 and 768 wide in each workspace and both themes; run axe-core 4.10.2 on each (as in `plans/2026-10-31-ui-redesign-slice-4.md` Task 3) and fix findings.
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS; axe: no violations, no horizontal scroll.
- [ ] **Step 5: Commit** `Library: Find, Details, Index review and People`; re-shoot `docs/mockups` captures for the three tabs in a separate commit.

### Task 10: Bench harness, health, documentation

**Files:**
- Create: `control/tests/bench_search.py`, `control/tests/test_bench_search.py`, `docs/specs/search.md`
- Modify: `control/volt/health.py` (indexed, queued, failed counts), `docs/architecture.md`, `docs/data-model.md`, `docs/specs/library.md`, `docs/roadmap.md`, `docs/operations.md`, `CHANGELOG.md`

**Interfaces:**
- Produces: `python -m tests.bench_search --db <path or dsn> --profile news|long --assets 10000 --seed 1 --runs 200` printing JSON `{"profile", "assets", "db_bytes", "index_bytes", "rss_bytes", "queries": {"<class>": {"p50", "p95", "p99"}}}`; `generate(store, assets, profile, seed)` bulk-inserts through `executemany`, deterministic for a seed; `QUERY_SET` of 40 named queries across the classes in the spec. `health.collect` gains an `indexing` block.

- [ ] **Step 1: Write the failing tests:** `test_generator_is_deterministic` (same seed, same checksum of a sampled table); `test_small_corpus_300_assets_search_returns_nonempty_for_every_query_class`; `test_query_plans_at_300_assets` (the text classes use the FTS or GIN index, none scans `span`); `test_bench_output_shape`; `test_health_reports_indexing_counts`.
- [ ] **Step 2: Run** `python -m unittest tests.test_bench_search -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Then run the bench by hand: `python -m tests.bench_search --db <tmp>.db --profile news --assets 10000`, and the same against `VOLT_TEST_PG_DSN`. Record the numbers, the machine and the date in `docs/specs/search.md` under "Measured", and the targets they missed, if any, under "Limits and unproven". If a run is not made, that section says so.
- [ ] **Step 4: Update the docs:** `data-model.md` (labels, metadata, people), `library.md` (the new search beside `library.search`), `architecture.md` (the derived index and the two full-text engines), `operations.md` (the new env vars, erase, backups holding old templates), `roadmap.md` and `CHANGELOG.md`; state what is unvalidated in the same words as the spec. Run the full suite on both databases.
- [ ] **Step 5: Commit** `Search and indexing: bench harness, health, docs`; open a PR titled "Search, metadata and AI indexing".
