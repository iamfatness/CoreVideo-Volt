# Volt mobile and remote access: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a producer a phone and tablet layout of the desk that cannot take or kill, make the desk installable and safe to reach over the internet, and make the state poll cheap on a poor link.

**Architecture:** Server changes in `control/volt/serve.py` (conditional state, lite view, room memo, rate limits, trusted-proxy address, viewer role, scope seam, remote media rule) plus a new `ratelimit.py`. Front end in `control/volt/session/operator/` (new `mobile.js` of pure helpers, a service worker at `/sw.js`, a manifest and icons, touch-mode rendering and CSS). No schema change, no new POST, no new dependency.

**Tech Stack:** Python standard library; static HTML, CSS and JS with no build and no framework; `unittest` in `control/tests/` (the `Api` base class from `tests/test_operator_api.py`, `_node` and `FILES` from `tests/test_operator_ui.py`); `node` for pure-function and service-worker tests; a manual real-browser script (Playwright and axe-core 4.10.2 installed in a scratch directory, never in the repo) against `python -m tests.operator_harness`.

**Spec:** [2026-11-01-mobile-remote-design.md](../specs/2026-11-01-mobile-remote-design.md). Depends on the identity spec (sessions, cookies) only for the pieces the spec marks dormant; nothing here blocks on it.

## Global Constraints

- Desk keys, the desk's `armedAction`, and the desk's 2 s / 6 s poll do not change. The 768 and 1366 layouts do not change. `test_the_old_keys_are_unchanged` and the existing suite stay green at every commit.
- Every new POST is classified in `ROUTE_ROLES` and audited. **This plan adds no POST.** `test_every_post_route_has_an_explicit_role` must still pass without edits to its role set.
- The CSP gains exactly `manifest-src 'self'` and `worker-src 'self'`; no `unsafe-*`, no external host, no inline script or style. `test_references_only_same_origin_assets` holds: every new `href` starts with `/operator/`.
- `localStorage` stays limited to `volt.workspace` and `volt.theme`. The token never leaves `sessionStorage`. Nothing under `/api/`, no `Authorization`-bearing request and no media is ever cached or touched by the worker.
- Tests use fakes and a fake clock; no test touches the network or a real push service. Anything needing a real phone, a real certificate or a push provider is worded as unvalidated in the docs.
- State is icon plus word plus colour; new colour tokens go into both theme sets and the contrast test.
- Touch mode does not render TAKE, GO, Kill, Record start or stop, Clear, Legal hold, Publish or Delete. The server never trusts the layout.
- Each task is its own commit; the full suite (`cd control && python -m unittest discover -s tests`) is green before the next starts. CI is read per job (conclusions, not names).

## Review Focus

1. **A touch that acts.** Any path on a phone or tablet where one tap, a double-tap, a scroll ending on a button, a re-render under the finger, or a keyboard shortcut can take, kill, record, clear rights or publish; arming that can be satisfied by a double-tap; layout that moves when a button arms.
2. **Stale shown as live.** A `304` that is treated as "nothing changed" after a show switch or a lost connection; back-off that delays an on-air change beyond 4 s; the worker or the HTTP cache serving an old rundown or an old page against a new API; offline mode that shows the last rundown.
3. **Secrets in the wrong place.** The token, a state payload or a media URL in Cache Storage, a URL, a log line or `localStorage`; an `X-Forwarded-For` honoured from a peer that is not a listed proxy; a `304` or a different error revealing anything to an unauthenticated caller.
4. **Desk regressions.** Changed keys, changed arming, a changed poll cadence, a payload that is not byte-identical when `view` is omitted, a CSP broader than two directives, a layout shift at 768 or 1366.
5. **Rights and lockout.** A `viewer` or `mobile` principal reaching a path, number or picture of an uncleared or legally held asset (ticket issuance, thumbnail, proxy, `assets[].file`, `room.artifactPath`); a rate limit that an attacker's failures can use to block a valid user, or that blocks the desk's own thumbnails and polls.

---

### Task 1: Conditional state, lite view, room memo

**Files:**
- Modify: `control/volt/serve.py` (`_route` branch for `/api/state`, `_operator_state`, `do_POST` success path), `control/volt/operator_state.py` (`build`)
- Create: `control/tests/test_state_conditional.py`

**Interfaces:**
- `serve.state_etag(body: bytes) -> str` returns `'"' + sha256(body).hexdigest()[:32] + '"'`.
- `serve.etag_matches(header: str | None, etag: str) -> bool` accepts `*`, strong and weak forms and comma lists.
- `GET /api/state?show=&view=lite|full`: omitted view is today's payload byte for byte; `lite` drops `assets` and `spans`; anything else is `422 bad_input`. Responses carry `ETag`, `Cache-Control: no-store`, `Vary: Authorization`; a match answers `304` with no body.
- `operator_state.build(store, rundown, show_id, room, view="full") -> dict`.
- `serve.ROOM_MEMO_TTL: float` (from `VOLT_ROOM_MEMO_MS`, default 1000 ms; 0 disables), `serve._room_memo_clear()`, `serve.NOW = time.monotonic` (tests replace it). A successful POST calls `_room_memo_clear()`.

- [ ] **Step 1: Write the failing tests** in `test_state_conditional.py` (class `Conditional(Api)`, using `FakeRoom` and the `normal` seed): `test_state_has_etag_and_no_store`; `test_two_quiet_polls_give_the_same_tag`; `test_if_none_match_gets_304_with_empty_body` (also `W/"tag"`, a list, and `*`); `test_a_change_gives_a_new_tag` (hold the show, tag differs); `test_unauthenticated_request_gets_401_not_304` (token set, no header, with a matching `If-None-Match`); `test_omitted_view_is_unchanged_bytes` (compare to a stored `json.dumps` of `_operator_state`); `test_lite_drops_assets_and_spans_and_keeps_the_rest` (keys exactly `shows show rundown taking cues media room mos orderVersion`; `media[...]["blockedBy"]` present); `test_unknown_view_is_422`; `test_room_is_observed_once_per_memo_window` (a counting `FakeRoom`, `serve.NOW` fake: two polls inside 1 s count one `observe`, a poll after it counts two); `test_a_post_clears_the_memo` (hold then poll observes again).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_state_conditional -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Build the payload, serialise once, derive the tag, apply the condition after `_gate`. Memo wraps `operator_state.room_state(get_room)`; tests disable it with `serve.ROOM_MEMO_TTL = 0` in `Api.setUp` overrides where they count calls.
- [ ] **Step 4: Run** the new file, `tests.test_operator_api`, `tests.test_operator_ui`, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `State: ETag and 304, lite view, one room observation per second`.

### Task 2: Rate limits and the trusted-proxy client address

**Files:**
- Create: `control/volt/ratelimit.py`, `control/tests/test_ratelimit.py`
- Modify: `control/volt/serve.py` (`Handler._authenticate`, `do_POST`, the `/api/state` branch, `_error` log line), `docs/tls.md` (Task 9 finishes the prose)

**Interfaces:**
- `class Limiter: __init__(self, rate: int, per: float, clock=time.monotonic, max_keys: int = 10000)`; `allow(self, key: str) -> tuple[bool, float]` returns `(ok, retry_after_seconds)`; token bucket; `rate == 0` always allows; the key table is bounded (oldest evicted).
- `serve.LIMITS = {"authfail": Limiter(30, 60), "write": Limiter(120, 60), "state": Limiter(120, 60)}` built from `VOLT_RATE_AUTHFAIL`, `VOLT_RATE_WRITE`, `VOLT_RATE_STATE`.
- `serve.client_ip(peer: str, forwarded: str | None, trusted: frozenset[str]) -> str`: the peer unless it is trusted, then the right-most `forwarded` entry that is not trusted (the peer if none). `VOLT_TRUSTED_PROXIES` is a comma list, default empty.
- A limited request is `429` `{"code": "rate_limited"}` with `Retry-After` (whole seconds, at least 1), through `ApiError(429, ..., headers=...)`.

- [ ] **Step 1: Write the failing tests.** `Bucket`: `test_allows_the_rate_then_refuses_with_retry_after`, `test_refills_with_a_fake_clock`, `test_rate_zero_is_off`, `test_key_table_is_bounded`. `ClientIp`: `test_header_ignored_unless_peer_is_trusted`, `test_rightmost_untrusted_entry_wins`, `test_spoofed_leftmost_entry_is_ignored`, `test_empty_or_garbage_header_falls_back_to_the_peer`. `Http(Api)`: `test_failed_auth_is_throttled_per_client` (31 wrong tokens: the last is 429 with `Retry-After`); `test_a_valid_token_is_never_charged_to_the_authfail_bucket` (30 failures, then a valid request succeeds); `test_writes_are_limited_per_principal` (two users; one exhausts, the other still posts); `test_state_polls_limited_per_principal_not_thumbs` (thumbnail requests do not touch the `state` bucket); `test_limits_off_by_env_zero`.
- [ ] **Step 2: Run** `python -m unittest tests.test_ratelimit -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `ratelimit.py` and the three call sites; charge `authfail` only where `_authenticate` is about to raise `401` for a presented-but-invalid credential (a request with no credential is also charged to the address, since it costs the same to answer; a valid one never is).
- [ ] **Step 4: Run** the new file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Rate limits and a verified trusted-proxy client address`.

### Task 3: Viewer role, session-scope seam, remote media rule

**Files:**
- Modify: `control/volt/users.py` (`ROLES`, the `bad_role` text, the CLI `choices`), `control/volt/serve.py` (`Principal`, `do_GET` and `do_POST` gates, `_issue_media_ticket`, the thumb and proxy branches of `_route`, `_operator_state`), `control/volt/media.py`
- Create: `control/tests/test_viewer.py`

**Interfaces:**
- `users.ROLES == ("viewer", "operator", "admin")`; `Principal.scope: str = "full"`.
- `serve.VIEWER_GET = frozenset({"/api/state", "/rundown", "/api/whoami", "/api/health"})` plus the thumbnail route under the media rule. A viewer's other GETs and every POST: `403 forbidden_role`.
- `serve.MOBILE_ALLOWED = frozenset({"act_hold", "act_release", "act_skip"})`; scope `mobile` and any other action: `403 forbidden_scope`.
- `media.remote_gate(store, asset_id: str) -> None` raises `MediaError(403, "rights_blocked", ...)` when `operator_state._blocked_by(cleared, legal_hold)` is not `None`; applied to ticket issuance and the thumbnail route for a limited principal (role `viewer` or scope `mobile`). The proxy and a `kind=proxy` ticket are `403 forbidden_role` for them.
- A limited principal's `/api/state` is forced to `view=lite` with `room.artifactPath` and each `mos[].detail` blanked.

- [ ] **Step 1: Write the failing tests.** `test_viewer_can_read_state_and_rundown`; `test_viewer_post_of_every_route_is_403_forbidden_role` (loop over `serve.ROUTES`); `test_viewer_denied_audit_users_metrics_assets_mos_proxy`; `test_viewer_state_is_lite_and_paths_are_blank`; `test_viewer_thumb_of_legal_hold_is_rights_blocked` and `..._not_cleared`; `test_viewer_thumb_of_cleared_asset_is_served`; `test_viewer_ticket_for_proxy_is_refused_and_for_blocked_thumb_is_rights_blocked`; `test_operator_and_admin_behaviour_unchanged` (an operator still gets full state, a proxy and a ticket for a held asset); `test_scope_mobile_allows_only_hold_release_skip` (set `scope` by hand through a test hook on the handler); `test_scope_mobile_thumb_of_blocked_asset_refused`; `test_user_add_accepts_viewer_and_rejects_bogus_role`; keep `tests.test_users` green (`ROUTE_ROLES` values are still `{operator, admin}`).
- [ ] **Step 2: Run** `python -m unittest tests.test_viewer tests.test_users -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the gates before route lookup for POST (after the 404 for an unknown path), and the allow-list before routing for viewer GETs.
- [ ] **Step 4: Run** the two files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Viewer role, mobile scope seam, rights rule on remote media`.

### Task 4: Install shell: manifest, icons, service worker, CSP

**Files:**
- Create: `control/volt/session/operator/manifest.webmanifest`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`, `control/volt/session/sw.js`, `control/tests/test_pwa.py`
- Modify: `control/volt/serve.py` (`STATIC`, `_static` CSP string), `control/volt/session/operator/index.html` (manifest link, icon link, `theme-color` pair, `viewport-fit=cover`), `operator.js` (registration only), `control/pyproject.toml` if package data lists files

**Interfaces:**
- `STATIC` gains `/operator/manifest.webmanifest` (`application/manifest+json`), the three PNGs (`image/png`), `/operator/mobile.js` (Task 5) and `/sw.js` (`text/javascript`, file `sw.js` in `session/`).
- `sw.js` exports under node: `SHELL` (the fixed precache list), `CACHE_NAME(version)`, `shouldHandle(method: str, path: str, hasAuth: bool) -> bool`. It handles only `GET` of a `SHELL` path without `Authorization`; network first, cache on network failure; `activate` deletes caches whose name differs.
- CSP string gains `manifest-src 'self'; worker-src 'self'`.

- [ ] **Step 1: Write the failing tests** in `test_pwa.py`: `test_new_files_are_served_with_their_types` (open, no token); `test_csp_has_exactly_two_new_directives` (compare the policy with the two directives removed to the old string; no `unsafe`); `test_manifest_fields` (name, `start_url` `/operator`, `scope` `/`, `display` `standalone`, colours `#141414`, icons 192 and 512 present and each file's PNG signature and IHDR width/height match); `test_index_links_manifest_and_every_href_rule_still_holds`; `test_viewport_allows_zoom` (no `maximum-scale`, no `user-scalable`); `test_sw_never_handles_api_auth_post_or_media` (node: `/api/state`, `/rundown`, `/assets`, `/media`, `/metrics`, `/health`, `/auth/x`, any POST, any request with auth, any thumb path: `shouldHandle` false); `test_sw_handles_exactly_the_shell` (every `SHELL` path true; the list contains no `/api`); `test_sw_is_network_first` (node with fake `fetch` and `caches`: online returns the network response; offline returns the cached shell; offline for an `/api/` path is not intercepted); `test_cache_name_changes_with_version_and_activation_deletes_old`; `test_registration_is_in_operator_js_and_guarded` (feature-detected, in try/catch); `test_local_storage_use_unchanged`.
- [ ] **Step 2: Run** `python -m unittest tests.test_pwa -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Rasterise `brand/logos/mark-slashes.svg` once to the three PNGs (any tool; note the command in `docs/operator-ui.md` in Task 9); the manifest and worker are plain files.
- [ ] **Step 4: Run** the new file, `tests.test_operator_ui`, and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Installable shell: manifest, icons, network-first service worker`.

### Task 5: `mobile.js` helpers and the cheap poll

**Files:**
- Create: `control/volt/session/operator/mobile.js`, `control/tests/test_mobile_js.py`
- Modify: `operator.js` (`refresh`, `schedule`, `call` for state), `index.html` (script tag before `operator.js`)

**Interfaces** (all pure, exported under node as `module.exports`, on `window.VoltMobile` in the page):
- `formFactor(width: number, coarse: boolean) -> 'desk' | 'tablet' | 'phone'`: coarse and under 600 is phone; coarse and 600 to 1000 is tablet; else desk.
- `nextPoll(form: string, st: {unchanged: number, errors: number, hiddenSince: number|null}, ev: {kind: 'changed'|'notmodified'|'error'|'tap'|'visible', now: number, hidden: boolean, retryAfter?: number, saveData?: boolean, rand?: number}) -> {ms: number, st}`. Desk: 2000 visible, 6000 hidden, ignores everything else. Phone and tablet: 2000, after five `notmodified` widen to 3000 then 4000 (cap), reset on `changed`, `tap`, `visible`; hidden 6000 for 30 s then 30000; errors 2, 4, 8, 16, 30 s with up to 25 percent jitter from `rand`; `retryAfter` (seconds) is a floor; `saveData` makes the base 5000.
- `armStep(armed: {key: string, at: number} | null, key: string, now: number, touch: boolean) -> {armed: {key, at} | null, fire: boolean}`: on touch a second press fires only at 400 ms or more and under 4000 ms after arming; a different key re-arms; not touch behaves as the desk (second press fires).
- `freshness(ageMs: number) -> {word: string, level: 'live'|'aging'|'stale', actionsOk: boolean, showData: boolean}`: live to 6 s (`Live`), aging to 30 s (`Stale N s`), beyond 30 s no data and no actions (`No connection`).
- `diffAlerts(prev: object|null, next: object) -> Array<{kind: string, id: string, text: string}>` for `cue_failed`, `held`, `released`, `room_unreachable`, `room_back`, `recording_failed`, `item_on_air`; `prev === null` returns `[]`.
- State request: the page keeps the last `ETag` per `show|view` in memory, sends `If-None-Match`, treats `304` as `notmodified`, clears the tag on a show change, a `401` or an error, and asks for `view=lite` on tablet and phone and when `saveData`.

- [ ] **Step 1: Write the failing tests** in `test_mobile_js.py` (node, `skipUnless NODE`): `test_form_factor_table`; `test_desk_poll_is_exactly_2s_and_6s_whatever_the_events`; `test_phone_widens_after_five_304_and_caps_at_4s`; `test_change_tap_or_visibility_resets_to_2s`; `test_hidden_over_30s_goes_to_30s`; `test_error_backoff_sequence_with_jitter_bounds`; `test_retry_after_is_a_floor`; `test_save_data_base_5s`; `test_arm_second_press_under_400ms_does_not_fire`; `test_arm_second_press_after_400ms_and_under_4s_fires`; `test_arm_expires_after_4s`; `test_arm_other_key_rearms`; `test_arm_desk_behaviour_unchanged`; `test_freshness_levels_and_words`; `test_diff_alerts_each_kind_and_none_on_first_state`. Static: `test_state_request_sends_if_none_match_and_handles_304` and `test_etag_cleared_on_show_change_and_401` (source checks on `operator.js`); `test_desk_cadence_constants_unchanged` (`POLL_MS`, `POLL_HIDDEN_MS`, `ARM_MS`).
- [ ] **Step 2: Run** `python -m unittest tests.test_mobile_js -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `mobile.js`, then wire `refresh`/`schedule` to `nextPoll`, with the desk path producing the same delays as before.
- [ ] **Step 4: Run** the new file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Cheap poll: If-None-Match, lite view, back-off and the pure mobile helpers`.

### Task 6: Touch mode, phone and tablet layouts

**Files:**
- Modify: `operator.js` (`renderChrome`, `renderShow` transport and row builders, the rights table builder, `setScreen`, a new `touchMode()`), `operator.css` (tablet and phone rules, bottom bar, cards, targets, safe areas), `index.html` (tab bar landmark, More dialog, freshness pill)
- Create: `control/tests/test_touch_layout.py`

**Interfaces:**
- `touchMode() -> boolean` (coarse pointer, unless the per-tab `volt.desk` toggle in `sessionStorage` is on). In touch mode the builders do not emit TAKE (`renderShow`'s `transport`), GO, Kill, Remove, Rename, Attach, Record start or stop, Clear, Lift hold, Publish, Delete, or the `#build` and `#cue-add` forms on phone.
- Phone: `body.form-phone`, bottom `nav` (Rundown, Devices, Rights, More), on-air strip, item cards, show action bar (Hold or Release, two-press via `armStep`), item action bar (Skip two-press, Details; Approve only when `S.data.caps && S.data.caps.approve`), Devices screen from the existing room, MOS and recording data, read-only Rights cards. Tablet: `body.form-tablet`, two columns, 44 px targets, build forms shown. Hold reason on touch: `Held from a phone`.
- CSS: touch targets 48 px (phone) and 44 px (tablet) with 8 px gaps; `touch-action: manipulation` on controls; `env(safe-area-inset-*)`; landscape phone (`max-height: 500px`) swaps the bar for the 64 px rail; no animation on new states; new tokens (if any) in both theme sets.

- [ ] **Step 1: Write the failing tests.** Static source checks in `test_touch_layout.py`: `test_touch_branch_never_emits_take_go_kill_record_clear_publish_delete` (slice the builders by name; in the touch branch none of `takeNow`, `killItem`, `removeItem`, `recordStart`, `recordStop`, `legalHold`, `act('cleared'`, `publish`, `delete` is reachable); `test_desk_branch_unchanged` (the old key handler and `armedAction` bodies are byte-identical to a stored hash); `test_hold_and_skip_on_touch_go_through_armStep`; `test_phone_hold_reason_string`; `test_approve_slot_absent_without_capability`; `test_stale_state_disables_hold_and_skip_with_aria_disabled_and_a_reason`; `test_tab_bar_is_a_nav_with_aria_current`; `test_css_has_48_and_44px_targets_and_touch_action`; `test_no_style_attributes_set_by_the_script` (existing rule); `test_viewport_and_safe_area_rules_present`. Node: `test_touch_mode_decision` with fake `matchMedia` and `sessionStorage`. Contrast: extend `Tokens` so every new token is in both sets.
- [ ] **Step 2: Run** `python -m unittest tests.test_touch_layout tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Touch rendering is a branch at the top of each builder that returns the touch version; the desk code below it is not edited. The armed button keeps its box (reserve space for the longer `Confirm` label).
- [ ] **Step 4: Run** the new file, `tests.test_operator_ui` (keys, CSP, sprite, icons, contrast) and the full suite. Expected: PASS. Then run the browser check in Task 8 for this task's screens.
- [ ] **Step 5: Commit** `Phone and tablet layouts: touch mode without TAKE or Kill, two-press hold and skip`.

### Task 7: In-page alerts, freshness pill, install hint

**Files:**
- Modify: `operator.js` (`refresh` hands each state pair to `diffAlerts`, header pill, More dialog), `index.html`, `operator.css`
- Test: `control/tests/test_touch_layout.py` (class `Alerts`), `test_mobile_js.py`

**Interfaces:**
- `Turn on alerts` button in the More dialog: only a press calls `Notification.requestPermission()`; never on load. A granted permission and a hidden-but-alive page produce `registration.showNotification(text, {tag: kind + id})`; the text carries no rundown content beyond the item slug. `navigator.vibrate` only for a new alert and only when `matchMedia('(prefers-reduced-motion: reduce)')` is false. The control states that alerts work only while the page is open.
- Header pill words from `freshness`; the live region `#live` announces each alert.
- `Install` item in More, shown only when `beforeinstallprompt` fired (Chromium); otherwise a plain note that install needs HTTPS (see tls.md). No claim about iOS.

- [ ] **Step 1: Write the failing tests:** `test_permission_is_asked_only_on_a_press` (source: `requestPermission` appears once, inside a click handler); `test_vibrate_respects_reduced_motion`; `test_notification_text_has_no_rundown_content` (the builder returns slug only); `test_alert_dedupe_by_tag`; `test_pill_words_come_from_freshness`; `test_install_hint_does_not_claim_ios`; `test_alerts_note_says_page_must_be_open`.
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the two files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `In-page alerts, freshness pill, install hint`.

### Task 8: Real-browser phone check with axe-core

**Files:**
- Create: `control/tests/browser/phone_check.mjs`, `control/tests/browser/README.md` (how to install Playwright and axe-core in a scratch directory), `control/tests/test_browser_checks.py`

**Interfaces:**
- `node phone_check.mjs --base <url> [--token T] --axe <path to axe.min.js> [--out <dir>]` exits non-zero on any failure and prints one line per viewport, scenario, theme and screen. The unittest wrapper runs it only when `VOLT_BROWSER_CHECKS=1` and skips otherwise.
- Viewports with touch emulation: 320x568, 360x740, 390x844, 844x390, 768x1024, 1024x768; plus 1920x1080, 1366x768 and 768x1024 fine-pointer as the desk no-regression run.
- Per screen: axe with tags `wcag2a wcag2aa wcag21aa wcag22aa best-practice` reports zero violations and the `target-size` rule is present in the results; `scrollWidth <= innerWidth`; every interactive element is at least 44x44 (48 on phone) outside running text; in touch mode no `#take`, no Kill, no Record start; arming (tap, 100 ms, tap) does not act and (tap, 500 ms, tap) does; the worker registers on `localhost`, the shell opens with the network blocked and shows `No connection`, and the worker's cache holds no `/api/` request; screenshots at 390x844 in both themes.

- [ ] **Step 1: Write the failing wrapper test** `test_script_exists_and_lists_every_viewport_and_scenario` (static; runs always) and `test_browser_run` (skipped without the env var).
- [ ] **Step 2: Run** `python -m unittest tests.test_browser_checks -v`. Expected: the static test FAILs, the browser test skips.
- [ ] **Step 3: Implement** the script and README.
- [ ] **Step 4: Run it for real**, from `control/`, with a token so the 401 and sign-in paths are exercised:
  `python -m tests.operator_harness library --port 8791 --token $(python -c "print('t'*24)")` in one shell, then
  `VOLT_BROWSER_CHECKS=1 node tests/browser/phone_check.mjs --base http://127.0.0.1:8791 --token tttttttttttttttttttttttt --axe <scratch>/axe.min.js --out <scratch>/shots`
  for the scenarios `normal held take_fail record_producing library`, dark and light. Expected: no violations at any size, no horizontal scroll, the desk sizes unchanged.
- [ ] **Step 5: Commit** `Phone-size browser check with axe-core`.

### Task 9: Docs, tls.md, changelog

**Files:**
- Modify: `docs/specs/operator-ui.md` (phone and tablet layouts, touch mode, poll plan, lite view, the browser-check method and the measured lite-versus-full sizes), `docs/tls.md` (client address row, `VOLT_TRUSTED_PROXIES`, `304` and `ETag` pass-through, HSTS detail, hosted checklist, rate limits), `docs/brand-guide.md` (one line on the touch and stale states if new tokens were added), `docs/roadmap.md`, `CHANGELOG.md`
- Test: `control/tests/test_operator_ui.py` (docs checks live beside the code that pins the strings)

- [ ] **Step 1: Write the failing tests:** `test_tls_doc_names_trusted_proxies_and_the_peer_rule`; `test_operator_ui_doc_lists_every_new_env_var` (`VOLT_ROOM_MEMO_MS`, `VOLT_RATE_AUTHFAIL`, `VOLT_RATE_WRITE`, `VOLT_RATE_STATE`, `VOLT_TRUSTED_PROXIES`, `VOLT_BROWSER_CHECKS`); `test_docs_mark_push_ios_install_and_real_phone_as_unvalidated`; `test_changelog_mentions_viewer_role`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Write the docs.** Record the PNG rasterisation command, the measured lite and full state sizes on the `library` scenario, and what was and was not run on a real device.
- [ ] **Step 4: Run** the full suite, then the Task 8 browser run once more on the final tree. Expected: PASS.
- [ ] **Step 5: Commit** `Docs: mobile and remote access`.

## After the last task

Manual, on real hardware, written up in `docs/dress-rehearsal.md` as unvalidated until run: a phone and a tablet on the Caddy `tls internal` recipe with the root installed; install to the home screen on Android Chrome and iOS Safari; Hold and Skip with gloves and in a moving vehicle; the offline shell in airplane mode; a `viewer` token against a held asset. Each result is recorded with the device and browser version.
