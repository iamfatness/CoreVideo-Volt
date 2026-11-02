# Volt: scale and operations, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

## Why

Volt runs well for one room: one `serve`, one or more runners, one clock, SQLite or Postgres, a Prometheus text page and a health report. The first customer is a daily news or live-show team, with a production-house MAM track second, and Volt is delivered both hosted (we run it) and installed (they run it). Hosted means several organisations in one deployment, a service that survives one process or one host dying, and numbers we can read before a customer phones us. Installed means the same code runs on one box and upgrades without a weekend. This spec covers that gap: tenants, more than one `serve` process, monitoring and alerts, storage across sites, abuse limits, rolling upgrades, backup drills, capacity and an admin status view.

**Caveat on the competition.** What the incumbents offer here (multi-tenant hosted MAM, HA, dashboards, multi-site sync, SLAs) is from general knowledge of how products in this class are usually sold, not from an audit of any named product. Nothing below says a competitor lacks or has a feature. The measured part of this spec is Volt's own code, read on 2026-11-01.

**What the code says today (read, not assumed).**

| Fact | Where | Consequence |
|---|---|---|
| `_SEQ` is a `threading.Lock` used in exactly three handlers: `act_go` (`serve.py:310`), `act_skip` (`:435`), `act_kill` (`:448`). Comments mention it at `:403`, `:449`, `:467`, `:741`. | `serve.py:83` | In-process only. But the decision in each is already made under `store.transaction(show_lock(show_id))` (`playout.advance` `:153`, `playout.skip` `:197`, `rundown.kill` `:111`), a Postgres advisory lock. `take` and the building edits already skip `_SEQ`. So `_SEQ` is a redundant courtesy for go, skip and kill, and the claim "two `serve` processes are not safe" (`product-readiness.md:34`, `architecture.md`) is stale for those three. |
| `_REC = threading.Lock()` wraps `act_record_start` (`:484`) and `act_record_stop` (`:498`) across a room wait of up to 10 s (start) or 30 s (stop). | `serve.py:84` | This is the real in-process-only lock. `mint_source` is race-safe on the unique `source_key`, but nothing stops two processes both driving the one room's recorder. |
| `Handler` sets no `timeout` and no `protocol_version`; `ThreadingHTTPServer` is unbounded; the stdlib listen backlog (`request_queue_size`) is 5. | `serve.py` (grep: none set) | A slow client holds a thread forever; every request is HTTP/1.0 and opens a new TCP connection; a burst of 50 desks may overflow the backlog. To confirm in the load test. |
| Every request opens a `Store`; a bearer check opens a second (`_bearer_principal`), and `_auth_configured` a third when no token matches. With `VOLT_PG_POOL_MAX` unset each is a new Postgres connection. | `serve.py` (`_bearer_principal`, `_auth_configured`, `do_GET`, `do_POST`) | 25 polls per second (50 desks, one poll per 2 s) is 25 to 75 connects per second against Postgres. |
| `GET /api/state` returns every undeleted asset, every show, spans, and calls `room.observe()` (a network call to the core) on every poll. | `operator_state.build`, `serve._operator_state` | Cost grows with the library and with the number of desks, and 50 desks make 25 observe calls per second on the CoreVideo core. |
| The version-skew guard runs at `serve` and runner start and on every SQLite open; on Postgres a running node is not re-checked. It refuses any database newer than the code, with no notion of an additive change. | `migrations.check_not_newer`, `serve.main` | An old node that restarts after a rolling step cannot come back even when the change was additive. |
| Metrics are gauges built per scrape from the database and disk. There is no request count, latency, error rate, pool or job-duration series. | `health.prometheus` | No SLO can be computed. |
| The runner (`runner_heartbeat`, `SKIP LOCKED` claim, leases), the clock (conditional-UPDATE claim, `show_occurrence` unique index) and the periodic tasks (compare-and-set) are already multi-process safe. | `runner.md`, `clock.py:20`, `runner.py:445` | They need tenant awareness, not new locking. |

## Goals

1. **Several tenants (organisations) in one hosted deployment, isolated.** A request, job, backup, audit row, media ticket and storage key belongs to exactly one tenant, and a defect cannot show one tenant another's data.
2. **Two or more `serve` processes behind a load balancer are supported and tested**, with a rolling restart that drops no request that was not in flight on the node being stopped.
3. **Installed customers get none of this complexity.** Tenancy off is today's behaviour, byte for byte, on SQLite or Postgres.
4. **Monitoring that can be acted on:** request, error, latency, pool and job series; dashboards and alert rules as files in the repo; suggested SLOs; a documented set of limits.
5. **Rate limits and abuse protection** that keep one tenant, one stuck client or one password guesser from taking the service from the others.
6. **A storage-location model** with a site label, replicas and a verified copy per location, so a second site can hold the media.
7. **Upgrades that roll:** expand/contract migrations, a skew rule that tolerates additive changes, drain on stop.
8. **Backups per tenant and a restore drill that is run, timed and recorded.**
9. **An admin status and usage view**, in the app, that answers "is it healthy, what is it using, what is it close to".

## Non-goals

- A billing system, invoices, plans, a signup flow or a customer portal. This spec defines the usage numbers a billing system would read, not the system.
- Single sign-on, OIDC, SCIM and the user model. That is the separate identity spec (written alongside this one; its filename is not fixed at the time of writing). This spec only fixes how a tenant is found before a user is authenticated.
- Active-active across regions, or cross-region failover of Postgres. One region at launch.
- Auto-scaling policy tuned to a cloud. Kubernetes manifests are provided as unvalidated starting points.
- Replacing the stdlib server with an ASGI or async framework. Revisit only if the load test says the thread-per-request model is the limit (see Calls I made).
- Tenant-visible control of limits and plans. Limits are set by the platform operator by CLI.
- Anything needing the CoreVideo core, a newsroom system or an NLE. Same separation as [the remaining-build spec](2026-10-31-remaining-build-design.md).

## Anti-goals

Failure even if every test passes:

- Any path where tenant A reads or writes tenant B's rows, objects, files, tickets, logs or metrics, including through an error message, a metric label, a cache or a shared module global.
- A feature that makes the installed single-tenant product slower, harder to start or dependent on Postgres.
- A rolling upgrade that works in the test and loses a take, a claimed job or an as-run time in the field.
- A limit that blocks a live `go`, `take`, `hold` or `kill` because a tenant read too much. Show control is the last thing to be shed.
- Dashboards or alerts that name metrics the code does not emit.
- Any claim that HA, a restore, a failover or a site sync works, resting on a test that used fakes.
- A per-tenant metric label with unbounded cardinality, or a per-request database write on the poll path.

## Constraints

Owner decisions, recorded here as the owner's, not mine:

- The first customer is a daily news or live-show team; a production-house MAM track is second. Capacity numbers in this spec are for the first.
- Volt is delivered both hosted and installed.
- Sign-in is OpenID Connect first; the identity spec covers it.
- Tests use fakes and never the network. Anything needing real infrastructure (cloud accounts, a cluster, a second site) is written as **unvalidated** and has a rehearsal plan below.
- Every new POST is classified in `ROUTE_ROLES` and audited. `test_users.py` already fails on an unclassified route.
- Schema changes are numbered migrations, tested forward on SQLite and Postgres, with a historical-shape test for the new head.
- The licence is proprietary (`LICENSE`, `NOTICE`). **Dependency, not this spec's topic:** installed customers need a customer licence agreement, and hosted customers need terms of service and a data-processing agreement, before either shape ships to anyone outside the owner. `LICENSE` grants no right to host or use without a written agreement.

Engineering constraints:

- Python standard library plus the existing optional extras; no new runtime dependency. Postgres 16 is the only proven version.
- Roles stay `operator` and `admin`. A platform operator is not a role in a tenant; platform actions are CLI-only.
- The operator UI stays static files under the same-origin CSP, uses the redesign system (icon plus word plus colour, tokens in each theme set that exists, existing keys unchanged) and passes axe-core.
- Payload casing is camelCase. Error bodies keep `{ok:false, error:{code, reason}}`.
- Nothing on the poll path writes to the database.

## The design

### 1. Tenancy

**Options.**

| | Schema per tenant (one database) | Row-level `tenant_id` | Database per tenant |
|---|---|---|---|
| Existing SQL | Unchanged: tables are unqualified and `search_path` picks the schema | Every statement and every unique constraint changes (`source_key`, `show_occurrence`, job keys, advisory-lock names); roughly every query in about twenty modules | Unchanged |
| Cost of a missed filter | A missing table fails loudly (`search_path` holds only the tenant schema) | Silent cross-tenant read | Fails loudly |
| Backup, restore, delete one tenant | `pg_dump --schema`, `DROP SCHEMA` | Selective export of rows from every table, hard to prove complete | Whole-database dump |
| Migrations | Run once per tenant schema (hundreds is fine, thousands is not) | Once | Once per database |
| Connections | One shared pool; `search_path` set on checkout | One pool | A pool per tenant: does not scale |
| Noisy neighbour | Shared; limited by app-level limits | Shared | Strongest; per-tenant sizing |
| Fits SQLite installed mode | Not applicable: tenancy is Postgres-only | Would force a column everywhere | Not applicable |

**Recommendation: schema per tenant, with a registry row that can point a tenant at its own database.** A tenant is a `TenantRef(slug, dsn, schema)`. Almost every tenant shares the cluster DSN and has its own schema; an enterprise tenant that must be isolated gets its own DSN and schema `public` or a named one. Both are the same code path, so database-per-tenant costs nothing extra to offer later. Row-level `tenant_id` is rejected: the cost is rewriting every query and the failure is silent. Postgres row-level security is not used; it would add a second isolation mechanism that has to be proven too.

**Control schema.** A schema `volt_control` holds the registry. It has its own numbered list (`CONTROL_MIGRATIONS`) and version table (`control_schema_version`), tested forward on Postgres only (tenancy is Postgres-only; the call is recorded below). The per-tenant list (`MIGRATIONS`) is unchanged in kind and runs in each tenant schema.

**Resolution of the tenant.** By the request's `Host` header, matched against `tenant_domain`, **before** authentication. The tenant is never taken from a request body, a header the client chooses, or the token. The token is then looked up in that tenant's `volt_user` table, so a valid token from tenant A presented to tenant B's host finds no user and gets 401. An unknown host gets the same 404 as an unknown route, with no body that confirms a tenant exists. A suspended tenant gets 403 `tenant_suspended` on everything except `/health` and `/ready`.

**How the context flows (the stdlib server).**

- `Handler` resolves a `TenantContext(ref, settings, limits)` once per request from a small in-process cache of the registry (60 s TTL; a suspend takes effect within the TTL, or at once via `volt tenant suspend --notify` which bumps a `registry_epoch` row every node reads on its next cache refresh at most every 5 s).
- Every `Store(DB_PATH, ...)` in `serve.py` (in `do_GET`, `do_POST`, `_open_store`, and any added since) becomes `ctx.open_store()`. A test parses `serve.py` and fails on any `Store(` call outside that one function.
- Module globals that are really per-tenant move into `TenantSettings`: `OUT_DIR` (thumbnails go under `OUT_DIR/<slug>/`), the proxy, publish and quarantine directories, the storage prefix (`tenants/<slug>/` in every object key), the media key, the room URL and room token (`get_room()` takes the context), and `UNPUBLISH_BACKEND`. `_REC` becomes a lease per tenant and room (section 2).
- The legacy `VOLT_TOKEN` is refused when tenancy is on (a shared admin token across tenants is a cross-tenant credential). `serve` exits 78.
- Media tickets: the signing key is derived per tenant, `HMAC(master, slug)`, so a ticket from A never verifies on B even for the same asset id. `tickets.py` is unchanged; only the secret passed in differs.
- Advisory-lock names (`show:{id}`, `asset:{id}`, `proxy-essence:{id}`, `item-cues:{id}`, `shots:{id}`) are prefixed with the schema name inside `PgConnection.transaction`, so two tenants never contend on, or are serialised by, the same lock.
- `pg.PgConnection(dsn, schema=...)` takes a connection from the one shared pool and runs `SET search_path TO "<schema>"` on checkout and `RESET search_path` on return. The pool is keyed by cluster DSN, never by tenant. The invariant is tested directly: a connection handed to tenant B after tenant A never shows A's tables.

**Runner and clock.** Both iterate tenants. `volt runner --tenants all|slug,slug` and `volt clock --tenants ...` read the registry every 30 s and serve each active tenant. The runner claims **round-robin, one job per tenant per pass**, with a per-tenant cap on running jobs (`max_running_jobs`), so a tenant with 10,000 queued transcripts cannot starve the others. Leases, retries and heartbeats are unchanged and live in each tenant's schema. The clock needs a room URL per tenant and skips a tenant with none (as `volt run` does today). A dedicated runner for one enterprise tenant is `--tenants acme`.

**Audit.** Each tenant's `audit` table is in its own schema, so tenant audit is isolated for free. Platform actions (create, suspend, resume, change a limit, move a tenant) append to `control_audit` with the operator's OS user and a reason. Tenant audit is never visible to the platform CLI's default views.

**Backup.** Per tenant: `pg_dump --schema=<schema>`; the manifest gains `tenant` and `schema`; restore can go to the same tenant or into a new slug. The control schema is dumped with the cluster (or by its own `volt backup --control`). Cluster-level protection (base backups plus WAL archiving for point-in-time recovery) is the Postgres operator's job and a stated dependency of the hosted RPO.

**Migration path from today's single-tenant data.** No data moves.
1. Stage 0: tenancy off (`VOLT_TENANCY` unset). Today's behaviour. Installed customers stay here forever.
2. Stage 1: `volt tenant adopt --slug acme --schema public --domain acme.volt.example` creates `volt_control` (control migrations) and registers the existing database as one tenant whose schema is `public`. The data is where it was.
3. Stage 2: set `VOLT_TENANCY=on` and restart. New tenants get new schemas (`volt tenant create`). The adopted tenant keeps working under its domain; with `VOLT_TOKEN` removed first and a user-token admin created (`volt user add`), because tenancy refuses the legacy token.
4. Rollback to stage 0 is the same database with `VOLT_TENANCY` unset; nothing about the adopted tenant's schema changed.

### 2. Making several `serve` processes safe

**Every use of `_SEQ`** (checked by grep on 2026-11-01):

| Site | What it orders | What already protects it across processes | Replacement |
|---|---|---|---|
| `serve.py:83` definition | | | deleted |
| `act_go` `:310` | two goes advancing one rundown | `playout.advance` runs in `store.transaction(show_lock(show_id))` (advisory lock) | none; remove |
| `act_skip` `:435` | skip against a take or go | `playout.skip` takes the show lock (`:197`); `TakingError` on a `taking` row | none; remove |
| `act_kill` `:448` | kill against a go | `rundown.kill` is one conditional write under the show lock (`:111`), tested in `test_race_kill.py` | none; remove |
| comments `:403 :449 :467 :741` | | | reworded |
| docs: `operator-api.md:231`, `foundation.md:41,53`, `architecture.md` (proven/not proven), `product-readiness.md:34` (dated, left), `shot-spans.md:161`, `plan-foundation.md` | say "single server process" | | updated except dated snapshots |

The three removals are made **only after** a test shows they change nothing: threads and then real subprocesses issue concurrent go, skip and kill with `_SEQ` patched to a no-op, and the outcome set is the same as with it.

**`_REC`** is different: nothing in the database stands behind it. Replace it with a **lease row**, not a long-held advisory lock (a transaction held for 30 s ties a pooled connection). `lease(name, holder, expires_at)` with a conditional upsert (`INSERT ... ON CONFLICT (name) DO UPDATE ... WHERE lease.expires_at < now OR lease.holder = ?`). Name `record:<room key>`, TTL 120 s, released in `finally`, expiry covers a crashed node. A second caller gets **409 `record_busy`** instead of waiting; this is a behaviour change from "queue behind the other" to "refuse", chosen because a second Record click during the first's room wait is a mistake, not a request. On SQLite, one process, the lease row works the same, so there is one code path.

**Other per-process state, checked:**

| State | Verdict |
|---|---|
| `ROOM`, `POLL`, `PUBLISH`, `UNPUBLISH_BACKEND` | Test seams; unset in production. Per-tenant room goes through the context. |
| `health._size_cache` | Per-process cache of thumbnail size; harmless. |
| Thumbnails under `VOLT_OUT/thumbs` | A per-node cache, regenerable. Needs the hi-res or proxy to be reachable from every node (below). |
| Proxy and thumbnail **serving** from `VOLT_PROXY_DIR` | A node can only serve files it can read. Multi-node needs either a shared read-write-many volume or serving from object storage (section 7: a 302 to a presigned URL). |
| Media key | `VOLT_MEDIA_KEY` (or `VOLT_TOKEN`) must be identical on every node. `/api/health` reports a short fingerprint of the key so the status view can flag a mismatch. |
| `VOLT_WATCH_SEEN` fingerprint file | Per runner host. Rule: one watcher runner per ingest root. |
| Periodic tasks (`purge`, `refresh-urls`, `cache-gc`) | Idempotent and guarded (`runner.md`, `retention.py` takes `asset:{id}` locks). Safe on many runners. `cache-gc` acts on the local disk only. |
| Rate-limit buckets (new) | In-process, so the effective limit is per node (section 4). |
| Request metrics (new) | In-process; Prometheus sums them across nodes. |

**Rules.** Two `serve` processes on SQLite are not supported (SQLite is one host; WAL tolerates it, but the rolling-restart and lease guarantees are tested on Postgres). More than one `serve` requires Postgres and `VOLT_PG_POOL_MAX` set. Pool arithmetic: `nodes * VOLT_PG_POOL_MAX + runners * 2 + reserve <= max_connections`; `volt check` warns when the product would exceed an operator-supplied `VOLT_PG_MAX_CONNECTIONS`.

### 3. Request economy (measured before it is tuned)

Cheap, safe changes the load test justifies and that the plan makes first: one `Store` per request (the auth lookup reuses it); a 1 s shared cache of `room.observe()` per room so 50 desks cost one observe call per second, with the cached age in the payload (`room.observedAgeMs`); HTTP/1.1 keep-alive (`protocol_version = "HTTP/1.1"`, which requires `Content-Length` on every response; the static and Range paths are re-checked); `request_queue_size = 128`. Not done here, left to the numbers: paginating `assets` in `/api/state` (changes the Library's contract) and an ETag/304 on the state payload.

### 4. Rate limits and abuse protection

- **Bounded server.** `Handler.timeout = VOLT_REQUEST_TIMEOUT` (default 30 s) so a stalled client frees its thread; a concurrency gate `VOLT_MAX_CONCURRENT` (default 64 per node) answering **503 `busy`** with `Retry-After: 1`. `/health`, `/ready` and show control (`go`, `take`, `hold`, `release`, `kill`, `skip`) are exempt from the gate and from rate limits.
- **Token buckets** in memory, keyed by `(tenant, principal)` per route class: `read` (default 20/s, burst 40; a desk polls at 0.5/s), `write` (5/s, burst 10), `heavy` (`act_detect`, `act_export_nle`, `act_ingest`, `act_publish`, `act_asset_archive`: 1/s, burst 3). Over the limit: **429 `rate_limited`** with `retryAfterSeconds` and a `Retry-After` header. A tenant aggregate bucket caps all principals together.
- **Credential guessing.** Per client address, 10 failed bearer checks in 60 s yields **429 `rate_limited`** for 60 s on that address, and a log line. The client address is the socket peer unless `VOLT_TRUSTED_PROXIES` names it, in which case the first untrusted `X-Forwarded-For` entry is used (the Caddy and nginx recipes in [tls.md](../../tls.md) set it).
- **Quotas** (Postgres-backed, checked at enqueue, ingest and record start, not on reads): `max_assets`, `max_storage_bytes` (sum of `essence` sizes where known), `max_jobs_per_day`, `max_running_jobs`. Over quota: **429 `quota_exceeded`** with `quota` and `limit`. Unlimited by default in installed mode; set per tenant by `volt tenant limit`.
- **Per node, not global.** N nodes allow N times the configured bucket. The hosted edge (the proxy) carries the global per-IP limit; the in-app limiter is the second line. Stated so nobody reads 20/s as a cluster-wide guarantee.

### 5. Metrics, dashboards, alerts, SLOs

**Add to `/metrics`** (all prefixed `volt_`): `http_requests_total{route_class,status_class}`; `http_request_seconds` histogram (buckets 0.005 to 10 s) by route class; `http_inflight`; `rate_limited_total{kind}`; `busy_total`; `db_checkout_seconds` histogram and `db_pool_in_use`/`db_pool_waiting` from `pg.Pool.stats()`; `take_seconds{phase="room"|"overhead"}` (so the room's time and ours are separate); `job_seconds{type}` histogram and `job_queue_wait_seconds{type}`; `runner_claims_total{outcome}`; `backup_last_age_seconds` (newest verified manifest in `VOLT_BACKUP_DIR`, cached 60 s) and `restore_drill_last_age_seconds` (from `drill.json` written by the drill); `serve_nodes{version}`; `replica_lag_assets{location}`. The existing 14 series are unchanged.

**Cardinality.** Route classes are a fixed set (about 8), not paths. Process-wide request series carry no tenant label. Per-tenant gauges (`tenant_assets`, `tenant_storage_bytes`, `tenant_jobs_queued`, `tenant_queue_oldest_seconds`) exist only on the **platform metrics listener** (`VOLT_METRICS_BIND`, default `127.0.0.1:9108`, `VOLT_METRICS_TOKEN` required off loopback), capped at 500 tenants with the rest summed as `tenant="other"`. On a tenant host `/metrics` and `/api/health` report that tenant only and omit process-wide request series, so one tenant cannot learn another's load.

**Files** (all in the repo, validated by tests against the code): `deploy/monitoring/volt-dashboard.json` (Grafana JSON: availability, request rate and p50/p95/p99, errors by class, take overhead, pool, queue depth and age, runners alive, disk, backup age, replica lag); `deploy/monitoring/volt-alerts.yml` (Prometheus rules); `deploy/monitoring/scrape.example.yml`.

**Alert rules** (page = wake someone; ticket = next working day): page on `volt_up == 0` for 1 m, a show-hold or take failure rate over budget, queued-due jobs with zero runners for 5 m, `volt_schema_version` differing across nodes for more than 30 m, a disk under its floor, `backup_last_age_seconds > 26 h`; ticket on oldest queued age over 15 m for 10 m, stuck jobs, `restore_drill_last_age_seconds > 35 d`, pool waiting for 5 m, replica lag growing for 1 h, rate-limit rejections sustained for one tenant.

**SLO suggestions** (starting points to be replaced by measured values; internal targets until the owner says they are commitments, see Open questions): availability of the API 99.9% per month, counting 5xx and `busy` over all requests except exempt health calls; `/api/state` p95 under 250 ms at 50 desks; control-plane overhead of a take (excluding the room wait) p95 under 500 ms; proxy job queue wait p95 under 60 s; hosted RPO 5 minutes (needs WAL archiving) and RTO 60 minutes; installed RPO is the backup interval (nightly default).

### 6. Deployment shapes

Minimum viable first; each later shape adds one thing.

1. **Single box (installed default, and the first hosted pilot).** `volt run` under systemd or Task Scheduler as today; SQLite or local Postgres; Caddy in front ([tls.md](../../tls.md)). Hosted pilot adds `VOLT_TENANCY=on`, Postgres, a nightly per-tenant dump to another disk and another place. No HA: a dead box is a restore. This is what ships first.
2. **Compose (one host, process-level HA).** `deploy/compose/compose.yaml`: Caddy, Postgres 16, two `serve`, two `runner`, one `clock`, Prometheus and Grafana loaded from `deploy/monitoring`. Survives a crashed process and allows a rolling restart; does not survive the host. Needs a shared volume for proxies or the object-storage serving path.
3. **Kubernetes.** `deploy/kubernetes/`: `serve` Deployment (at least 2, readiness `/ready`, preStop drain, PodDisruptionBudget), `runner` Deployment (scaled by queued-due jobs), `clock` Deployment (2 is safe), a Job for `volt migrate`, Services and an Ingress; Postgres from the provider's managed HA service; media on object storage. **Unvalidated** until the rehearsal below is run.

### 7. Storage locations and site sync

Today `essence.location` is one path or key, and `VOLT_STORAGE` is one backend for publish. The model adds **locations** and **copies** without changing `essence.location` (it stays the canonical original, so nothing existing breaks).

- `storage_location`: a named place with a `site` label, a `kind` (`local`, `s3`, `azure`, `gcs`), a `profile` (the name of a backend configured in settings; credentials stay in the SDK chains, as `storage.md` requires), a `role` (`hi-res`, `proxy`, `publish`, `archive`), `priority` within its site, `replicate_from` (another location, or none), `enabled`. Locations are declared in settings (`VOLT_LOCATIONS`, JSON) and synced into the table at start (`volt storage sync-locations`), so the table holds references and state, never secrets.
- `essence_copy(essence_id, location_id, key, sha256, bytes, state, verified_at)`; `state` is `pending`, `copying`, `verified`, `failed`. A copy counts only when `verified`.
- **Site sync is a pull.** A runner declares its site (`VOLT_SITE`). A closed essence (never an open one: a file still being recorded is not replicated) whose location has `replicate_from` set gets a `replicate` job whose payload names the destination; only a runner at the destination's site claims it. It reads from the source copy, writes through the destination backend with its own verification, compares SHA-256, and records `verified`. A crash leaves `copying`, which the lease recovers; a source copy is never deleted by replication.
- **Read resolution:** `resolve(essence, site)` returns the nearest verified copy: same site by `priority`, then other sites. A node serves a proxy by streaming a local file or answering **302** to a presigned URL when the best copy is in object storage (this removes the shared-volume requirement for multi-node serving).
- **Conflicts:** none by construction. Closed essences are immutable and addressed by checksum; replication is one-way per location; two locations replicating from each other is refused at sync time.
- **Interaction:** archive (migration 20) keeps working on the canonical copy; purge and delete remove every copy and list them in `purge_audit`; legal hold blocks archive and replica deletion alike. `verify-storage` gains a per-location mode.
- **Hosted:** tenants cannot define locations; every key is under `tenants/<slug>/` and a location's profile is chosen by the platform operator.

### 8. Upgrades and schema migrations

**Expand/contract.** A migration is `expand` (adds a column with a default or NULL, adds a table or index; code from the previous release still works) or `contract` (drops, renames, narrows or tightens). `contract` migrations are split across two releases: release N stops using the old shape, release N+1 drops it. A contract migration is declared `breaking=True` and carries a note.

**Skew rule.** `schema_version` gains a `min_code` column (set on a breaking migration to its own version, otherwise carried). `check_not_newer` then refuses only when the database's required minimum code version is greater than the code's `known_version()`; an older node against a database with only additive newer migrations keeps running and can restart. Existing rule kept: a runner or server never writes a schema it does not understand. **Bridge release:** the first release that contains this rule cannot be rolled onto nodes older than it (their guard is the old one); it is a one-time stop-and-start. Every release after it can roll.

**Procedure** (hosted and compose; installed on one box simply stops and starts):
1. `volt upgrade plan`: lists node versions, pending migrations, and for each whether it is expand or contract; refuses a rolling plan if any pending migration is `breaking`.
2. Take a verified backup (per tenant and control).
3. `volt migrate [--tenants all]`: runs the numbered migrations as a separate step, under the existing advisory lock, tenant by tenant, recording progress; safe to re-run. Nodes no longer need to be the thing that migrates.
4. Roll `serve` nodes one at a time: SIGTERM sets draining, `/ready` goes 503, the node waits up to `VOLT_DRAIN_SECONDS` (default 20) for in-flight requests, then exits; the load balancer has already stopped sending.
5. Roll runners (they finish the job in flight; leases cover the rest) and the clock.
6. `volt upgrade plan --verify` confirms one version everywhere. A mixed-version state longer than 30 minutes alerts.
Rollback: forward-only. An expand-only release can be rolled back by running the previous code (the skew rule allows it); anything else is a restore. The runbook says which.

### 9. Backups and restore drills

`volt backup` is per tenant (section 1) and scheduled by the operator's timer (`deploy/systemd/volt-backup.timer` and a Task Scheduler equivalent are added; today nothing schedules it). `volt drill restore --tenant X` is new: restore the newest backup into a scratch schema (or a scratch SQLite file), run the migrations, compare row counts to the manifest, run `verify-storage` against media, time every phase, drop the scratch, and write `drill.json` (when, duration, counts, result). The product says "restored" only when the drill's counts match. A drill is due monthly (alert at 35 days). Media is separate: object-store versioning and cross-site replicas (section 7) are the media backup; `--with-media` stays for local folders.

### 10. Load testing, capacity and limits

`control/bench/load_state.py` (stdlib threads, keep-alive on or off) drives a real `serve` against a seeded database. All numbers below are **to be measured**; none is a claim.

| Test | Shape | Measure |
|---|---|---|
| State poll | 50 desks, one poll per 2 s for 10 min; library of 100, 10,000 and 100,000 assets; room observe cached and uncached | p50/p95/p99 latency, requests per second sustained, response bytes, SQL statements per request, CPU of the `serve` process, DB connections used, observe calls per second on a fake room |
| Poll plus writes | the above plus 5 writers editing a rundown and 1 take every 30 s | take overhead p95, lock waits (`pg_locks`), any 5xx |
| Job throughput | enqueue 10,000 no-op jobs; 1, 4, 16 runners; Postgres and SQLite | claims per second, claim latency p95, duplicates (must be zero), lease recoveries after `kill -9` of a runner |
| Take storm | 20 shows, simultaneous takes against a fake room with 2 s observe latency | connection hold time, pool waiting, `busy` rate, any lost take |
| Tenants | 100 tenants with 10 idle desks each, one noisy tenant | per-tenant fairness of job claims, rate-limit behaviour, cost of the registry cache, migration time for 100 schemas |
| Soak | 24 h at the 50-desk shape | memory growth, connection count, log volume |

**Documented limits** (`docs/operations.md` gains a table; values filled from the measurements, with the box they were measured on): desks per node, assets per library before the state payload is too large, tenants per cluster before migration time or pool size bites, jobs per second, `VOLT_PG_POOL_MAX` guidance. **Capacity planning rules** are written as formulas (desks / 25 per node as a starting guess, pool arithmetic from section 2, disk per hour of recorded media from the codec) and marked as guesses until the measurements replace them.

### 11. Admin status and usage view

**API** (reads, `admin` role; a tenant admin sees their tenant only):
- `GET /api/admin/status` returns `{version, schemaVersion, tenant: {slug, state}, servers: {count, versions, mediaKeyFingerprints}, health: <health.collect shape>, usage: {assets, storageBytes, jobs24h: {done, failed}, takes24h, writes24h}, limits: {...}, headroom: {...}, backup: {lastAgeSeconds, lastDrillAgeSeconds, overdue}, locations: [{name, site, state, replicaLag}], limiter: {rejected24h}}`. Hostnames of other servers are never included for a tenant (`servers` is counts and versions); installed single-tenant mode may list nodes by name.
- `GET /api/admin/usage?days=30` returns `{ok, days: [{day, assetsCreated, storageBytes, jobsDone, jobsFailed, takes, writes, apiRequests}]}`.
Errors: 403 `forbidden_role`, 403 `tenant_suspended`, 422 `bad_input` (days over 366), 503 `status_unavailable` (the database could not be read; the body still carries what is known).

**UI.** A **Status** screen, visible only to admins, placed in the rail below a divider so it is not part of any workspace's default (Producer, Editor and Operator are layouts, not permissions). Panels, each a titled card on the redesign grid: **Health** (one chip: Healthy, Degraded or Down, icon plus word plus colour, with the `problems` sentences listed under it); **Servers** (count, versions, and a Mixed versions chip during an upgrade); **Capacity** (assets, storage, jobs per day, running jobs: each a labelled bar with the number and the limit in text, and a Near limit chip at 80% and Over limit at 100%); **Queue** (counts by type and state, oldest queued age); **Backups** (last backup age, last drill age, Overdue chip); **Storage locations** (state per location, replicas behind); **Usage, last 30 days** (a table with an inline SVG bar per day; the number is always printed). It polls every 10 s, every 60 s when the tab is hidden; nothing is optimistic; there is one action, **Verify location** (operator-safe, single press, a read-only job), which is the one POST. Keys: none added; if the builder needs one it must pass the existing key rules. New chip colours go into every theme set that exists and the contrast test; axe-core reports no violations on the screen.

### 12. Data model

Two new migration lists, no numbers claimed here (the builder takes the next free number in each at build time).

**Per-tenant list (`MIGRATIONS`, tested forward on SQLite and Postgres, historical-shape test for the new head):** `lease(name PK, holder, expires_at)`; `storage_location(id PK, name UNIQUE, site, kind, profile, role, priority, replicate_from, enabled, created_at)`; `essence_copy(essence_id, location_id, key, sha256, bytes, state, verified_at, PK(essence_id, location_id))`; `usage_daily(day, metric, value, PK(day, metric))`; `serve_node(name PK, host, pid, version, schema_version, started_at, beat_at, draining)` (used only when tenancy is off; with tenancy on it lives in the control list); `schema_version.min_code INTEGER`.

**Control list (`CONTROL_MIGRATIONS`, Postgres only, own `control_schema_version`):** `tenant(slug PK, state, dsn, schema, created_at, suspended_at)`; `tenant_domain(host PK, slug)`; `tenant_limit(slug, name, value, PK(slug, name))`; `tenant_setting(slug, name, value, PK(slug, name))` (non-secret settings only; secrets stay in the environment or a secret store, referenced by name); `registry_epoch(id, epoch)`; `control_audit(id, at, operator, action, slug, detail)`; `serve_node` as above.

### 13. API routes

| Route | Method | Role | Errors |
|---|---|---|---|
| `/api/admin/status` | GET | admin | 403 `forbidden_role`, 403 `tenant_suspended`, 503 `status_unavailable` |
| `/api/admin/usage` | GET | admin | 403, 422 `bad_input` |
| `/api/storage/locations` | GET | operator | 403 `tenant_suspended` |
| `/api/assets/{id}/replicate` | POST (`act_asset_replicate`) | operator | 404 `not_found`, 409 `replicate_refused` (open essence, no replicating location, legal-hold rules do not block replicas), 429 `quota_exceeded`, 409 `archived` |
| `/api/storage/locations/{id}/verify` | POST (`act_location_verify`) | admin | 404, 409 `location_disabled` |
| `/ready` | GET, no auth | none | 503 while draining or when the database cannot be read |
| every route | any | | 404 unknown host, 403 `tenant_suspended`, 429 `rate_limited`, 429 `quota_exceeded`, 503 `busy` |

Both new POSTs are added to `ROUTE_ROLES` and audited with the tenant's own `audit` table. Record start and stop gain 409 `record_busy`. Platform actions have no HTTP surface: `volt tenant create|adopt|list|suspend|resume|limit|migrate|domain add`, `volt platform status`.

### 14. Behaviour rules

1. The tenant comes from the host and nothing else; unknown host is 404; suspended is 403 except health and ready.
2. No `Store(` outside the context's opener; no module global holds a tenant's data.
3. A pooled connection is reset to no `search_path` before it returns to the pool; a connection that cannot be reset is closed.
4. Show control is never rate limited, gated or shed; a gate over capacity still lets those through.
5. Replication never deletes a source copy and never copies an open essence.
6. A copy is `verified` only after its SHA-256 matches; resolution never returns an unverified copy.
7. Migrations run as a step before nodes roll; a node never migrates a schema while serving unless it is the only node (single-box mode keeps today's migrate-on-start).
8. A breaking migration is refused by `upgrade plan` for a rolling upgrade.
9. A tenant's metrics, health and status never include another tenant's numbers or another server's hostname.
10. Every limit and quota response names the limit and, when relevant, when to retry.

## What the tests will and will not prove

**Will prove (fakes, no network):** isolation at the store layer and over HTTP with two tenants on Postgres schemas (poisoned-connection test, token and ticket cross-use, lint test on `Store(`); that go, skip and kill give the same outcomes without `_SEQ`, with threads and with real `serve` subprocesses on loopback; lease acquire, expiry and conflict on both backends; fair claim order and the per-tenant running cap with a frozen clock; token buckets and the concurrency gate with a fake clock; histogram and counter text format; that every metric named in the dashboard and alert files is emitted; the expand/contract skew rule against a synthetic newer migration; drain and `/ready`; replication ordering, checksum comparison, crash between copy and record, and read resolution against the local and fake backends; usage rollup arithmetic; the status payload and the Status screen's states in the harness; forward migrations of both lists.

**Will not prove:** behaviour under real network partitions, a real load balancer, real Kubernetes or compose on a real host (YAML files are structural text checks only; no YAML parser is in the standard library); Postgres failover, PgBouncer compatibility, or any Postgres other than 16; object-store replication across real sites and links; that thread-per-request survives the 50-desk shape on production hardware (the load test measures it; the suite does not); that an alert fires (no `promtool`); that a backup of a large tenant restores in the time the drill reports for a small one; or that any SLO number is met.

**Rehearsals (everything below is unvalidated until run; each has a pass condition):**
- R1 compose on one machine: `kill -9` a `serve` and a runner mid-load; no accepted take lost, no duplicated job, load balancer recovers within 10 s.
- R2 kind or a small cluster: rolling upgrade across an expand migration with the poll test running; zero failed requests that were not in flight on a stopping node.
- R3 provider-managed Postgres: failover; time to recover, count of failed requests; the pool reconnects.
- R4 hosted restore: drill on a copy of a real tenant; compare to RTO.
- R5 load test on the target VM size; fill the limits table.
- R6 two real sites over a real link: replicate 50 GB, interrupt the link, resume, verify.

## Left to the builder

Exact column types, indexes and constraint names; the SQL of the lease upsert on each backend; the exact bucket sizes after the load test; the Grafana panel layout; the alert thresholds' final numbers; the screen layout of the Status cards within the redesign's rules; how `PgConnection` resets `search_path` on a pooled connection (a `reset` callback, a connection check, or a pool `configure`); whether the registry cache lives in `serve.py` or `tenancy.py`; the byte-level format of `drill.json`; which Postgres roles the per-tenant schemas use (this spec requires that the application role cannot read another tenant's schema only if the builder finds that cheap; see Calls I made).

## Calls I made, for the owner to check

- Schema per tenant, with the registry able to point a tenant at its own database. Row-level `tenant_id` rejected for the silent-failure risk.
- Tenancy is Postgres-only and off by default; installed customers never see it. The control list is therefore tested on Postgres only.
- Tenant found by `Host`, before auth. The legacy `VOLT_TOKEN` is refused in tenanted mode.
- Platform operations are CLI-only; no platform HTTP API and no platform role in a tenant. This keeps the cross-tenant surface off the network.
- `_SEQ` is retired, not replaced, for go, skip and kill, because the show lock already decides them. This corrects the repo's own docs. `_REC` becomes a lease, and a second record click is refused (409) instead of queued.
- In-process rate limits, per node. A global limit is the edge's job.
- Replication is pull, one-way, closed essences only. Two-way sync is out.
- Rolling upgrades need a one-time bridge release. I prefer that to rewriting the guard in place.
- Stay on the stdlib server for now. If the 50-desk test shows the GIL or thread count is the limit, the next step is more processes behind the balancer (which this spec already supports) before any framework change.
- SLO numbers are suggestions; RPO and RTO for hosted depend on the Postgres service chosen.
- Per-tenant Postgres roles (so a SQL injection in one tenant's path cannot read another's schema) are desirable and I did not require them, because the application uses one pool and one role. Search-path isolation is application-level isolation, and I say so.

## Open questions

1. **Where does hosted Postgres run, and who operates it** (a provider's managed HA service, or one we run)? It sets the hosted RPO and RTO we can promise, the failover rehearsal and the backup story. Default assumed: a provider-managed service with point-in-time recovery.
2. **Are the SLO numbers internal targets, or commitments written into a customer contract?** Default assumed: internal until the owner decides. Contract wording is outside this spec.
3. **How long do we keep a cancelled hosted tenant's data, and in what form do they get it back?** Default assumed: schema kept suspended for 30 days, a full export (per-tenant backup plus media list) delivered on request, then dropped.
4. **One region at launch?** Default assumed: yes, with the region chosen by the first customer's location.
