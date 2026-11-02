# Volt collaboration and identity: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sign-in with Google and Microsoft (OIDC, unvalidated against a real provider), a read-only role, groups and per-show access, edit leases with presence, comments with mentions and notifications, and an admin audit view, without changing how bearer tokens work or touching the on-air path.

**Architecture:** Server-side authorization-code flow with PKCE in the stdlib `ThreadingHTTPServer` (`control/volt/serve.py`), RS256 verification in pure Python (`control/volt/oidc_jwt.py`), server-side sessions behind an HttpOnly cookie with Origin and per-session CSRF checks, new tables behind per-task migrations, new modules `access.py`, `leases.py`, `discuss.py`, `oidc.py`, and static UI additions in `control/volt/session/operator/`. Presence and leases ride the existing 2-second `/api/state` poll for reads and a 10-second POST for writes.

**Tech Stack:** Python 3.12 standard library, SQLite and Postgres through `volt.store.Store`, `unittest` in `control/tests/` (run from `control/`), node-driven UI tests in `control/tests/test_operator_ui.py` (`_node`, `FILES`, `Icons.SPRITE_IDS`), harness `control/tests/operator_harness.py`, axe-core via the routed same-origin script used in earlier slices.

**Spec:** `docs/superpowers/specs/2026-11-01-collaboration-identity-design.md`. Read it first; the owner decisions and the "Calls I made" there are inputs, not suggestions.

## Global Constraints

- Bearer tokens (legacy `VOLT_TOKEN` and `volt_user` tokens) behave byte for byte as before; every existing test passes unchanged except the ones this plan names.
- Every new POST function is in `serve.ROUTES` and in `serve.ROUTE_ROLES` (`test_users.Registry.test_every_post_route_has_an_explicit_role` compares the two sets); every new route namespace has an entry in `_AUDIT_KIND`; every 2xx write is audited, except presence and lease renew heartbeats (named exceptions, tested).
- Each migration is appended to `migrations.MIGRATIONS` with the next number at build time, uses `IF NOT EXISTS` per backend, and has a historical-shape forward test on SQLite and Postgres (`tests/historical_shapes.py`, `VOLT_TEST_PG_DSN`).
- Tests use fakes and a frozen clock; none touches the network. Real-provider behaviour is written as unvalidated everywhere it is mentioned.
- UI: static files, same-origin CSP, no inline script or style, no `http(s)://` literals in `operator.js` other than what tests already allow, forms are not used for navigation (`form-action 'none'`), state is icon plus word plus colour, red only for a live path, new colours in both theme sets and the contrast test, existing keys unchanged, new icons added to the sprite and to the sprite test.
- No secret, token, cookie, code or comment body in the audit table, the log or a URL.
- Never block take, go, hold, release, skip or kill on a collaboration table.

## Review Focus

- **Forged or replayed login:** wrong `iss`, `aud`, `nonce`, `alg` (`none`, `HS256` with the public key as secret), `kid`, stale `iat`, replayed `state`, a callback from another browser, a modulus under 2048 bits, an exponent of 3. Every one is refused and writes `login_failed` and no session.
- **Authority outliving its source:** a disabled user, a removed group member, a role change or an ended session must lose access on the very next request, for cookie and bearer alike.
- **A write that slips past a guard:** a cookie POST without Origin or CSRF header, a viewer POST to an operator route, a show-scoped route absent from `ROUTE_SHOW_LEVEL` and `NOT_SHOW_SCOPED`, an `rename` with a stale `version` or against another holder's lease.
- **Two editors, one winner:** N concurrent lease acquirers and N concurrent reorders (SQLite and Postgres); expiry on a frozen clock; takeover then the old holder's write and renew; two tabs of one user.
- **The on-air path:** with the lease, presence, comment and session tables dropped or locked, take, go, hold, release, skip and kill still work.

---

### Task 1: Viewer role, groups, effective role

**Files:**
- Create: `control/volt/access.py`
- Modify: `control/volt/migrations.py`, `control/volt/schema.sql`, `control/volt/users.py` (`ROLES`), `control/volt/serve.py` (`Principal`, `_bearer_principal`, `do_POST` role check, group routes, `ROUTES`, `ROUTE_ROLES`, `_AUDIT_KIND`), `control/volt/cli.py` (group and role subcommands)
- Test: `control/tests/test_access.py` (new); update `control/tests/test_users.py` `Registry` for the `viewer` value

**Interfaces:**
- Produces: `access.RANK = {"viewer": 0, "operator": 1, "admin": 2}`; `access.effective_role(store, user_name: str, direct_role: str) -> str`; `access.groups_of(store, user_name) -> tuple[str, ...]`; `access.create_group(store, name, role=None) -> dict`; `access.set_members(store, name, add=(), remove=(), source="manual") -> dict`; `access.delete_group(store, name)`; `access.set_user_role(store, name, role, legacy_admin=False)`; `access.AccessError(status, code, reason)`. Columns `volt_user.email`, `display_name`, `source`; tables `volt_group(name, role, created_at)`, `group_member(group_name, user_name, source)`. Routes `GET /api/groups`; `POST /api/groups`, `/api/groups/{name}/members`, `/api/groups/{name}/delete`, `/api/users/{name}/role`; `Principal.groups`, `Principal.via`. `do_POST` compares `RANK[need] > RANK[role]` (an unclassified route is still `admin`); `ROUTE_ROLES` accepts `viewer`.
- Consumes: `users.UserError` pattern; `users.disable`'s last-admin rule (`409 last_admin` also for demoting the last admin).

- [ ] **Step 1: Write the failing tests.** `test_viewer_cannot_post_operator_route` (viewer token on `act_hold` route: `403 forbidden_role`); `test_viewer_can_get_state`; `test_effective_role_is_highest_of_direct_and_group` (operator user in an admin group is admin; remove membership, next request is operator again); `test_disabled_user_loses_group_access_at_once`; `test_last_admin_cannot_be_demoted` (`409 last_admin`); `test_group_routes_are_admin_and_audited` (operator gets 403; admin gets 200 and an audit row `group_create`); `test_registry_accepts_viewer` (every value in `ROUTE_ROLES` is in `RANK`); `test_unclassified_route_stays_admin_only`; `test_migration_forward_both_databases` (historical shape with `volt_user` lacking the new columns gains them, existing tokens still authenticate).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_access tests.test_users -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration (`columns=` for `volt_user`, `CREATE TABLE IF NOT EXISTS` for both groups tables), the module, the rank check, the routes, and `volt group add|member|role` CLI commands.
- [ ] **Step 4: Run** the new tests, then the whole suite `python -m unittest discover -s tests`. Expected: PASS.
- [ ] **Step 5: Commit** `Viewer role and groups with an effective role`.

### Task 2: Audit view

**Files:**
- Modify: `control/volt/store.py` (`list_audit_page`), `control/volt/migrations.py` (indexes), `control/volt/serve.py` (`/api/audit` branch in `_route`), `control/volt/session/operator/index.html`, `operator.js`, `operator.css`
- Test: `control/tests/test_audit.py` (extend), `control/tests/test_operator_ui.py` (`ActivityScreen`)

**Interfaces:**
- Produces: `Store.list_audit_page(actor=None, action=None, kind=None, target=None, since=None, until=None, before=None, limit=100) -> tuple[list[dict], Optional[str]]` (rows plus the next cursor, ordered newest first by `(at, id)`); `audit.csv_cell(value: str) -> str` (quotes per RFC 4180 and prefixes a single quote when the first character is `=`, `+`, `-`, `@`, tab or CR); `GET /api/audit?actor=&action=&kind=&target=&from=&to=&before=&limit=&format=csv`. Without `target` the call needs admin (`403 forbidden_role`); with only `target` it behaves exactly as today. Unknown parameters stay `422 bad_input`; `limit` over 500 is `422`.
- UI: an Activity screen (admin) with a filter bar, table, "Load more", CSV link as a fetch-to-blob (a bearer cannot ride a plain link). Row action rendered as icon plus word.

- [ ] **Step 1: Write the failing tests.** `test_filter_by_actor_and_action`; `test_cursor_pages_without_gaps_or_repeats` (250 rows with identical `at`, page by 100, union equals all, no duplicates, on both databases); `test_target_only_query_unchanged_for_operator`; `test_no_target_needs_admin`; `test_csv_neutralises_formulas` (`=HYPERLINK(...)`, `+1`, `-2`, `@x` come out quoted with a leading `'`; embedded quote, comma and newline round-trip through `csv.reader`); `test_limit_and_unknown_param_refused`; `test_forward_indexes_exist`; UI: `test_activity_source_has_no_innerhtml_for_rows`, `test_activity_icons_in_sprite`.
- [ ] **Step 2: Run** `python -m unittest tests.test_audit tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the old single-target path as a thin call so nothing changes for existing clients.
- [ ] **Step 4: Run** the files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Audit view: filters, cursor paging, CSV, Activity screen`.

### Task 3: Sessions, cookie auth, CSRF

**Files:**
- Create: `control/volt/sessions.py`
- Modify: `control/volt/migrations.py`, `schema.sql`, `serve.py` (`_authenticate`, `Principal`, `do_POST` CSRF gate, whoami, routes, `_AUDIT_KIND`), `operator.js` (token dialog: start a session after a token is entered; send `X-Volt-Csrf`; keep the legacy path), `users.py` (disable ends sessions)
- Test: `control/tests/test_sessions.py` (new), `test_operator_ui.py`

**Interfaces:**
- Produces: table `session(id_hash, user_name, provider, csrf, created_at, last_seen_at, expires_at)`; `sessions.create(store, user_name, provider, now) -> tuple[str, str]` (cookie value, csrf); `sessions.resolve(store, cookie_value, now) -> Optional[SessionInfo]` (applies idle and absolute limits, touches `last_seen_at` at most once per 60 s); `sessions.end(store, id_hash)`; `sessions.end_for_user(store, name)`; `sessions.cookie_header(value, secure: bool, host_prefix: bool) -> str`; env `VOLT_PUBLIC_URL`, `VOLT_SESSION_IDLE` (default 8 h), `VOLT_SESSION_MAX` (default 14 h). Routes `POST /api/session/start` (bearer required), `POST /api/session/end`. `GET /api/whoami` adds `effectiveRole`, `groups`, `via`, `displayName`, `csrf` (session only). Cookie requests on POST require `Origin` equal to the `VOLT_PUBLIC_URL` origin (`403 bad_origin`) and `X-Volt-Csrf` equal to the session value (`403 bad_csrf`). A bearer header wins over a cookie.
- Consumes: Task 1 `effective_role`.

- [ ] **Step 1: Write the failing tests.** `test_cookie_attributes` (HttpOnly, SameSite=Lax, Secure and `__Host-` name only under an https public URL, no Domain); `test_new_session_id_each_login_and_client_id_ignored`; `test_cookie_value_stored_only_hashed`; `test_cookie_post_without_origin_is_refused`; `test_cookie_post_with_foreign_origin_is_refused`; `test_cookie_post_without_csrf_header_is_refused`; `test_bearer_post_needs_no_origin_or_csrf`; `test_json_content_type_still_required`; `test_idle_and_absolute_expiry_on_frozen_clock`; `test_disable_user_kills_session_next_request`; `test_role_change_applies_without_new_login`; `test_last_seen_write_is_throttled` (60 requests in 60 s produce one write); `test_session_end_clears_cookie`; `test_whoami_returns_csrf_only_for_session`.
- [ ] **Step 2: Run** `python -m unittest tests.test_sessions -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Inject `now` everywhere; no `time.time()` inside the module body.
- [ ] **Step 4: Run** the file, the UI tests, the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Server-side sessions with CSRF checks for cookie requests`.

### Task 4: JWT verifier and the fake provider

**Files:**
- Create: `control/volt/oidc_jwt.py`, `control/tests/oidc_fake.py`
- Test: `control/tests/test_oidc_jwt.py` (new)

**Interfaces:**
- Produces: `oidc_jwt.verify_id_token(token: str, jwks: dict, *, issuer: str, audience: str, nonce: str, now: float, leeway: int = 120, max_age: int = 600) -> dict` returning claims or raising `oidc_jwt.TokenError(code)` with codes `malformed`, `alg_unsupported`, `unknown_kid`, `bad_signature`, `bad_key`, `bad_claim:<name>`; `oidc_jwt.rsa_verify_rs256(n: int, e: int, signing_input: bytes, signature: bytes) -> bool` (expected `EMSA-PKCS1-v1_5` bytes built from the SHA-256 digest, compared whole with `hmac.compare_digest`); `oidc_jwt.JwksCache(fetch, now, floor=300, ceiling=86400, refetch_gap=60)` with `.keys(provider, kid)`. In `oidc_fake.py`: `FakeProvider` (discovery, JWKS, token endpoint as an object with `get_json(url)` and `post_form(url, data)`), a committed throwaway 2048-bit test key labelled test-only, an independent `sign_rs256` helper, and misbehaviour switches (`bad_nonce`, `bad_aud`, `bad_iss`, `expired`, `old_iat`, `alg`, `kid`, `rotate_keys`, `truncate_sig`, `forge_e3`, `small_key`).
- Consumes: nothing from earlier tasks (pure functions).

- [ ] **Step 1: Write the failing tests.** `test_valid_token_verifies`; `test_alg_none_refused`; `test_hs256_signed_with_public_key_refused`; `test_es256_refused_alg_unsupported`; `test_wrong_issuer_audience_nonce_each_refused` (one assertion per claim, code names the claim); `test_expired_and_far_future_and_stale_iat`; `test_skew_leeway_boundary` (accepted at +119 s, refused at +121 s on the injected clock); `test_azp_required_when_aud_is_a_list`; `test_unknown_kid_one_refetch_then_refused` (second forged `kid` within 60 s does not fetch again); `test_rotated_keys_picked_up`; `test_truncated_signature_and_wrong_length_refused`; `test_bleichenbacher_style_forgery_refused` (e=3 key and crafted padding); `test_small_modulus_and_small_exponent_refused`; `test_openssl_cross_check` (skipped when `openssl` is absent: a signature made by `openssl dgst -sha256 -sign` verifies); `test_jwks_stale_while_error_up_to_24h`.
- [ ] **Step 2: Run** `python -m unittest tests.test_oidc_jwt -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `RS256 ID-token verifier with JWKS cache and a fake provider`.

### Task 5: The OIDC flow and identity mapping

**Files:**
- Create: `control/volt/oidc.py`
- Modify: `control/volt/migrations.py`, `schema.sql`, `serve.py` (`do_GET` routes for `/auth/*` before `_gate_or_ticket`, `STATIC` untouched), `access.py` (claim map), `operator.js` and `index.html` (provider anchors in the Token dialog), `docs/specs/operator-api.md`, `docs/operations.md` (config, `VOLT_PUBLIC_URL`, unvalidated note)
- Test: `control/tests/test_oidc_flow.py` (new)

**Interfaces:**
- Produces: tables `auth_flow(state, nonce, code_verifier, provider, next, binder_hash, created_at, expires_at)`, `identity(issuer, sub, user_name, email, display_name, last_login_at)`, `oidc_group_map(provider, claim_value, group_name)`; `oidc.load_config(path, env) -> OidcConfig | None`; `oidc.safe_next(value: str) -> str`; `oidc.begin(store, cfg, provider_id, next_value, now, http) -> tuple[str, str]` (authorization URL, flow cookie value); `oidc.complete(store, cfg, query: dict, flow_cookie: str, now, http) -> LoginResult` (user name, next path) or raises `oidc.LoginError(code)` with `flow_invalid`, `oidc_unavailable`, `oidc_token_invalid`, `oidc_alg_unsupported`, `not_provisioned`, `user_disabled`; `oidc.map_user(store, provider, claims) -> str`. Routes `GET /auth/providers`, `GET /auth/login`, `GET /auth/callback` (`404` when OIDC is off). `POST /api/groups/{name}/claims`, `POST /api/users/{name}/identity` (admin). Callback responses set `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Consumes: Task 3 sessions, Task 4 verifier and fake, Task 1 groups.

- [ ] **Step 1: Write the failing tests.** `test_login_redirect_has_pkce_state_nonce_and_config_redirect_uri` (S256 challenge matches verifier; redirect URI equals `VOLT_PUBLIC_URL` plus path even with a hostile `Host`); `test_happy_path_creates_session_and_audits_login`; `test_callback_replay_refused` (second use of the same `state`); `test_callback_from_other_browser_refused` (missing or wrong `volt_flow`); `test_expired_flow_refused`; `test_flow_table_is_capped_and_pruned`; `test_open_redirect_inputs_become_root` (`//evil.com`, `/\evil`, `https://evil`, `javascript:x`, `/a\r\nSet-Cookie:`, 600 characters); `test_provider_error_param_not_reflected`; `test_every_fake_misbehaviour_refuses_and_audits_login_failed`; `test_unknown_identity_links_by_verified_email_only` (Google `email_verified: false` refused; Microsoft outside the tenant list refused); `test_nOAuth_style_changed_email_does_not_relink` (a known `(iss, sub)` with a new email logs in as the same user); `test_no_user_refused_unless_auto_provision` and `test_auto_provision_name_is_sanitised_and_not_desk`; `test_disabled_user_refused`; `test_oidc_groups_replace_only_oidc_sourced_membership`; `test_microsoft_group_overage_leaves_groups_and_warns`; `test_provider_down_cached_keys_still_verify_unknown_kid_refused`; `test_client_secret_never_in_log_or_audit` (capture log and table); `test_oidc_off_gives_404`.
- [ ] **Step 2: Run** `python -m unittest tests.test_oidc_flow -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with the HTTP seam injected (module-level `HTTP` in `oidc.py`, set by tests, defaulting to a `urllib.request` client with 10 s timeouts and no redirect following). Mark the docs "unvalidated against a real provider".
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `OIDC sign-in with Google and Microsoft (unvalidated against a real provider)`.

### Task 6: Per-show access

**Files:**
- Modify: `control/volt/access.py`, `control/volt/migrations.py`, `schema.sql`, `serve.py` (`ROUTE_SHOW_LEVEL`, `NOT_SHOW_SCOPED`, a resolver per route, `do_POST` check after the role check, filtering in `_operator_state` and `/rundown`), `control/volt/operator_state.py` (`shows` takes a visibility predicate), `operator.js` (access panel under users admin; `forbidden_show` message)
- Test: `control/tests/test_show_access.py` (new)

**Interfaces:**
- Produces: table `show_acl(show_id, subject_kind, subject, level)`; `access.LEVELS = {"view": 0, "edit": 1, "run": 2}`; `access.show_level(store, principal, show_id) -> Optional[str]` (admin and legacy `desk` return `run`; a show with no ACL rows returns the level the global role implies: viewer `view`, operator `run`); `access.set_acl(store, show_id, subject_kind, subject, level_or_none)`; `access.show_of(store, kind, id) -> Optional[str]` for `item`, `cue`, `show`, `comment`. Routes `GET /api/shows/{show}/access` (operator), `POST /api/shows/{show}/access` (admin). Refusal `403 forbidden_show` whose reason names the needed level and lists no other subjects.
- Consumes: Tasks 1 and 3.

- [ ] **Step 1: Write the failing tests.** `test_open_show_unchanged` (no rows: operator still runs it); `test_first_row_hides_show_from_others` (state and rundown omit it; a direct route call is `403 forbidden_show`, not `404`, for a caller who may know it exists, and `404` for a show that does not exist); `test_levels_gate_routes` (view cannot add an item; edit cannot take; run can); `test_global_role_caps_acl` (viewer with `run` still `403 forbidden_role`); `test_admin_and_desk_bypass`; `test_group_and_user_subjects`; `test_every_post_route_is_scoped_or_exempt` (each function in `serve.ROUTES` is in exactly one of `ROUTE_SHOW_LEVEL` or `NOT_SHOW_SCOPED`; each `ROUTE_SHOW_LEVEL` function has a resolver); `test_clock_and_runner_unaffected` (a clock-fired item on an ACL-restricted show still fires); `test_acl_set_is_admin_and_audited`.
- [ ] **Step 2: Run** `python -m unittest tests.test_show_access -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Resolve the show before opening the handler's work, never after.
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Per-show access levels for groups and users`.

### Task 7: Item leases, versioned rename, presence

**Files:**
- Create: `control/volt/leases.py`
- Modify: `control/volt/migrations.py`, `schema.sql`, `control/volt/building.py` (`rename_item` accepts `version` and `client_id`; `item_version`), `serve.py` (routes, `_operator_state` adds `presence`, `leases`, `ROUTE_SHOW_LEVEL` entries), `operator.js`, `index.html`, `operator.css`
- Test: `control/tests/test_leases.py` (new); extend `test_show_building.py`

**Interfaces:**
- Produces: tables `lease(resource_kind, resource_id, holder, client_id, acquired_at, renewed_at, expires_at)`, `presence(user_name, client_id, show_id, view, seen_at)`; `leases.TTL = 45`; `leases.acquire(store, kind, rid, holder, client_id, now) -> Lease` (one upsert guarded by expiry or same holder and client; raises `LeaseError(409, "leased", ...)` with `holder`, `expiresAt`); `leases.release(store, kind, rid, holder, client_id)`; `leases.takeover(store, kind, rid, holder, client_id, now) -> Lease` (also writes a `lease_taken` notification row when Task 8 exists, otherwise a no-op hook); `leases.live(store, show_id, now) -> list`; `leases.beat(store, user, client_id, show_id, view, now)`; `leases.who(store, show_id, now) -> list`; `building.item_version(store, item_id) -> str` (sha256 of slug and script, 16 hex). Routes `POST /api/items/{item}/lease`, `/lease/release`, `/lease/takeover`, `POST /api/presence` (not audited); `rename` takes `version?`, `clientId?` and returns `409 stale_item` (with the current slug and script) or `409 leased`. UI: STATE kinds `editing` and `yours`; row chip; two-press Take over; lost-lease banner with "Copy my text"; presence strip with a text list.
- Consumes: Tasks 3 and 6.

- [ ] **Step 1: Write the failing tests.** `test_free_item_acquires_and_renew_extends`; `test_expiry_on_frozen_clock_frees_it`; `test_other_holder_gets_409_leased_with_holder`; `test_same_user_second_client_conflicts`; `test_n_acquirers_one_winner` (8 threads, both databases, exactly one 200); `test_takeover_then_old_holder_renew_is_lease_lost` and `test_takeover_audited`; `test_rename_with_stale_version_is_409_and_changes_nothing`; `test_rename_against_other_holders_live_lease_is_409`; `test_rename_without_lease_or_version_behaves_as_before`; `test_non_ready_item_cannot_be_leased` (`not_editable`); `test_take_is_never_blocked_by_a_lease` and `test_on_air_routes_work_with_lease_and_presence_tables_dropped`; `test_presence_expires_after_30s_and_rows_pruned_after_5min`; `test_state_lists_presence_and_leases_filtered_by_show_view`; `test_heartbeats_not_audited_but_acquire_takeover_release_are`; UI: `test_lease_states_in_state_map_and_sprite`, `test_presence_strip_has_text_list`, `test_takeover_is_two_press`.
- [ ] **Step 2: Run** `python -m unittest tests.test_leases tests.test_show_building tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The renew timer and heartbeat live in `operator.js` with `try/catch` around any storage; the textarea keeps the user's draft on a lost lease.
- [ ] **Step 4: Run** the files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Edit leases, versioned rename and presence`.

### Task 8: Comments, mentions, notifications

**Files:**
- Create: `control/volt/discuss.py`
- Modify: `control/volt/migrations.py`, `schema.sql`, `serve.py` (routes, `/api/state` unread count, `_AUDIT_KIND`), `control/volt/retention.py` or the runner's retention pass (purge notifications older than 90 days), `operator.js`, `index.html`, `operator.css`
- Test: `control/tests/test_discuss.py` (new), `test_operator_ui.py`

**Interfaces:**
- Produces: tables `comment`, `comment_mention`, `notification` (spec columns); `discuss.add_comment(store, author, kind, target, body, parent=None, now) -> dict` (1 to 4000 characters, one reply level, 30 per minute per author else `429 rate_limited`); `discuss.parse_mentions(store, body, show_id) -> list[str]` (enabled users with view access only); `discuss.resolve(store, id, by, resolved)`; `discuss.delete(store, id, by, is_admin)`; `discuss.notify(store, user, kind, ...)`; `discuss.list_comments(store, kind, target, viewer)`; `discuss.notifications(store, user, unread_only)`; `discuss.mark_read(store, user, ids=None)`. Routes `GET /api/comments`, `POST /api/comments`, `/api/comments/{id}/resolve`, `/api/comments/{id}/delete`, `GET /api/notifications`, `POST /api/notifications/read`. UI: thread panel with textContent rendering, mention autocomplete (listbox, keyboard operable), count chip on item rows, header bell. Sprite gains `comment`, `bell`, `edit`, `history`.
- Consumes: Tasks 3, 6, 7 (`lease_taken` notification).

- [ ] **Step 1: Write the failing tests.** `test_comment_requires_view_access_and_is_audited_without_body`; `test_mention_notifies_enabled_user_with_access_only` (a mention of a user without access or a nonexistent name is plain text and creates no notification); `test_reply_notifies_thread_participants_not_author`; `test_second_level_reply_refused`; `test_resolve_needs_edit_and_delete_needs_author_or_admin`; `test_deleted_comment_body_hidden_in_list`; `test_rate_limit_429`; `test_notifications_are_private_to_owner`; `test_mark_read_only_own_ids`; `test_unread_count_in_state`; `test_ui_renders_body_with_textcontent` (source of the comment renderer contains no `innerHTML`); `test_mention_markup_from_server_list_not_from_body`; `test_forward_migration_both_databases`; `test_old_notifications_purged_after_90_days`.
- [ ] **Step 2: Run** `python -m unittest tests.test_discuss tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the files and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Comments with mentions and in-app notifications`.

### Task 9: Browser check, accessibility, docs

**Files:**
- Modify: `control/tests/operator_harness.py` (scenarios `signed_out_with_providers`, `lease_held_by_other`, `lease_lost`, `presence_two_users`, `comments_thread`, `activity_filtered`), `control/volt/session/operator/operator.css` (both theme sets), `docs/specs/operator-api.md`, `docs/specs/operator-ui.md`, `docs/architecture.md`, `docs/operations.md`, `docs/roadmap.md`, `docs/brand-guide.md` (only if a new colour token is added and measured), `CHANGELOG.md`; create `docs/specs/collaboration.md` summarising what is built and what is unvalidated
- Test: `control/tests/test_operator_ui.py` (contrast cases for the new states in both themes)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests.** Contrast test cases for `editing`, `yours`, the lost-lease banner and the notification count in both theme sets (text 4.5:1, icons 3:1); `test_new_post_routes_documented` (every route in `ROUTES` appears in `operator-api.md`).
- [ ] **Step 2: Run** the UI test file. Expected: FAIL.
- [ ] **Step 3: Implement** the tokens and docs. Run each harness scenario in a browser with the cache disabled, screenshot dark and light, run axe-core in each scenario (reload per scenario), and check the page with two browser profiles against one server for a real lease conflict.
- [ ] **Step 4: Run** `python -m unittest discover -s tests` on SQLite, and again with `VOLT_TEST_PG_DSN` set. Expected: PASS, with Postgres-only tests skipped when unset.
- [ ] **Step 5: Commit** `Collaboration and identity: screens, contrast, docs`.

## Open for the owner

Auto-provisioning of unknown sign-ins (default: refuse), and single Microsoft tenant per customer versus `common`. See the spec's Open questions.
