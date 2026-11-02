# Volt: scale and operations, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several tenants in one hosted deployment, two or more `serve` processes behind a balancer, request and job metrics with dashboards and alerts as files, rate limits, storage locations with site sync, rolling upgrades, per-tenant backups with a restore drill, and an admin status view. Installed single-tenant behaviour does not change.

**Architecture:** Python standard library control plane in `control/volt/`, static operator UI in `control/volt/session/operator/`. Tenancy is schema-per-tenant on Postgres with a `volt_control` registry; the request resolves the tenant from `Host` before auth. `_SEQ` is retired for go/skip/kill (the show advisory lock already decides them); `_REC` becomes a lease row.

**Tech Stack:** `unittest` in `control/tests/`; shared race helpers `tests/racekit.py` (`SqliteBase`, `PgBase`, `Bg`), live Postgres via `VOLT_TEST_PG_DSN` (`tests/pgutil.py`, one throwaway schema per test); UI tests in `tests/test_operator_ui.py` with the harness in `tests/operator_harness.py`.

**Spec:** `docs/superpowers/specs/2026-11-01-scale-operations-design.md`. Read its "What the code says today" table first; every task cites lines from it, and the builder re-greps before relying on a line number.

## Global Constraints

- Tests use fakes and never the network. Subprocess `serve` tests bind loopback port 0 only. Anything needing a cluster, a cloud account or a second site is written as **unvalidated** and listed under Rehearsals in the spec; do not word it as proven.
- Every new POST is classified in `ROUTE_ROLES` and audited; `tests/test_users.py` already fails on an unclassified route. Reads of admin data use the existing `_require_admin`.
- Schema changes are numbered migrations (take the next free number in `MIGRATIONS` at build time; never edit a shipped entry), added to `store.SCHEMA` and `schema.sql` in the same column order, with a historical-shape forward test on SQLite and Postgres. The control list is Postgres-only and has its own forward test.
- Tenancy off must behave exactly as today. Every task's tests include the tenancy-off path.
- Nothing on the poll path (`GET /api/state`) writes to the database. No new runtime dependency.
- UI: static files, same-origin CSP, no `http(s)://`, `style=` or inline script; status is icon plus word plus colour; new colour states go into every theme set that exists and the contrast test; existing keys unchanged; axe-core clean.
- Payloads are camelCase; errors are `{ok:false, error:{code, reason}}`; metrics are prefixed `volt_`.
- Each task is its own commit and leaves `cd control && python -m unittest discover -s tests` green on SQLite, and green on Postgres when `VOLT_TEST_PG_DSN` is set. Docs named in a task are updated in that task. Do not start Task N+1 on a red suite.

## Review Focus

- **Tenant bleed.** A connection, cache, thumbnail path, object key, ticket, log line, metric label, lock name or error body that carries tenant A's data into tenant B's request. Hunt for module globals and for any `Store(` outside the context opener.
- **Lost or doubled work on a stop.** A node killed mid-take, a runner killed mid-job, a clock killed after claim, a lease that expires while its holder is alive: nothing fires twice, nothing is dropped, the show lock is released.
- **Show control shed by a limit.** `go`, `take`, `hold`, `release`, `kill`, `skip` rejected by the rate limiter, the concurrency gate, a quota or a suspended-tenant check that should have exempted them.
- **Rolling-upgrade skew.** An old node against an additive newer schema, a new node against an old one, a restart in the middle, a breaking migration slipped into a rolling plan, the bridge release.
- **Unvalidated claims worded as validated.** HA, failover, site sync, restore time, alerts firing, SLO met, YAML valid: none is proven by fakes; the docs and test names must not say so.

---

### Task 1: Retire `_SEQ`, make `_REC` a lease, prove two `serve` processes

**Files:**
- Create: `control/volt/sequencing.py`, `control/tests/test_sequencing.py`, `control/tests/test_multi_serve.py`
- Modify: `control/volt/serve.py` (`_SEQ` at `:83`, `act_go`, `act_skip`, `act_kill`, `_REC`, `act_record_start`, `act_record_stop`, comments at `:403 :449 :467 :741`), `control/volt/migrations.py`, `control/volt/store.py` (SCHEMA), `control/volt/schema.sql`, `docs/operations.md`, `docs/architecture.md`, `docs/specs/operator-api.md`, `docs/specs/foundation.md`, `docs/specs/shot-spans.md`, `docs/plan-foundation.md`

**Interfaces:**
- Produces: `class LeaseBusy(Exception)` with `.name`, `.holder`; `@contextmanager def exclusive(store, name: str, ttl: float = 120.0, holder: str | None = None, now: Callable[[], float] = time.time)` acquiring a row in a new `lease(name PK, holder, expires_at)` table by one conditional upsert, releasing in `finally`, raising `LeaseBusy`; `def record_lease_name(room_key: str) -> str` returning `f"record:{room_key}"`. New migration: `lease` table (base-table creation makes it; the entry records it). Error mapping: `LeaseBusy` in record start/stop -> `ApiError(409, "record_busy", ...)`.
- Removes: `serve._SEQ` and its three `with` blocks.

- [ ] **Step 1: Write the failing tests.** `test_sequencing`: `test_second_holder_is_refused_while_lease_is_live` (assert `LeaseBusy`, holder named); `test_lease_expires_and_can_be_taken` (frozen `now`); `test_release_on_exception`; `test_same_holder_may_renew`; each runs on SQLite and, with a DSN, Postgres (`SqliteBase`/`PgBase`). `test_multi_serve`: `test_go_skip_kill_outcomes_identical_without_seq` (patch `serve._SEQ` out; N threads hit `go`/`skip`/`kill` on two connections as in `test_race_kill.py`; the set of final item statuses equals the set with the lock; no item aired twice); `test_two_serve_subprocesses_one_take` (spawn two `python -m volt.serve --port 0` against one SQLite file, fake room env, post `take` to both at once: exactly one 200, the other 409 `taking`); same on Postgres when a DSN is set; `test_record_start_second_caller_gets_record_busy`; `test_serve_has_no_seq` (`not hasattr(serve, "_SEQ")` and no `with _SEQ` in the source).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_sequencing tests.test_multi_serve -v`. Expected: FAIL (no module, `_SEQ` still present).
- [ ] **Step 3: Implement** the module and migration, replace `_REC` with `exclusive(store, record_lease_name(room_key))`, delete `_SEQ` after the equivalence test is green with the lock patched out, reword the four comments, and rewrite the "single server process only" sentences in the docs listed (leave `product-readiness.md`, a dated snapshot).
- [ ] **Step 4: Run** the new tests, then `tests.test_race_kill tests.test_race_publish tests.test_take tests.test_users` and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Retire _SEQ, lease for record, two-serve tests`.

### Task 2: Request economy and the load harness

**Files:**
- Create: `control/bench/load_state.py`, `control/tests/test_request_economy.py`
- Modify: `control/volt/serve.py` (`Handler._open_store`, `_bearer_principal`, `_auth_configured`, `do_GET`, `do_POST`, `_operator_state`, `Handler.protocol_version`, `ThreadingHTTPServer` subclass), `control/volt/operator_state.py` (`room_state`), `control/volt/users.py` (accept an open store)

**Interfaces:**
- Produces: `Handler._store()` returning one memoised `Store` per request, closed in `finally`; `class RoomObservationCache: __init__(self, ttl: float = 1.0, now=time.monotonic)`, `get(key: str, factory) -> dict` (adds `observedAgeMs`); `class VoltServer(ThreadingHTTPServer): request_queue_size = 128`; `Handler.protocol_version = "HTTP/1.1"`. `load_state.py` CLI: `python -m bench.load_state --url ... --desks 50 --seconds 600 --assets 10000 [--keepalive/--no-keepalive] --json`.

- [ ] **Step 1: Write the failing tests.** `test_one_store_per_request` (count `Store(` constructions across a bearer-authenticated GET and POST: exactly 1); `test_room_observed_once_per_second_for_50_polls` (fake clock and fake room counting `observe`: 50 polls in the same second -> 1 observe; after 1.1 s -> 2; error from the room is cached for the same ttl and still reported as `reachable: false`); `test_state_payload_unchanged_except_observed_age` (golden compare against `test_operator_api` payload shape); `test_keepalive_two_requests_one_connection` (`http.client`, `Connection: keep-alive`, two GETs, same socket); `test_every_response_has_content_length` (walk static, JSON, error, Range media, 401, 413 paths); `test_load_script_runs_against_in_process_server` (5 desks, 2 s, JSON has `p95Ms`, `rps`, `sqlStatementsPerRequest`).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_request_economy -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Count SQL statements through a wrapper used only by the bench. Keep the asset list uncapped (a contract change for the Library is out of scope).
- [ ] **Step 4: Run** the file, `tests.test_operator_api`, `tests.test_media`, `tests.test_session`, and the full suite. Expected: PASS.
- [ ] **Step 5: Run** the harness once at 100, 10,000 and 100,000 assets on this box and record the numbers (box described) in `docs/operations.md` under "Measured on" - labelled as one machine's result. Commit `Request economy: one store, cached observe, keep-alive, load harness`.

### Task 3: Request, pool and job metrics; dashboard and alert files

**Files:**
- Create: `control/volt/telemetry.py`, `control/tests/test_telemetry.py`, `deploy/monitoring/volt-dashboard.json`, `deploy/monitoring/volt-alerts.yml`, `deploy/monitoring/scrape.example.yml`
- Modify: `control/volt/serve.py` (timing around `do_GET`/`do_POST`, `/metrics` path), `control/volt/health.py` (`prometheus` appends the registry), `control/volt/runner.py` (job timing, claim outcome), `control/volt/pg.py` (`Pool.stats` use), `docs/operations.md` (Health section)

**Interfaces:**
- Produces: `class Registry` (thread-safe) with `counter(name, labels)`, `histogram(name, labels, buckets=DEFAULT_BUCKETS)`, `gauge_fn(name, fn)`; `REGISTRY` module singleton; `def route_class(method: str, fn_name: str | None, path: str) -> str` returning one of `state read write heavy health auth static media` (a fixed set); `def render(registry) -> str` (Prometheus text, cumulative `le` buckets, `_sum`, `_count`); metrics listed in spec section 5 (the request, pool, take, job, claim, backup-age, drill-age and node series).

- [ ] **Step 1: Write the failing tests.** `test_histogram_buckets_are_cumulative_and_sum_matches`; `test_text_format_parses` (regex per line; every `# TYPE` precedes samples); `test_route_class_is_a_closed_set` (every `ROUTES` function name and the GET paths map into the set; none returns a path); `test_request_counter_and_latency_recorded` (one GET, one POST, one 429); `test_no_unbounded_labels` (post 1,000 distinct fake paths -> series count unchanged); `test_dashboard_json_parses_and_every_expr_metric_is_emitted` (parse the JSON; extract metric names from each `expr`; compare with the union of names in a fake full report through `health.prometheus` plus `telemetry.render`); `test_alert_rules_reference_emitted_metrics` (text scan of `expr:` lines in the YAML); `test_existing_fourteen_series_unchanged` (names and label sets as in `health.prometheus` today).
- [ ] **Step 2: Run** `cd control && python -m unittest tests.test_telemetry -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the registry and wiring, then write the dashboard, the alerts (pages: down, hold/take failures, no runner, schema mismatch over 30 m, disk, backup age over 26 h; tickets: queue age, stuck jobs, drill over 35 d, pool waiting, rate-limit spikes) and the scrape example. Name the files **unvalidated** in a header comment: no Prometheus or Grafana was run.
- [ ] **Step 4: Run** the file and the full suite (`tests.test_ops_health` must be unchanged and green). Expected: PASS.
- [ ] **Step 5: Commit** `Metrics: requests, pool, jobs; dashboard and alert files`.

### Task 4: Tenancy core: control schema, registry, `search_path` isolation

**Files:**
- Create: `control/volt/tenancy.py`, `control/tests/test_tenancy.py`
- Modify: `control/volt/pg.py` (`PgConnection(dsn, schema=None)`, lock-name prefixing in `transaction`, reset on return), `control/volt/store.py` (`Store(path, init=True, schema=None)`), `control/volt/migrations.py` (`run_all_tenants`), `control/volt/cli.py` (`volt tenant ...`), `docs/architecture.md`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class TenantRef: slug: str; dsn: str; schema: str`; `SLUG = re.compile(r"^[a-z][a-z0-9-]{1,38}$")`; `CONTROL_MIGRATIONS` (tables in spec section 12, numbered 1.., own `control_schema_version`); `class Registry: __init__(self, control_dsn)`, `create(slug, domain, dsn=None) -> TenantRef` (creates the schema, runs `MIGRATIONS` in it under the existing advisory lock), `adopt(slug, schema, domain)`, `get(slug)`, `by_host(host) -> TenantRef | None`, `active() -> list[TenantRef]`, `suspend(slug)`, `resume(slug)`, `set_limit(slug, name, value)`, `epoch() -> int`. CLI: `volt tenant create|adopt|list|suspend|resume|limit|migrate`. Every platform action appends to `control_audit`.

- [ ] **Step 1: Write the failing tests** (Postgres, skipped without a DSN; `PgCase`). `test_connection_for_b_never_sees_a_tables` (A has a marker row; a pooled connection used by A then B: B's `SELECT` of the marker table errors, `search_path` holds only B); `test_search_path_reset_when_returned_to_pool`; `test_lock_names_are_schema_scoped` (two tenants, same show id, concurrent `transaction(show_lock(id))` do not serialise); `test_create_runs_all_migrations_in_new_schema`; `test_adopt_public_moves_no_data` (row counts equal before and after adoption); `test_control_migrations_forward_from_empty`; `test_slug_rules_and_reserved_names`; `test_suspend_bumps_epoch`; `test_platform_actions_audited`; `test_run_all_tenants_is_idempotent_and_reports_per_tenant`. Tenancy-off: `test_store_without_schema_is_unchanged` (SQLite and Postgres).
- [ ] **Step 2: Run** `VOLT_TEST_PG_DSN=... python -m unittest tests.test_tenancy -v` (without a DSN: skipped, and the SQLite-only test passes). Expected: FAIL.
- [ ] **Step 3: Implement.** The pool stays keyed by cluster DSN. Failing to reset a connection closes it instead of returning it.
- [ ] **Step 4: Run** `tests.test_tenancy tests.test_postgres tests.test_postgres_hardening tests.test_migrations` and the full suite on both backends. Expected: PASS.
- [ ] **Step 5: Commit** `Tenancy: control schema, registry, schema-per-tenant connections`.

### Task 5: Tenancy on the request path

**Files:**
- Create: `control/tests/test_tenant_http.py`
- Modify: `control/volt/serve.py` (`TenantContext`, `resolve_tenant`, every `Store(` call, `OUT_DIR`, `get_room`, media key, `check_bind`/`main` refusal of `VOLT_TOKEN`), `control/volt/tenancy.py` (`TenantContext`, `TenantSettings`), `control/volt/tickets.py` (unchanged; key derivation lives in `tenancy.py`), `control/volt/health.py` (tenant-scoped report), `docs/operations.md`, `docs/tls.md` (host-based tenants)

**Interfaces:**
- Produces: `class TenantSettings(out_dir, proxy_dir, publish_dir, quarantine_dir, storage_prefix, media_key, room_url, room_token)` from `tenant_setting` plus env; `class TenantContext(ref, settings, limits)` with `open_store() -> Store`; `def derive_media_key(master: str, slug: str) -> str` (HMAC); `Handler._tenant` set before `_gate`; `ApiError(403, "tenant_suspended", ...)`; unknown host -> the generic 404. `VOLT_TENANCY=on` with `VOLT_TOKEN` set exits 78.

- [ ] **Step 1: Write the failing tests** (two tenants on Postgres schemas, in-process servers with host headers, fake room): `test_token_from_a_on_b_host_is_401`; `test_unknown_host_is_plain_404_without_tenant_hint`; `test_suspended_tenant_is_403_but_health_and_ready_answer`; `test_media_ticket_from_a_does_not_verify_on_b_same_asset_id`; `test_thumbnail_and_publish_paths_are_under_tenant_slug`; `test_every_get_route_returns_only_own_rows` (seed distinct marker rows in A and B; call every GET path in a fixed list including `/assets`, `/rundown`, `/api/state`, `/api/audit`, `/api/health`; no response from B contains A's markers; the list is checked against the `_route` source so a new GET path fails the test until added); `test_no_store_construction_outside_the_opener` (AST scan of `serve.py` and `operator_state.py`); `test_error_bodies_never_name_another_tenant`; `test_legacy_token_refused_when_tenancy_on`; `test_health_and_metrics_are_tenant_scoped`; `test_tenancy_off_requests_unchanged` (existing `test_operator_api` suite passes unmodified).
- [ ] **Step 2: Run** `VOLT_TEST_PG_DSN=... python -m unittest tests.test_tenant_http -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Registry cache TTL 60 s with the epoch re-read every 5 s. Move the per-tenant module globals into `TenantSettings`; keep module defaults only for tenancy-off.
- [ ] **Step 4: Run** the new file, `tests.test_operator_api tests.test_session tests.test_users tests.test_media`, then the full suite on both backends. Expected: PASS.
- [ ] **Step 5: Commit** `Tenancy: host-resolved tenant context through the server`.

### Task 6: Tenant-aware runner and clock, per-tenant backup, restore drill

**Files:**
- Create: `control/tests/test_tenant_workers.py`, `control/tests/test_drill.py`
- Modify: `control/volt/runner.py` (`--tenants`, `fair_order`, per-tenant running cap), `control/volt/clock.py` (`--tenants`), `control/volt/backup.py` (`tenant`/`schema` in manifest, `pg_dump --schema`, `restore(..., into_tenant=)`, `drill`), `control/volt/cli.py` (`volt backup --tenant`, `volt drill restore --tenant`), `deploy/systemd/volt-backup.service`, `deploy/systemd/volt-backup.timer`, `deploy/windows/install-backup-task.ps1`, `docs/specs/backup.md`, `docs/operations.md`

**Interfaces:**
- Produces: `def fair_order(tenants: Sequence[TenantRef], last_served: dict[str, float]) -> list[TenantRef]` (least recently served first); `Runner.run_cycle_tenants(registry)` claiming at most one job per tenant per pass and never above `max_running_jobs`; `backup.backup(source, dest, tenant: str | None = None)`; `backup.drill_restore(source, backup_dir, tenant, now, clock) -> DrillResult(ok, seconds_by_phase, row_counts, mismatches)` writing `drill.json`; manifest keys `tenant`, `schema`.

- [ ] **Step 1: Write the failing tests.** `test_fair_order_least_recent_first_and_stable`; `test_noisy_tenant_cannot_starve_others` (A has 1,000 queued, B and C one each; first pass claims one of each); `test_running_cap_respected_per_tenant`; `test_runner_reads_new_tenants_every_30s` (fake clock); `test_clock_skips_tenant_without_room`; `test_job_recovered_after_runner_killed_is_in_the_right_tenant`; `test_backup_of_tenant_a_contains_no_b_rows` (read the dump's row counts); `test_restore_into_new_slug_and_into_same`; `test_drill_restore_compares_counts_and_writes_drill_json`; `test_drill_fails_loudly_on_count_mismatch` (corrupt one table); `test_drill_drops_its_scratch_schema_even_on_failure`; SQLite single-tenant drill (scratch file). Postgres tool use goes through the existing `VOLT_PG_DUMP`/`VOLT_PG_RESTORE` wrapper convention in `test_data_safety.py`; skipped when the tools are absent.
- [ ] **Step 2: Run** `python -m unittest tests.test_tenant_workers tests.test_drill -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** If the task passes a day of work, split the drill into its own commit.
- [ ] **Step 4: Run** the two files, `tests.test_runner tests.test_clock tests.test_data_safety tests.test_race_*`, and the full suite on both backends. Expected: PASS.
- [ ] **Step 5: Commit** `Tenant-aware workers, per-tenant backup, restore drill`.

### Task 7: Rate limits, concurrency gate, quotas, abuse protection

**Files:**
- Create: `control/volt/ratelimit.py`, `control/tests/test_ratelimit.py`
- Modify: `control/volt/serve.py` (`Handler.timeout`, gate, limiter calls after `_gate`, auth-failure counting, client-address rule), `control/volt/tenancy.py` (`quota_for`), `control/volt/ingest.py`, `control/volt/live.py` (quota hooks at enqueue, ingest, record start), `control/volt/telemetry.py` (counters), `docs/operations.md`

**Interfaces:**
- Produces: `class TokenBucket(rate, burst, now)`; `class Limiter: check(key: tuple, cls: str) -> Decision(allowed: bool, retry_after: float)` with classes `read`, `write`, `heavy`, plus a tenant aggregate; `SHOW_CONTROL = frozenset({"act_go","act_take","act_hold","act_release","act_kill","act_skip"})` always exempt; `class ConcurrencyGate(limit)`; `class FailureTracker(window=60, max_failures=10)`; `def client_address(peer, headers, trusted: Sequence[str]) -> str`; `def check_quota(store, limits, name, adding=1)` raising `QuotaExceeded(name, limit)` -> 429 `quota_exceeded`. Env: `VOLT_REQUEST_TIMEOUT`, `VOLT_MAX_CONCURRENT`, `VOLT_TRUSTED_PROXIES`, `VOLT_RATE_READ/WRITE/HEAVY`.

- [ ] **Step 1: Write the failing tests.** `test_bucket_refills_with_fake_clock`; `test_read_limit_is_per_principal_not_per_tenant`; `test_show_control_is_never_rejected_even_when_bucket_is_empty_and_gate_full`; `test_429_has_retry_after_header_and_body`; `test_gate_returns_503_busy_and_health_still_answers`; `test_ten_bad_tokens_in_a_minute_blocks_that_address_only`; `test_forwarded_for_ignored_unless_peer_is_trusted`; `test_stalled_client_is_dropped_after_timeout` (socket sends half a request; thread frees within timeout + 1 s); `test_quota_blocks_enqueue_and_ingest_but_not_reads_or_go`; `test_quota_unlimited_by_default_when_tenancy_off`; `test_limiter_state_is_per_process_documented` (asserts the docstring/constant names the per-node caveat).
- [ ] **Step 2: Run** `python -m unittest tests.test_ratelimit -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Limiter checks run after tenant and auth resolution so the key has a principal; the gate runs first and exempts `/health`, `/ready` and show control.
- [ ] **Step 4: Run** the file, `tests.test_hardening tests.test_serve_hold tests.test_operator_api`, and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Rate limits, concurrency gate, quotas, credential-guess lockout`.

### Task 8: Storage locations, replicas and site sync

**Files:**
- Create: `control/volt/locations.py`, `control/tests/test_locations.py`
- Modify: `control/volt/migrations.py`, `control/volt/store.py`, `control/volt/schema.sql`, `control/volt/runner.py` (`replicate` handler, `VOLT_SITE` claim filter), `control/volt/serve.py` (`act_asset_replicate`, `act_location_verify`, `GET /api/storage/locations`, proxy 302), `control/volt/backup.py` (`verify_storage` per location), `control/volt/retention.py` (purge removes copies), `docs/specs/storage.md`, `docs/operations.md`

**Interfaces:**
- Produces: new migration (tables `storage_location`, `essence_copy`); `def sync_locations(store, env) -> list[str]` from `VOLT_LOCATIONS` JSON; `def resolve(store, essence_id, site) -> Copy | None` (verified only; same site by `priority`, then others); `def replicate(store, backend_for, essence_id, to_location, clock) -> Copy`; job type `replicate` with payload `{essence, to}`; handlers refuse an open essence. Routes in `ROUTE_ROLES`: `act_asset_replicate` operator, `act_location_verify` admin. Errors per spec section 13.

- [ ] **Step 1: Write the failing tests** (fake in-memory backends and the local backend). `test_replicate_copies_verifies_and_records_only_after_checksum_match`; `test_crash_between_copy_and_record_leaves_copying_and_lease_recovers`; `test_open_essence_is_never_replicated` (409 `replicate_refused`); `test_source_copy_never_deleted`; `test_resolve_prefers_same_site_then_priority_and_skips_unverified`; `test_two_way_replication_refused_at_sync`; `test_only_runner_at_destination_site_claims`; `test_proxy_302_to_presigned_url_when_best_copy_is_in_object_store`; `test_purge_removes_every_copy_and_lists_them_in_purge_audit`; `test_legal_hold_does_not_block_replicas_but_blocks_archive`; `test_keys_are_under_tenant_prefix`; `test_routes_classified_and_audited`; `test_migration_forward_both_backends`; `test_replicate_unvalidated_label_present` (the doc page for this feature contains "unvalidated").
- [ ] **Step 2: Run** `python -m unittest tests.test_locations -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Replication bandwidth cap `VOLT_REPLICATE_MBPS` is a sleep between chunks, covered by a fake-clock test.
- [ ] **Step 4: Run** the file, `tests.test_storage tests.test_storage_clouds tests.test_archive tests.test_data_safety tests.test_migrations`, and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `Storage locations, copies and pull-based site sync (unvalidated against real sites)`.

### Task 9: Admin status and usage API and screen

**Files:**
- Create: `control/volt/adminstatus.py`, `control/tests/test_adminstatus.py`
- Modify: `control/volt/serve.py` (`GET /api/admin/status`, `GET /api/admin/usage`, node heartbeat thread), `control/volt/runner.py` (`usage-rollup` periodic), `control/volt/migrations.py` (`usage_daily`, `serve_node`), `control/volt/session/operator/{index.html,operator.js,operator.css}` (Status screen), `control/volt/session/operator/` icon sprite (if a new icon is needed), `control/tests/test_operator_ui.py`, `control/tests/operator_harness.py` (scenarios `status_ok status_degraded status_mixed_versions status_near_limit`), `docs/specs/operator-ui.md`, `docs/specs/operator-api.md`

**Interfaces:**
- Produces: `def status(ctx, now) -> dict` and `def usage(store, days: int) -> dict` (shapes in spec section 11); `Periodic("usage-rollup", 300, rollup_task)` writing `usage_daily` rows by upsert; `serve_node` heartbeat every 10 s with `draining`; JS `renderStatus(state)`, `STATE.nearlimit`/`STATE.overlimit`/`STATE.mixed`/`STATE.overdue`. Payload keys are camelCase as listed in the spec; `servers` is counts and versions only in tenanted mode.

- [ ] **Step 1: Write the failing tests.** API: `test_status_requires_admin` (operator -> 403 `forbidden_role`); `test_status_in_tenant_mode_has_no_other_tenant_or_hostname`; `test_usage_days_over_366_is_422`; `test_rollup_is_idempotent_and_sums_match_tables`; `test_status_unavailable_503_still_carries_known_fields`; `test_servers_flags_mixed_versions_and_media_key_mismatch`; `test_headroom_near_and_over_limit_at_80_and_100_percent`; `test_poll_path_writes_nothing` (statement log shows no INSERT/UPDATE during `/api/state`). UI (node, via `_node`): `test_status_hidden_for_operator_role`; `test_chip_specs_have_icon_word_class_and_sprite_icon`; `test_capacity_bar_prints_number_and_limit_in_text`; `test_polls_10s_and_60s_hidden`; `test_no_optimistic_update_on_verify_location`; contrast test includes new tokens; axe on the four harness scenarios.
- [ ] **Step 2: Run** `python -m unittest tests.test_adminstatus tests.test_operator_ui -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse `health.collect` for the health card. Re-shoot screenshots for `docs/mockups` only if the repo convention requires it; do not add a brand claim to `brand-guide.md`.
- [ ] **Step 4: Run** both files and the full suite; run the harness scenarios in a browser and axe 4.10.2 as in the slice plans. Expected: PASS.
- [ ] **Step 5: Commit** `Admin status and usage: API and Status screen`.

### Task 10: Rolling upgrades, drain, deployment shapes

**Files:**
- Create: `control/tests/test_upgrade.py`, `deploy/compose/compose.yaml`, `deploy/compose/Caddyfile`, `deploy/kubernetes/{serve,runner,clock,migrate-job,ingress,pdb}.yaml`, `docs/scale.md`
- Modify: `control/volt/migrations.py` (`Migration.breaking`, `schema_version.min_code`, `check_not_newer`), `control/volt/cli.py` (`volt migrate`, `volt upgrade plan [--verify]`), `control/volt/serve.py` (`/ready`, drain on SIGTERM, `VOLT_DRAIN_SECONDS`), `control/volt/supervisor.py` (stop grace covers drain), `control/volt/health.py` (`mediaKeyFingerprint`), `docs/operations.md` (Upgrade), `CHANGELOG.md` (bridge-release note)

**Interfaces:**
- Produces: `Migration(..., breaking: bool = False)`; `migrations.required_code_version(db) -> int` (`MAX(min_code)`); `check_not_newer` raising `SchemaTooNewError` only when `required_code_version(db) > known_version()`; `def plan(nodes, db) -> UpgradePlan(rolling_ok, pending: list[(version, breaking)], reasons)`; `GET /ready`; `Server.drain(seconds)`; CLI exit codes 0 plan ok, 2 rolling refused, 78 config.

- [ ] **Step 1: Write the failing tests.** `test_old_code_runs_against_additive_newer_schema` (synthetic newer migration with `breaking=False`: guard passes, SQLite and Postgres); `test_old_code_refuses_breaking_newer_schema`; `test_min_code_carried_forward_when_not_breaking`; `test_plan_refuses_rolling_when_any_pending_is_breaking`; `test_volt_migrate_is_idempotent_and_per_tenant_reports`; `test_ready_503_while_draining_and_health_still_ok`; `test_drain_finishes_in_flight_request_then_exits` (slow handler, SIGTERM, response completes, then the process exits; subprocess on loopback); `test_new_requests_after_drain_are_refused_at_the_listener_or_503`; `test_media_key_fingerprint_differs_when_key_differs_and_does_not_reveal_key`; `test_deploy_files_exist_and_name_ready_probe_and_pdb` (text checks only); `test_changelog_names_the_bridge_release`.
- [ ] **Step 2: Run** `python -m unittest tests.test_upgrade tests.test_migrations -v`. Expected: FAIL.
- [ ] **Step 3: Implement**, then write `docs/scale.md`: the three deployment shapes, the upgrade procedure from spec section 8, the load-test method, the limits table (filled from Task 2 numbers, with the box named) and the six rehearsals with pass conditions. Header each deploy file and the page: **unvalidated until the rehearsal in the spec has been run**.
- [ ] **Step 4: Run** the files and the full suite on both backends. `docker compose -f deploy/compose/compose.yaml config` only if Docker is present, recorded as a manual check, not a test.
- [ ] **Step 5: Commit** `Rolling upgrades: expand/contract guard, migrate step, drain, deploy shapes`.

---

## After the last task

Update `docs/roadmap.md` (the "In the tree" and "Next" lines) and `CHANGELOG.md`. Run R1 (compose, two nodes, kill -9) before telling anyone HA works; run R5 before publishing any capacity number. Open items that are not code: the customer licence agreement for installed, hosted terms and a data-processing agreement, and the identity spec's tenant-to-issuer mapping, which must use `tenant_domain` and the registry from Task 4.
