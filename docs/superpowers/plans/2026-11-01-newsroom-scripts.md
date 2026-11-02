# Volt: newsroom scripts and workflow, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A script editor with markup and read time, a story status and approval workflow with a recorded approver, a prompter feed and page, wire intake into an inbox, floated stories, templates, and a story archive, all behind the existing operator model.

**Architecture:** A new pure module `volt/story.py` (markup parsing, stats, transitions, approval policy) with a JavaScript twin for live counts; one migration adding columns and tables; thin `act_*` handlers in `serve.py` over `story.py`; the take path gains an optional approval gate; wire intake is a runner `Periodic` with injected fetcher and pure parsers; the UI is one new Scripts screen plus a prompter page, in the static operator directory.

**Tech Stack:** Python standard library, `unittest` with fakes (`control/tests`), SQLite and Postgres (`VOLT_TEST_PG_DSN` for the live-Postgres classes), plain JS tested under node and axe-core as in `tests/test_operator_ui.py`.

**Spec:** [2026-11-01-newsroom-scripts-design.md](../specs/2026-11-01-newsroom-scripts-design.md). Names below marked "proposed" are this plan's choices; every name marked "existing" was read in the tree on 2026-11-01 and the builder re-checks it before use.

## Global Constraints

- Rules from [the remaining-build plan](2026-10-31-remaining-build.md) hold: own branch and PR per task or small group; TDD with fakes, no network; a historical-shape forward-migration test on SQLite and Postgres for the new head; every new POST in `ROUTE_ROLES` (existing, `serve.py`) and audited (the POST handler calls `store.append_audit` when `ok` is true; add new URL prefixes to `_AUDIT_KIND`); suite green (`cd control && python -m unittest discover -s tests -v`) before the next task.
- The migration number is whatever follows the current head (21 when this was written); do not hard-code it in prose. `schema.sql` and `store.SCHEMA` change together (existing parity test in `tests/test_postgres.py`).
- Roles stay `operator` and `admin`. Approval policy sits behind `story.can_approve`; do not design identity here.
- UI: static files, same-origin CSP, no framework, state language (icon, word, colour), both themes and the contrast test for any new token, existing keys unchanged (Ctrl+S inside the editor textarea is the one addition and goes in the keys table), icons added to the sprite and `Icons.SPRITE_IDS` test.
- Markup never creates or fires a cue. Nothing from a wire reaches a rundown without a human action. Off-by-default for anything that can refuse or alter a take.
- Anything needing a real newsroom system, prompter, wire service or anchor is worded "unvalidated" in the shipped docs.
- Commit messages end with the repository's attribution line; do not commit in the planning step.

## Review Focus

1. **Approval outliving its text.** Every path that changes slug or script (script route, `rename`, copy, template, recurrence clone, wire take) must drop or reset approval; grep for every `UPDATE item SET script` and `INSERT INTO item`.
2. **Gate coverage.** With `VOLT_REQUIRE_APPROVAL=1`, an unapproved story must be refused by Go, `take`, the clock's scheduled take, `show_input` and the cue path; find every caller of the take path, and prove a refused clock take leaves a failed schedule state, not a silent retry.
3. **Leaking private text.** `//` notes must be absent from the prompter feed, text export, MOS story body, wire-take echo and archive search results shown to non-editors of that story; test each output with a note in the script.
4. **Lost updates and races.** Two saves at one revision; save racing approve; double wire take; delete-show racing a save; revision pruning removing a revision an approval points at.
5. **Hostile input.** Script and wire bodies rendered only as text nodes; XML with DOCTYPE/ENTITY refused before parse; oversize feed bodies and slow servers bounded; a prompter ticket unusable on any other route or show; `LIKE` wildcards in a search escaped.

---

### Task 1: Script markup, counts and read time (pure, both languages)

**Files:**
- Create: `control/volt/story.py`, `control/tests/golden/script_markup.json`, `control/tests/test_story_markup.py`
- Modify: `control/volt/session/operator/operator.js` (add `scriptStats`, export it in the existing `module.exports`), `control/tests/test_operator_ui.py` (node parity test)

**Interfaces:**
- Produces (proposed): `story.Block` (`kind`, `text`, `para`), `story.parse_script(text: str) -> list[Block]`, `story.script_stats(text: str, wpm: int = 160) -> dict` returning `{words, readSeconds, instructions, notes, warnings}`, `story.read_text(text) -> str` (read blocks only, paragraphs joined), `story.DEFAULT_WPM = 160`, `story.WPM_RANGE = (60, 400)`. JS: `scriptStats(text, wpm)` with identical output keys.
- Consumes: nothing.

- [ ] **Step 1: Failing tests** in `test_story_markup.py`, driven by the fixture file (each case: `text`, `wpm`, expected stats and blocks): `test_instruction_not_counted` (`[SOT 0:23]` adds 0 words, 1 instruction), `test_private_note_excluded` (`// call the mayor` adds 0 words, 1 note, absent from `read_text`), `test_unclosed_bracket_is_read_and_warned` (`warnings == ["unclosed_bracket"]`, words counted), `test_paragraphs`, `test_read_seconds_rounds` (160 words at 160 wpm is 60), `test_crlf_and_blank_lines`, `test_empty_script_is_zero`, `test_wpm_out_of_range_raises ValueError`, `test_unicode_words_counted_by_whitespace`. In `test_operator_ui.py`, `test_script_stats_twin` runs `scriptStats` under node over the same fixture and asserts equality with the expected values.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_story_markup -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `story.py` and `scriptStats`.
- [ ] **Step 4: Run** the new tests and `tests.test_operator_ui`. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: markup parsing and read time, with a JavaScript twin`.

### Task 2: Migration, revisions and the script save

**Files:**
- Modify: `control/volt/migrations.py`, `control/volt/store.py` (SCHEMA), `control/volt/schema.sql`, `control/volt/story.py`, `control/volt/building.py` (`rename_item` delegates), `control/volt/serve.py` (`act_story_script`, `GET /api/items/{id}/story`, `_AUDIT_KIND` unchanged for `items`, `ROUTE_ROLES`), `control/volt/clock.py` (clone resets), `control/tests/historical_shapes.py` only if a new shape is needed
- Create: `control/tests/test_story_save.py`
- Modify: `control/tests/test_migrations.py` (forward test for the new head)

**Interfaces:**
- Adds the columns and the tables `story_rev`, `story_event` from the spec's data model.
- Produces (proposed): `story.StaleScript(Exception)` carrying `current` (rev, script, slug, actor, at); `story.set_script(store, item_id, *, script=None, slug=None, base_rev=None, actor: str) -> dict` returning `{rev, changed, storyStatus}`, one transaction under `rundown.show_lock(show_id)` (existing) that checks the revision, writes `item`, inserts `story_rev`, prunes to 50 (never the approved revision), appends a `story_event`, and drops approval; `story.get_story(store, item_id) -> dict`. Route `POST /api/items/{id}/script` (operator), `GET /api/items/{id}/story`. Error mapping through the existing `building.BuildError(status, code, reason, **extra)` pattern: `stale_script` 409 with `current`, `not_editable` 409, `story_killed` 409, `not_found` 404, `bad_input` 422.
- Consumes: `store.transaction(lock)`, `rundown.show_lock`, `building._item_row`, `serve._unknown_keys`, `serve._str`.

- [ ] **Step 1: Failing tests:** `test_save_bumps_rev_and_stores_revision`; `test_same_text_is_noop_same_rev`; `test_stale_base_rev_409_with_current_text` (two saves at rev 1: second gets `stale_script` and the winner's script, nothing written); `test_aired_item_refused_not_editable`; `test_on_air_item_allowed_bumps_rev`; `test_over_20000_chars_422`; `test_prune_keeps_50_and_the_approved_rev`; `test_rename_with_script_goes_through_set_script` (rev bumped, history row, approval dropped, `baseRev` optional there); `test_route_roles_and_audit` (`ROUTE_ROLES["act_story_script"] == "operator"`, an audit row with the token's user); `test_recurrence_clone_resets_story_fields` (clone has rev 1, no approval, `draft`); migration tests `test_forward_from_every_historical_shape_sqlite` and `..._postgres` (skipped without `VOLT_TEST_PG_DSN`) plus a schema parity assertion.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_story_save tests.test_migrations -v`. Expected: FAIL.
- [ ] **Step 3: Implement** migration, SCHEMA, schema.sql, `set_script`, handlers; make `building.rename_item` call `set_script`; update `clock.materialise_recurring`'s clone insert.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: revisions, optimistic save and the story read route`.

### Task 3: Story status, approval and the optional air gate

**Files:**
- Modify: `control/volt/story.py`, `control/volt/serve.py` (`act_story_status`, `ROUTE_ROLES`), `control/volt/playout.py` (gate in `advance` before `take(...)`), `control/volt/rundown.py` and `control/volt/cues.py` only if their take callers bypass `advance`, `control/volt/clock.py` (scheduled take outcome)
- Create: `control/tests/test_story_status.py`

**Interfaces:**
- Produces (proposed): `story.TRANSITIONS: dict[str, set[str]]`, `story.set_status(store, item_id, status, *, base_rev, actor, principal, note=None) -> dict`, `story.can_approve(principal, item_row) -> bool` (default true for any operator/admin; narrowed by `VOLT_APPROVERS`), `story.require_approval() -> bool` (reads `VOLT_REQUIRE_APPROVAL`), `playout.StoryNotApproved(PlayoutError)` mapped to 409 `story_not_approved`. Route `POST /api/items/{id}/story-status` (operator): errors `bad_transition` 409, `not_approver` 403, `stale_script` 409.
- Consumes: Task 2's `set_script`/columns, existing `playout.advance`, `PlayoutError`, `TakingError`.

- [ ] **Step 1: Failing tests:** `test_every_legal_move_and_every_illegal_pair` (table-driven over all status pairs); `test_draft_to_approved_refused`; `test_approve_records_actor_rev_time_and_event`; `test_edit_after_approval_returns_to_ready`; `test_not_approver_403_with_env_list`; `test_gate_off_by_default_takes_unapproved`; `test_gate_on_go_refuses_unapproved_leaves_state_unchanged`; `test_gate_on_scheduled_take_marks_failed_not_retried` (frozen clock); `test_gate_on_allows_approved_at_current_rev_only`; `test_killed_story_not_editable_until_revived`; `test_story_kill_does_not_change_item_status`.
- [ ] **Step 2: Run** `python -m unittest tests.test_story_status -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Find every caller of the take path first (`grep -n "take(" control/volt/*.py`) and place the gate where all of them pass.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: story status, approval record and an optional air gate`.

### Task 4: Floating, reading pace and read-time length

**Files:**
- Modify: `control/volt/story.py`, `control/volt/serve.py` (`act_item_float`, `act_show_pace`, `rundown_payload`, `operator_state.py` media block), `control/volt/playout.py` (`advance` skips floated without marking skipped), `control/volt/mos.py` (omit floated; strip `//` notes from the story body), `control/volt/session/operator/operator.js` (`itemSeconds` fallback, `frontTimes`/`backTimes` ignore floated)
- Create: `control/tests/test_story_float.py`

**Interfaces:**
- Produces (proposed): `story.set_floated(store, item_id, floated, actor)`, `story.set_pace(store, show_id, wpm | None, actor)`; routes `POST /api/items/{id}/float`, `POST /api/shows/{id}/reading-pace` (operator); `/rundown` items gain `storyStatus`, `scriptRev`, `words`, `readSeconds`, `floated`, `lengthSource`; top-level `readWpm`. Error `not_floatable` 409.
- Consumes: Task 1 stats, Task 2 columns, existing `rundown_payload`, `frontTimes`, `backTimes`, `itemSeconds`.

- [ ] **Step 1: Failing tests:** `test_go_passes_over_floated_without_marking_skipped`; `test_unfloat_restores_position`; `test_float_refused_for_on_air_taking_aired_skipped_killed`; `test_floated_excluded_from_totals` (node, `frontTimes`/`backTimes`); `test_span_less_item_uses_read_time` and `test_item_with_media_keeps_media_length` (`lengthSource`); `test_pace_range_422_and_null_clears`; `test_mos_export_omits_floated_and_private_notes` (existing `mos.py` builders, golden compare); `test_rundown_payload_has_new_fields_and_no_script_text`.
- [ ] **Step 2: Run** `python -m unittest tests.test_story_float tests.test_mos tests.test_playout -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Existing MOS golden tests must still pass for shows with no floated item and no notes.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: floated stories, reading pace and read-time length`.

### Task 5: The Scripts screen

**Files:**
- Modify: `control/volt/session/operator/index.html` (screen, sprite symbols, keys table), `operator.css` (tokens in both theme blocks only if new), `operator.js` (screen, `WORKSPACES`/`railFor`, `STATE` entries, `stateChipSpec`), `control/tests/test_operator_ui.py`, `control/tests/operator_harness.py` (a `scripts` scenario)

**Interfaces:**
- Consumes: Tasks 2 to 4 routes; existing `call(...)`, `stateChip`, `STATE`, `el`, `announce`, `WORKSPACES`, `railFor`, `ICON_NAMES`.
- Produces (proposed): `STATE.draft`, `STATE.storyReady`, `STATE.approved`, `STATE.storyKilled`, `STATE.floated`, `STATE.unsaved`; `legalMoves(storyStatus) -> string[]` (mirrors `story.TRANSITIONS`; a test pins the two together); `resolveConflict(mine, theirs, choice)`; screen id `#scripts`; sprite ids `script`, `float`.

- [ ] **Step 1: Failing tests:** `StateLanguage` key set grows; `test_legal_moves_match_server_table` (node output equals the fixture exported by Python); `test_scripts_in_producer_and_operator_rails`; `test_no_inline_style_or_external_urls` (existing guard); `test_ctrl_s_only_inside_editor` (the handler is attached to the textarea, not the document); `test_conflict_keeps_editor_text`; `test_approve_needs_two_presses`; `test_editing_disabled_with_reason_for_aired`; `test_live_count_region_is_polite`; axe over the harness `scripts` scenario in both themes; contrast test passes with any new tokens.
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the list, editor, preview, status control and conflict dialog. Text is always set with `textContent`/the `el` helper.
- [ ] **Step 4: Run** the full suite; screenshot the harness in dark and light at 1280 and 1000 px.
- [ ] **Step 5: Commit** `Scripts: the editor screen`.

### Task 6: Prompter feed, ticket and page

**Files:**
- Modify: `control/volt/tickets.py` (kind `prompter`, bound to a show id), `control/volt/serve.py` (feed route, ticket route, text export, `STATIC` entries, ticket accepted on the prompter feed only, `ROUTE_ROLES`)
- Create: `control/volt/session/operator/prompter.html`, `prompter.js`, `prompter.css`, `control/tests/test_prompter.py`

**Interfaces:**
- Produces (proposed): `story.prompter_feed(store, show_id) -> dict` (spec shape), `story.script_text(store, show_id) -> str`; `tickets.issue(secret, show_id, "prompter", ttl, now)` using the existing signature (add `"prompter"` to `KINDS`; the existing tests that pin `KINDS` are updated deliberately); routes `GET /api/shows/{id}/prompter`, `POST /api/shows/{id}/prompter-ticket` (operator), `GET /api/shows/{id}/script?format=text`; page `/prompter`.
- Consumes: existing `tickets.issue/verify`, `Handler._gate_or_ticket`/`_ticket_on_media_get` pattern, `MEDIA_KEY`.

- [ ] **Step 1: Failing tests:** `test_feed_omits_notes_floated_killed_aired_skipped`; `test_feed_order_and_current`; `test_feed_requires_approved_when_gate_on`; `test_version_changes_only_with_content`; `test_ticket_reads_only_its_show_feed` (other show, other route, `/api/state`, a proxy path: all refused); `test_ticket_expired_refused`; `test_ticket_issue_needs_operator_and_signing_key` (503 `no_media_key` pattern); `test_text_export_has_no_notes`; `test_prompter_page_static_csp` (no inline script/style, served with the CSP header); node test that `prompter.js` builds nodes with `textContent` only and that a script containing `<img onerror=...>` renders as text.
- [ ] **Step 2: Run** `python -m unittest tests.test_prompter tests.test_tickets -v`. Expected: FAIL.
- [ ] **Step 3: Implement** feed, ticket and page (mirror toggle, size, speed in `localStorage` in try/catch, scroll idle until started, jump to current).
- [ ] **Step 4: Run** the full suite; axe on the page in both themes.
- [ ] **Step 5: Commit** `Scripts: a prompter feed and page behind a show-scoped ticket`.

### Task 7: Wire intake and the inbox

**Files:**
- Create: `control/volt/wire.py`, `control/tests/test_wire.py`, `control/tests/golden/wire/` (fixtures: RSS, Atom, NewsML subset, JSON with a mapping, a DOCTYPE file, an oversize file)
- Modify: migration and schema (if not already added in Task 2), `control/volt/runner.py` (register a `Periodic` when feeds are configured), `control/volt/serve.py` (`GET /api/wire`, `act_wire_take`, `act_wire_dismiss`, `_AUDIT_KIND["wire"]`, `ROUTE_ROLES`), `control/volt/session/operator/operator.js` and `index.html` (Wire tab)

**Interfaces:**
- Produces (proposed): `wire.parse_rss(data: bytes) -> list[WireStory]`, `wire.parse_atom`, `wire.parse_newsml`, `wire.parse_json(data, mapping) -> list[WireStory]`, `WireStory(external_id, headline, body, byline, published_at)`, `wire.poll(store, feeds, fetch, now)` where `fetch(url, timeout, max_bytes) -> bytes` is injected, `wire.feeds_from_env(environ)`, `wire.take(store, wire_id, show_id, slug, position, actor) -> item_id`. Errors `already_taken` 409, `not_found` 404, `bad_input` 422.
- Consumes: Task 2 `story` columns (`source_wire_id`), `building.add_item`-style position logic under `show_lock`, `runner.Periodic`.

- [ ] **Step 1: Failing tests:** one parser test per format against its fixture; `test_doctype_and_entity_refused_before_parse`; `test_html_body_reduced_to_text`; `test_oversize_and_timeout_recorded_not_raised` (fake fetcher); `test_duplicate_external_id_updates_new_row_leaves_taken_row`; `test_one_bad_feed_does_not_stop_others`; `test_no_feeds_configured_fetches_nothing`; `test_take_makes_draft_item_with_source_line_and_links_wire_row`; `test_double_take_is_one_item` (two threads, conditional update); `test_dismiss_then_take_refused`; `test_take_into_unknown_show_404`; `test_wire_text_never_becomes_a_cue`; roles and audit.
- [ ] **Step 2: Run** `python -m unittest tests.test_wire -v`. Expected: FAIL.
- [ ] **Step 3: Implement** parsers (standard library `xml.etree` only after the DOCTYPE/ENTITY refusal), poll, take, and the inbox tab.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: wire intake into a story inbox`.

### Task 8: Story and show templates

**Files:**
- Modify: `control/volt/story.py` (or create `control/volt/templates.py`), `control/volt/serve.py`, UI Templates tab (`operator.js`, `index.html`)
- Create: `control/tests/test_story_templates.py`

**Interfaces:**
- Produces (proposed): `templates.create(store, kind, name, body, actor) -> id`, `templates.from_show(store, show_id, name, actor)`, `templates.from_item(store, item_id, name, actor)`, `templates.instantiate_story(store, template_id, show_id, position, actor) -> item_id`, `templates.instantiate_show(store, template_id, title, actor) -> show_id`, `templates.delete`; routes `GET /api/templates`, `POST /api/templates` and `/instantiate` (operator), `POST /api/templates/{id}/delete` (admin).
- Consumes: `building.create_show`, `building.add_item`, existing cue insert pattern from `clock.materialise_recurring`.

- [ ] **Step 1: Failing tests:** `test_instantiated_stories_are_draft_rev_1_no_approval`; `test_show_template_carries_cues_slot_pace_not_spans`; `test_from_show_skips_killed_and_skipped`; `test_template_body_validated` (unknown keys, size cap); `test_delete_is_admin_only`; `test_instantiate_unknown_template_404`; `test_template_text_with_private_notes_kept_for_editors` (notes stay in the template, never on the prompter after instantiation of a draft).
- [ ] **Step 2: Run** `python -m unittest tests.test_story_templates -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Scripts: story and show templates`.

### Task 9: Story archive, search and copy; docs

**Files:**
- Modify: `control/volt/building.py` (`delete_show` snapshots into `story_archive` in the same transaction), `control/volt/story.py` (`search`, `copy_story`), `control/volt/serve.py` (`GET /api/stories`, `act_item_copy`), UI Archive tab, `docs/specs/rundown.md`, `docs/specs/operator-ui.md`, `docs/specs/operator-api.md`, `docs/roadmap.md`, `docs/data-model.md`, `CHANGELOG.md`; create `docs/specs/newsroom-scripts.md` (the shipped behaviour, with the unvalidated list)
- Create: `control/tests/test_story_archive.py`

**Interfaces:**
- Produces (proposed): `story.search(store, *, q, status, show, from_, to, limit, cursor) -> {rows, next}` (live items plus `story_archive`, each row `archived: bool`, notes never included in snippets), `story.copy_story(store, source_id, show_id, position, actor) -> item_id` (accepts `archive:<id>`), route `POST /api/items/{id}/copy`.
- Consumes: `building.delete_show` (existing, refuses shows that have aired anything), Task 2 columns.

- [ ] **Step 1: Failing tests:** `test_delete_show_archives_stories_atomically` (injected failure in the archive insert leaves the show intact); `test_search_matches_slug_and_script_case_insensitive`; `test_search_escapes_like_wildcards` (`%` and `_` literal); `test_search_filters_status_show_dates_and_pages`; `test_search_does_not_expose_private_notes_in_snippets`; `test_copy_creates_draft_rev_1_without_approval`; `test_copy_from_archive_id`; `test_copy_into_show_with_hold_still_allowed` (copy is not a take); Postgres variants skipped without the DSN; docs-contain-unvalidated-list test (the shipped doc names each item in the spec's "will not prove" list).
- [ ] **Step 2: Run** `python -m unittest tests.test_story_archive -v`. Expected: FAIL.
- [ ] **Step 3: Implement** and write the docs.
- [ ] **Step 4: Run** the full suite, then once with `VOLT_TEST_PG_DSN` set against a throwaway database if one is available. Expected: PASS; Postgres classes skipped otherwise.
- [ ] **Step 5: Commit** `Scripts: story archive, search and copy, with docs`.

---

## Ordering and slicing notes

Tasks 1 to 4 are server-only and each ships on its own. Task 5 is the first the producer sees. Tasks 6 to 9 are independent of each other after Task 4 and can be reordered by what the first customer needs first (probably 6 then 7). Task 3's gate and Task 4's `advance` change touch the same function: land them in order, or resolve in one branch.
