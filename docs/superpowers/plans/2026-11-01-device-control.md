# Volt device control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cue drive a vision switcher, a graphics engine, a MOS graphics device and PTZ cameras through one adapter contract, with macros, per-cue timing, preview before take, a device panel and safe mode. Audio scenes and VDCP are separate later plans (spec: adapter order).

**Architecture:** A new package `control/volt/adapters/` (contract plus one executor plus one module per device kind) registered through `roomreg.from_env` and `dispatch.ROOM_HANDLERS`; `take_item`, `dispatch_item` and the advisory rule are unchanged. Timed cues run in the clock. Safe mode, event log and status live in new tables. UI is static files.

**Tech Stack:** Python stdlib (`socket`, `struct`, `threading`, `xml.etree`), SQLite and Postgres, `unittest` in `control/tests/`, fakes on loopback sockets from a new `tests/devicekit.py`. UI in `control/volt/session/operator/`, tested through `tests/test_operator_ui.py` and `tests/operator_harness.py`.

**Spec:** `docs/superpowers/specs/2026-11-01-device-control-design.md`. House style: `docs/superpowers/plans/2026-10-31-remaining-build.md`.

## Global Constraints

- Opt-in: no adapter registers, polls or sends unless its environment variable is set (`VOLT_ATEM_HOSTS`, `VOLT_CASPAR_HOSTS`, `VOLT_MOS_LISTEN`, `VOLT_PTZ_HOSTS`, `VOLT_TALLY_MAP`, all `name=host[:port]` like `VOLT_HYPERDECK_HOSTS`). Nothing set means nothing sent and nothing shown.
- A cue is `fired` only when `ActionSpec.observed` returns `None` on a fresh read after the send. A failed device cue is advisory (`Result.advisory`), never rewinds a take.
- Tests use fakes on loopback sockets only; no test touches a real device or the internet. Anything real is worded **Unvalidated**.
- Protocol facts in the spec are from memory. Each adapter task starts with reading the published document (or the named open implementation, reading only) and recording its title, revision and the bytes it relied on in the adapter's spec page. If it cannot be obtained, stop that task and report; do not guess.
- One new forward migration (number = next free, no gaps), tested forward from every shape in `tests/historical_shapes.py` on SQLite and on Postgres (`VOLT_TEST_PG_DSN`). `schema.sql` matches.
- Every new POST is in `serve.ROUTE_ROLES` and audited by the generic path; `tests/test_users.py` stays green.
- The sqlite connection is never used from a worker thread; device I/O may be, writes are not.
- UI: no `http(s)://`, `style=`, `<style>` or inline script; status is icon plus word plus colour from `STATE`; no new colour state (safe mode reuses `held`); no new single-key shortcut; every new icon is in the sprite and `Icons.SPRITE_IDS`.
- Python is run from `control/`: `cd control && python -m unittest <module> -v`. Full suite: `python -m unittest discover -s tests`.

## Review Focus

- **Acknowledgement taken as proof.** For each adapter, a fake that answers success and changes nothing must leave the cue `failed`, and the fake's state must be what the `observed` function reads, not the reply.
- **A non-idempotent command sent twice.** ATEM `cut`/`auto` and Caspar bare `PLAY` after a dropped connection, a timeout, a clock restart between claim and send: the fake counts commands received and must see one.
- **A send path that skips the gate.** Safe mode, opt-in configuration and the event log must cover the executor, `prepare`, `mos_send.MosSender.step`, the HyperDeck handlers and the retry route. A new code path that opens a socket without `executor.execute` is a defect.
- **Preview that reaches air or is trusted when stale.** Preview commands are only preview commands; at take the preload is re-observed; a changed device falls back to the full action.
- **Timing and threads.** A timed cue sent twice by two clocks, late, or after its item left air; a group member's failure stopping the others; sqlite touched from a worker thread; a hung device holding a take, the clock or the runner.

---

### Task 1: The adapter contract and the executor

**Files:**
- Create: `control/volt/adapters/__init__.py`, `base.py`, `executor.py`; `control/tests/devicekit.py` (loopback `FakeTcpServer`, `FakeUdpServer`, scripted replies, command counter, "acks but never changes" and "drops after N bytes" modes); `control/tests/test_adapter_contract.py`
- Modify: none

**Interfaces:**
- Produces in `base.py`: `Observation(at, state, detail)`, `DeviceError(LiveError)`, `SafeModeError`, `InterlockError`, `OutcomeUnknown`, `ActionSpec(command, parse, perform, observed, idempotent, on_air, prepare=None, prepared=None, interlocks=())`, `Session`, `Adapter` protocols (as in the spec).
- Produces in `executor.py`: `execute(adapter, device, spec, params, ctx, store=None, gate=None) -> Outcome`, `prepare(adapter, device, spec, params, ctx, store=None, gate=None) -> Outcome`, `handler_for(adapter, spec) -> Callable[[Store, object, Cue, dict], None]`, `Outcome(status, detail, events)`. `gate` is a callable `(kind, spec) -> None` that raises `SafeModeError` (task 2 supplies the real one; here a fake).
- Rules: gate, interlocks, connect, baseline observe, one `perform`, poll `observed` with `ctx["attempts"|"interval"|"sleep"]`, two consecutive proofs when `on_air`; reconnect twice before the first byte; after bytes, idempotent re-observes then resends once, non-idempotent only observes.

- [ ] **Step 1: Write the failing tests** in `test_adapter_contract.py` with a toy adapter over `FakeTcpServer`: `test_ack_without_state_change_is_failed` (server says `OK`, state unchanged: `Outcome.status == "failed"`, detail starts `timed out,`); `test_proof_on_fresh_read_is_fired`; `test_on_air_needs_two_consecutive_proofs` (state flips back on the second read: failed); `test_unreachable_is_failed_not_hung` (closed port, returns inside connect timeout plus margin); `test_read_timeout_is_failed`; `test_drop_before_send_reconnects_twice_then_fails`; `test_idempotent_drop_after_send_reobserves_then_resends_once` (server counts 2 commands at most); `test_non_idempotent_drop_after_send_never_resends` (server counts exactly 1; if state already proves it, `fired`; otherwise `OutcomeUnknown` wording "sent; no answer"); `test_interlock_refuses_before_any_byte` (server received 0 bytes); `test_gate_refusal_sends_nothing`; `test_handler_for_has_dispatch_shape_and_raises_liveerror`; `test_parse_error_is_bad_input_not_failed`.
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_adapter_contract -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `base.py`, `executor.py`, `devicekit.py`. Worker threads are not used here.
- [ ] **Step 4: Run** the module, then `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: adapter contract and executor`.

### Task 2: Migration, registry, safe mode, event log, status poller

**Files:**
- Create: `control/volt/safety.py`, `control/volt/devices.py` (status, event log, poller), `control/tests/test_device_safety.py`, `control/tests/test_device_migration.py`
- Modify: `control/volt/migrations.py` (one `Migration`), `control/volt/schema.sql`, `control/volt/roomreg.py` (`from_env`, `configured`), `control/volt/dispatch.py` (`ROOM_HANDLERS` built from registered adapters), `control/volt/building.py` (`cue_commands`, `add_cue` generic over adapters), `control/volt/cues.py` (`add_cue` validation delegates), `control/volt/hyperdeck.py`, `control/volt/mos_send.py` (safe-mode check), `control/volt/runner.py` (poller step, every 2 s), `control/volt/serve.py` (`ROUTE_ROLES`, `POST /api/devices/safe-mode`, `GET /api/devices`, `devices` and `safeMode` in `operator_state`)

**Interfaces:**
- Produces: tables `cue` columns (`offset_ms`, `preroll_ms`, `group_key`, `due_at`, `claimed_at`, `preload_status`, `preload_detail`, `preloaded_at`), `device_status`, `device_event`, `device_safety`, `macro`, `macro_step`, `device_confirm`.
- `safety.is_on(store) -> bool`, `safety.set(store, on: bool, reason: str, actor: str)`, `safety.check_send(store, kind, spec) -> None` (raises `SafeModeError`; CoreVideo kind never refused), `devices.record_event(store, device, command, params, outcome, detail, cue_id=None, actor="system")`, `devices.status(store) -> list[dict]` (`kind, name, state, detail, observed, updatedAt, lastEvent`), `devices.poll(store, registry)` (observation only).
- `adapters.REGISTRY: dict[str, Adapter factory]`; `roomreg.from_env` registers a kind when its variable is set.

- [ ] **Step 1: Write the failing tests.** `test_device_migration`: `test_forward_from_every_historical_shape_sqlite`, `..._postgres` (skipped without `VOLT_TEST_PG_DSN`), `test_existing_cues_read_back_with_zero_offset_and_no_group`. `test_device_safety`: `test_safe_mode_refuses_every_send_path` (executor, `prepare`, `MosSender.step` with a fake device socket sees 0 bytes, HyperDeck handler on `FakeDeck` sees 0 commands); `test_safe_mode_still_observes_and_polls`; `test_corevideo_cue_unaffected_by_safe_mode`; `test_safe_mode_read_each_send_not_cached` (flip between two sends); `test_refused_cue_is_advisory_and_take_proceeds`; `test_nothing_configured_nothing_registered_nothing_shown`; `test_configured_true_for_device_only_env`; `test_every_send_has_an_event_row`; `test_safe_mode_route_role_and_audit`; `test_cue_commands_lists_only_configured_kinds`.
- [ ] **Step 2: Run** `python -m unittest tests.test_device_safety tests.test_device_migration -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep `hyperdeck.py` and `mos_send.py` behaviour identical apart from the gate and event row.
- [ ] **Step 4: Run** the two modules and the full suite, including `tests.test_users`, `tests.test_migrations`, `tests.test_hyperdeck`, `tests.test_mos_send`. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: schema, registry, safe mode, event log`.

### Task 3: ATEM adapter

**Files:**
- Create: `control/volt/adapters/atem.py`, `docs/specs/atem.md`, `control/tests/test_atem.py`
- Modify: `control/volt/adapters/__init__.py`, `docs/specs/cues.md`, `docs/rooms.md` (link only)

**Interfaces:**
- Produces: `atem.AtemAdapter(hosts, connect=None)` with `kind = "atem"`, `ACTIONS` = `atem.preview` (idempotent, prepare-class), `atem.program` (idempotent, on_air), `atem.cut`, `atem.auto` (non-idempotent, on_air); `parse_hosts(text)`, `parse_payload(text) -> {"switcher","input","me"}` (`A/3` shorthand); `AtemSession.observe()` -> state `{"program": int, "preview": int, "transition": bool}`.
- **Before code:** find and read a source for the wire (Blackmagic document if any; SKAARHOJ notes; `atem-connection` source, read only). Record in `docs/specs/atem.md`: sources, packet header layout, the commands used, handshake, ack and resend rules, firmware. If only memory is available, stop and report: the order moves to CasparCG.

- [ ] **Step 1: Write the failing tests** against a `FakeAtem` (UDP, handshake, state dump, command handling, scriptable faults): `test_parse_hosts_and_payload`; `test_handshake_and_state_dump_decoded`; `test_program_fired_only_when_program_input_shows_it`; `test_acked_but_unchanged_is_failed`; `test_cut_requires_preview_equals_target_and_swaps` (after cut: program is old preview, preview is old program); `test_cut_is_never_resent_after_lost_ack` (fake counts one `cut`, state shows the swap: `fired`; fake dropped it: `failed`, one command); `test_auto_waits_for_transition_flag_to_clear`; `test_preview_sets_preview_only_and_prepared_checks_it`; `test_packet_loss_resend_is_protocol_level_not_command_level`; `test_unreachable_switcher_is_failed_not_hung`; `test_unknown_switcher_name_is_failed`; `test_dispatch_advisory_does_not_rewind_take` (CoreVideo cue fired, ATEM cue fails, item on air, later cue runs).
- [ ] **Step 2: Run** `python -m unittest tests.test_atem -v`. Expected: FAIL.
- [ ] **Step 3: Implement**, with `docs/specs/atem.md` carrying "Unvalidated: tested against a fake switcher only".
- [ ] **Step 4: Run** `tests.test_atem`, `tests.test_dispatch`, `tests.test_take`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: ATEM adapter`.

### Task 4: CasparCG AMCP adapter

**Files:**
- Create: `control/volt/adapters/caspar.py`, `docs/specs/casparcg.md`, `control/tests/test_caspar.py`
- Modify: `control/volt/adapters/__init__.py`

**Interfaces:**
- Produces: `CasparAdapter` (`kind = "casparcg"`) with `caspar.load` (prepare-class, idempotent), `caspar.play` (non-idempotent for a bare clip, on_air), `caspar.stop`, `caspar.clear` (idempotent), `caspar.cg.add`, `caspar.cg.play`, `caspar.cg.update`, `caspar.cg.stop`; `render_template_data(obj) -> str` (escaped XML); `parse_info(xml) -> {"foreground","background","paused"}`.
- **Before code:** read the CasparCG wiki AMCP page for the customer's server version and its `INFO` output for that version; decide INFO polling versus OSC and record it.

- [ ] **Step 1: Write the failing tests** against a `FakeCaspar` (TCP, CRLF, `202 ... OK`, `INFO` XML, 4xx/5xx): `test_play_fired_only_when_info_shows_foreground`; `test_load_fired_when_background_shows`; `test_play_after_load_promotes_background`; `test_202_ok_but_info_unchanged_is_failed`; `test_4xx_reply_is_failed_with_server_words`; `test_bare_play_never_resent_after_timeout`; `test_cg_template_data_is_escaped_hostile_names` (a data value with `<`, `&`, quote, NUL); `test_cg_play_fired_when_template_host_shows_template`; `test_unreachable_and_timeout_are_failed_not_hung`; `test_info_for_unknown_version_layout_is_failed_not_guessed`.
- [ ] **Step 2: Run** `python -m unittest tests.test_caspar -v`. Expected: FAIL.
- [ ] **Step 3: Implement** and write the spec page ("Unvalidated").
- [ ] **Step 4: Run** the module and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: CasparCG adapter`.

### Task 5: Macros, cue timing, groups, the device tick

**Files:**
- Create: `control/volt/macros.py`, `control/volt/timeline.py`, `control/tests/test_macros.py`, `control/tests/test_cue_timing.py`
- Modify: `control/volt/dispatch.py` (concurrent immediate groups; `macro.run` handler), `control/volt/take.py` (call `timeline.schedule` after the take), `control/volt/clock.py` (`device_tick`, started whenever a device is configured), `control/volt/building.py` (`add_cue` accepts timing, `set_timing`), `control/volt/serve.py` (`act_cue_timing`, macro routes, `ROUTE_ROLES`), `docs/specs/cues.md`

**Interfaces:**
- Produces: `macros.create(store, name, steps, actor) -> id`, `macros.revise(store, macro_id, steps) -> version`, `macros.get(store, macro_id, version)`, `macros.delete(store, macro_id)` (`in_use` refused), `macros.run_handler(store, registry, cue, ctx)`; `timeline.schedule(store, item_id, t0)`, `timeline.tick(store, registry, now, grace=2.0, lease=300.0) -> list[Event]`, `building.set_timing(store, cue_id, offset_ms, preroll_ms, group)` (validation: device rooms only, `bad_timing` outside 0..600000 and 0..10000).
- Immediate group members run on worker threads; every store write on the caller.

- [ ] **Step 1: Write the failing tests.** `test_macros`: `test_macro_fired_only_if_every_step_fired`; `test_macro_failure_lists_each_failed_step`; `test_macro_rejects_corevideo_steps_nested_macros_over_20_steps_over_60s`; `test_editing_macro_does_not_change_armed_cue` (version pinned); `test_delete_in_use_refused`. `test_cue_timing`: `test_group_members_start_before_any_is_observed` (fakes record start times); `test_one_group_failure_does_not_stop_others`; `test_same_device_in_group_runs_in_position_order`; `test_sqlite_never_touched_off_main_thread` (connection wrapper raises on foreign thread); `test_timed_cue_not_run_inside_take`; `test_timed_cue_runs_at_due_time_frozen_clock`; `test_late_beyond_grace_is_failed_late_nothing_sent`; `test_item_left_air_before_due_nothing_sent`; `test_two_clocks_send_once` (two threads, fake counts one); `test_restart_between_claim_and_send_settles_from_state_never_resends`; `test_preroll_runs_prepare_earlier_and_preroll_over_offset_runs_at_t0`; `test_default_cues_unchanged` (offset 0, no group: old sequential behaviour); `test_corevideo_cue_timing_refused`.
- [ ] **Step 2: Run** `python -m unittest tests.test_macros tests.test_cue_timing -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both modules, `tests.test_clock`, `tests.test_dispatch`, `tests.test_take`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: macros, cue timing and groups`.

### Task 6: Preview (arm) before take, and the interlock engine

**Files:**
- Create: `control/volt/preview.py`, `control/tests/test_preview.py`
- Modify: `control/volt/adapters/executor.py` (`prepare`, interlock evaluation uses the registry), `control/volt/adapters/atem.py`, `caspar.py` (declare `prepare`/`prepared` and the on-air command sets), `control/volt/take.py` (use a still-valid preload), `control/volt/serve.py` (`act_item_preview`, `act_item_preview_clear`, `ROUTE_ROLES`)

**Interfaces:**
- Produces: `preview.run(store, registry, item_id, ctx) -> list[PreviewResult]`, `preview.clear(store, show_id)`, `preview.fresh(store, registry, cue, ctx) -> bool` (re-observes with `prepared`), error classes mapped to `not_next_item`, `not_previewable`, `safe_mode`, `held`. Each adapter exports `ON_AIR_COMMANDS` and `PREPARE_COMMANDS` as data.

- [ ] **Step 1: Write the failing tests.** `test_preview_never_sends_on_air_command` (parametrised over every registered adapter: the fake records every command; none in `ON_AIR_COMMANDS`); `test_preview_runs_prepare_for_next_ready_item_only`; `test_one_item_in_preview_per_show`; `test_clear_sends_nothing`; `test_take_sends_only_small_action_when_preload_fresh` (ATEM: `cut` only; Caspar: `PLAY` only); `test_take_falls_back_to_full_action_when_device_changed` (event "preload lost"); `test_stale_flag_after_restart_costs_nothing`; `test_preview_refused_in_safe_mode_and_when_held`; `test_corevideo_cue_not_previewable`; `test_interlock_reads_other_device_via_registry` (toy interlock refuses with 0 bytes sent).
- [ ] **Step 2: Run** `python -m unittest tests.test_preview -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the module, `tests.test_atem`, `tests.test_caspar`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: preview before take and interlocks`.

### Task 7: Retry, two-key, and the remaining routes

**Files:**
- Create: `control/volt/confirm.py`, `control/tests/test_device_api.py`
- Modify: `control/volt/serve.py` (`act_cue_retry`, `act_device_confirm`, `act_device_confirm_cancel`, `act_macro_*` if not done in task 5, `ROUTE_ROLES`, `GET /api/devices`, `GET /api/macros`), `control/volt/operator_state.py` (`devices`, `safeMode`, per-cue `timing` and `preload`)

**Interfaces:**
- Produces: `confirm.request(store, cue_id, actor) -> id`, `confirm.approve(store, confirm_id, actor, now) -> Cue` (`same_user`, `confirm_expired`, 60 s), `confirm.cancel`. Retry runs `executor.execute` through the same gate, so it cannot bypass safe mode.

- [ ] **Step 1: Write the failing tests.** `test_retry_non_on_air_runs_immediately_and_logs_event`; `test_retry_on_air_returns_202_needs_second_key`; `test_same_user_cannot_confirm_403`; `test_other_user_confirms_and_cue_runs`; `test_confirm_expires_after_60s_410` (frozen clock); `test_legacy_token_gets_two_key_unavailable`; `test_retry_refused_in_safe_mode`; `test_retry_only_for_failed_device_cue_on_air_item`; `test_every_new_post_in_route_roles_and_audited`; `test_state_carries_devices_safe_mode_timing_preload`; `test_error_codes_and_messages` for each code in the spec's route table.
- [ ] **Step 2: Run** `python -m unittest tests.test_device_api tests.test_users -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the modules and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: retry, second key and device routes`.

### Task 8: Operator UI: device panel, safe mode bar, cue timing, preview, macros

**Files:**
- Modify: `control/volt/session/operator/operator.js` (`devicePanel`, `safeModeBar`, cue timing chips and fields, preview button and chip, macro editor, retry controls; `ACTIONS` entries), `operator.css`, `index.html` (sprite: `device`, `preview` if absent), `control/tests/operator_harness.py` (scenarios `devices_up`, `devices_down`, `safe_mode`, `preview_ready`), `docs/specs/operator-ui.md`, `docs/brand-guide.md` only if a token changes (it should not)
- Test: `control/tests/test_operator_ui.py` (new class `DeviceUi`)

**Interfaces:**
- Produces: `DEVICE_STATE` map (`up` to `['ready','Connected']`, `connecting` to `pending`, `down` and `refused` to `failed`, `safe` to `['locked','Safe']`), `devicePanel(devices)`, `safeModeBar(safe)`, `timingChips(cue)`, `previewChip(cue)`.

- [ ] **Step 1: Write the failing tests.** `test_device_state_map_uses_existing_kinds`; `test_icons_exist_in_sprite`; `test_panel_hidden_when_no_device_configured`; `test_safe_mode_bar_has_icon_word_and_is_not_red`; `test_safe_mode_buttons_are_two_press`; `test_no_new_single_key_binding` (the key table in `operator.js` equals the pre-change table); `test_timing_fields_seconds_to_ms`; `test_csp_clean` (no `http(s)://`, `style=`, `<style>`, inline script in the touched files); `test_contrast_both_themes_unchanged_tokens`.
- [ ] **Step 2: Run** `python -m unittest tests.test_operator_ui.DeviceUi -v`. Expected: FAIL.
- [ ] **Step 3: Implement**; start the harness (`python -m tests.operator_harness safe_mode --port 8791`), screenshot each scenario in dark and light, run axe-core 4.10.2 as slice 4 did.
- [ ] **Step 4: Run** the whole `tests.test_operator_ui` and the full suite. Expected: PASS, axe clean.
- [ ] **Step 5: Commit** `Device control: device panel, safe mode and cue timing in the UI`.

### Task 9: MOS object and template push with a status listener

**Files:**
- Create: `control/volt/adapters/mos_objects.py`, `docs/specs/mos-objects.md`, `control/tests/test_mos_objects.py`
- Modify: `control/volt/adapters/__init__.py`, `control/volt/mos_send.py` (export `frame`, `Reader`, `parse` unchanged; no behaviour change), `docs/specs/mos.md` (link)

**Interfaces:**
- Produces: `MosObjectAdapter` (`kind = "mos-objects"`), `StatusListener(host, port, on_status)` (accepts device connections, replies to `heartbeat`, parses status), actions `mos.item.preload` (prepare-class) and `mos.item.take` (on_air) (names to verify); `VOLT_MOS_LISTEN`. `add_cue` refuses with `no_observation` when the listener is not configured.
- **Before code:** read the 2.8.5 sections for item and element messages and device status messages and the port roles; record the message names, required fields and which side connects in `docs/specs/mos-objects.md`.

- [ ] **Step 1: Write the failing tests** (fake MOS device that connects to the listener and receives pushes): `test_roack_alone_is_not_fired`; `test_fired_when_device_status_names_item_ready`; `test_take_fired_only_on_on_air_status`; `test_no_listener_means_no_observation_and_cue_refused`; `test_listener_binds_loopback_by_default_and_is_off_unset`; `test_listener_survives_garbage_and_half_messages`; `test_heartbeat_answered`; `test_status_for_other_item_ignored`; `test_unframed_utf8_is_rejected` (UTF-16BE framing reused from `mos_send.frame`); `test_safe_mode_blocks_push_not_listener`; `test_push_not_resent_blind_after_timeout`.
- [ ] **Step 2: Run** `python -m unittest tests.test_mos_objects -v`. Expected: FAIL.
- [ ] **Step 3: Implement** ("Unvalidated: fake device only").
- [ ] **Step 4: Run** the module, `tests.test_mos_send`, `tests.test_mos`, full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: MOS object and template push`.

### Task 10: PTZ presets with the on-air camera interlock

**Files:**
- Create: `control/volt/adapters/ptz.py`, `docs/specs/ptz.md`, `control/tests/test_ptz.py`
- Modify: `control/volt/adapters/__init__.py`, `docs/specs/operator-ui.md` (cue labels only)

**Interfaces:**
- Produces: `PtzAdapter` (`kind = "ptz"`), actions `ptz.recall` (idempotent), `ptz.learn` (operator-time; refused inside a take), `parse_tally_map(text) -> {camera: (switcher, input)}`, `positions_match(a, b, tolerance)`; interlock `camera_on_program(registry, params)` reading the switcher through the ATEM adapter.
- **Before code:** read the camera maker's VISCA command list for the customer's model and the VISCA-over-IP document: header layout, sequence numbers, the position inquiries, the replies.

- [ ] **Step 1: Write the failing tests** against a `FakePtz` (UDP, moves toward the target over several reads; modes: never arrives, acks but does not move, reports completion before arriving): `test_recall_fired_when_position_matches_learned_on_two_reads`; `test_completion_message_alone_is_not_fired`; `test_camera_still_moving_is_not_fired`; `test_never_arrives_is_failed_not_hung`; `test_learn_stores_reference_and_is_refused_in_a_take`; `test_recall_without_learned_reference_is_failed_with_reason`; `test_interlock_refuses_on_program_camera_zero_bytes_sent` (fake ATEM shows the mapped input on program); `test_interlock_allows_off_air_camera`; `test_missing_tally_map_entry_refuses_not_allows` (unknown means refuse); `test_safe_mode_blocks_recall`.
- [ ] **Step 2: Run** `python -m unittest tests.test_ptz -v`. Expected: FAIL.
- [ ] **Step 3: Implement**, with `docs/specs/ptz.md` ("Unvalidated: no real camera; position resolution and tolerance are guesses until measured").
- [ ] **Step 4: Run** the module and the full suite plus `git diff --stat` review that only listed files changed. Expected: PASS.
- [ ] **Step 5: Commit** `Device control: PTZ presets and the on-air camera interlock`.

---

## After this plan

Separate plans, each gated on the open questions: **audio scenes** (needs a named console and its published protocol), **VDCP** (needs the specification). Real-equipment runs are validation, tracked beside the CoreVideo runs in `docs/rooms.md`, not coding tasks. Update `docs/roadmap.md` ("In the tree" and "Next") and `docs/specs/cues.md` (Rooms) when each adapter lands.
