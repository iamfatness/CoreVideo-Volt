# Operations

For the person who installs and runs Volt. Developer docs are in [architecture](architecture.md) and
[start-here](start-here.md); what is still missing before more MAM work is in
[foundation](specs/foundation.md). [product-readiness](product-readiness.md) is a dated audit (2026-10-16), not the current gap list.

## What runs

| Process | Command | Needs | What it does |
|---|---|---|---|
| serve | `volt serve` | database | Operator API, `/` operator desk, health. Two processes serialise a take on item status `taking`; go, skip and kill decide under the show's database lock; record start and stop hold an `exclusive_lease` row (a second caller gets 409 `record_busy`). More than one `serve` needs Postgres (see "More than one serve process"). |
| runner | `volt runner` | database, ffmpeg | Claims queued jobs (ingest, proxy, transcript, publish, shot detection) and retries them. Several are safe. A crashed runner loses no work: its leases lapse and the jobs are re-queued. With `VOLT_WATCH=1` it also scans ingest folders. |
| clock | `volt clock` | database, `VOLT_ROOM_URL` | Takes scheduled rundown items on time. Run one. |
| run | `volt run` | all of the above | Starts the three as child processes, restarts any that crashes, stops them together. The clock is started only when `VOLT_ROOM_URL` is set. |

`python -m volt.serve`, `python -m volt.runner <db>` and `python -m volt.clock <db>` still work and take the
same arguments.

## Install

Requirements: Python 3.12 or newer, `ffmpeg` and `ffprobe` on PATH. The control plane itself has no
third-party Python dependencies.

```
git clone https://github.com/iamfatness/CoreVideo-Volt.git
cd CoreVideo-Volt
python -m venv venv
venv/bin/pip install -e "control[postgres]"          # Windows: venv\Scripts\pip
```

Install from a checkout with `-e` (editable), or from a wheel: operator pages (`control/volt/session/`) and
`control/volt/schema.sql` ship inside the package. `volt check` fails with a clear message if they are missing.
Extras, all optional:

| Extra | Adds | Needed for |
|---|---|---|
| `postgres` | psycopg, psycopg_pool | a `postgres://` database, `VOLT_PG_POOL_MAX` |
| `speech` | faster-whisper | `VOLT_ASR_ENGINE=faster-whisper` |
| `s3` | boto3 | `VOLT_STORAGE=s3` |
| `azure` | azure-storage-blob, azure-identity | `VOLT_STORAGE=azure` |
| `gcs` | google-cloud-storage | `VOLT_STORAGE=gcs` |
| `all` | everything above | |

Then check the machine before the first start:

```
volt check
```

Each line is `ok`, `warn` or `fail`; the exit code is 1 if anything failed. It checks Python, ffmpeg and ffprobe,
that the package has its pages and schema, the database (reachable, schema version against this build; it opens
SQLite read-only and never creates or migrates anything), the storage configuration, whether the token rule
allows the bind address, whether the port is free, and free disk against the floor. If `VOLT_ROOM_URL` is set and
`VOLT_DB` is SQLite it warns `sqlite_on_air` ("the room is configured; Postgres is the store for a live show").
A laptop without a room stays quiet. It never prints a token or a database password. `volt check --json` is for
scripts; `--env-file` checks the same settings a service will use.

Third-party terms for ffmpeg, faster-whisper, and the optional SDKs are in `NOTICE`. Volt itself is proprietary: see `LICENSE`. (Before 2026-11-01 there was no LICENSE.)

## Configure

Settings are environment variables. For a service put them in a settings file and pass `--env-file`:
`KEY=VALUE` per line, `#` comments, no expansion, backslashes literal, and a variable that is already set in the
real environment wins over the file. Templates: `deploy/systemd/volt.env.example` and
`deploy/windows/volt.env.example`. The file holds the token and perhaps a database password; restrict it (the
Windows installer does).

Defaults below are what applies when the variable is unset.

### Core

| Variable | Default | Meaning |
|---|---|---|
| `VOLT_DB` | `control/volt.db` | SQLite path or `postgres://user:pass@host/db`. `volt run` passes it to every process through the environment, so a DSN never appears on a command line. |
| `VOLT_OUT` | `control/out` | Thumbnail cache (`thumbs/`) and publish output root for the server. |
| `VOLT_PROXY_DIR` | none | Where the runner writes proxies. Required for proxy jobs. |
| `VOLT_PUBLISH_DIR` | none | Local publish directory. Required for `VOLT_STORAGE=local` publishing. |
| `VOLT_HOST` | `127.0.0.1` | Bind address. Anything but loopback needs `VOLT_TOKEN`. |
| `VOLT_PORT` | `8765` | Port; `0` picks a free one. |
| `VOLT_TOKEN` | none | Legacy shared bearer token, treated as an admin named `desk`. Required (16+ characters) to bind beyond loopback unless an enabled admin user exists. With no token and no users the server answers loopback `Host` headers only, so behind a TLS proxy a token or user is required ([tls](tls.md)). POSTs with this token may send `X-Volt-Actor` (default `desk`); per-user tokens ignore the header. |
| `VOLT_PUBLIC_URL` | none | The address browsers use for this server, e.g. `https://volt.example.com`. Decides the session cookie (`__Host-` name and `Secure` under https) and the `Origin` a cookie write must carry. Unset, cookie sessions work on loopback only. |
| `VOLT_SESSION_IDLE` / `VOLT_SESSION_MAX` | `28800` / `50400` | Seconds a cookie session may sit idle (8 h) and its absolute lifetime (14 h). |
| `VOLT_OIDC_CONFIG` | none | Path of the JSON file that turns on sign-in with Google or Microsoft (OpenID Connect). **Unvalidated against a real provider**: only the fake in `control/tests/oidc_fake.py` has been used. Needs `VOLT_PUBLIC_URL` (https, or a loopback address) because the redirect URI is `VOLT_PUBLIC_URL` + `/auth/callback`, never taken from the `Host` header. A path that cannot be read, or a wrong file, stops `volt serve` with exit 78. See "Sign-in with Google or Microsoft" below. |
| `VOLT_MEDIA_KEY` | none | HMAC key for media tickets. Unset: tickets are signed with `VOLT_TOKEN`. |
| `VOLT_ROOM_URL` | none | CoreVideo Pro core for cues, recording and the clock. Without it room-backed routes answer `no_room` and `volt run` skips the clock. |
| `VOLT_ROOM_TOKEN` | none | Token for that core, if it needs one. |
| `VOLT_SHOW_INPUT_ACTIONS` | `[{"action":"input.assign","args":["{slot}","{source}"]},{"action":"scene.route.set","args":["{scene}","{route}","{slot}"]}]` | JSON list of `{action, args}` steps before `transport.take` for `show-input.take`. Placeholders `{scene}`, `{source}`, `{slot}`, `{route}` come from the cue payload. Unset uses the default. `{route}` defaults to `ohg-host` when the payload omits `route`. Bad JSON refuses serve and clock at start. |
| `VOLT_CLOCK_GRACE` | `30` | Seconds past `scheduled_at` an item may still fire. |
| `VOLT_EXPORT_PATH_MAP` | none | `D:\iso=\\nas\iso;...` rewrites media paths in NLE exports. |

### Logging

| Variable | Default | Meaning |
|---|---|---|
| `VOLT_LOG_DIR` | none (stderr only) | Write `<dir>/<process>.log` (`serve.log`, `runner.log`, `clock.log`, `run.log`) with rotation. |
| `VOLT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `VOLT_LOG_FORMAT` | `text` | `text` (logfmt-style) or `json`. |
| `VOLT_LOG_MAX_MB` | `10` | Size at which a log file rotates. |
| `VOLT_LOG_BACKUPS` | `5` | Rotated files kept per process. |
| `VOLT_TENANCY` | off | `on` makes `serve` resolve a tenant from the `Host` header (hosted multi-tenant, Postgres only; see "Several organisations in one deployment"). Refused together with `VOLT_TOKEN` or `VOLT_OIDC_CONFIG` (exit 78). |
| `VOLT_CONTROL_DSN` | `VOLT_DB` when that is a Postgres DSN | The Postgres database holding the tenant registry (hosted multi-tenant only). Read by `volt tenant`. |
| `VOLT_CONTROL_SCHEMA` | `volt_control` | The schema in that database that holds the registry. |
| `VOLT_REQUEST_TIMEOUT` | `30` | Seconds a client socket may stall before `serve` drops it and frees the thread. Matters because connections are kept alive (HTTP/1.1). |
| `VOLT_ROOM_OBSERVE_TTL` | `1` | Seconds the room's observation is shared between desks. 50 desks polling `/api/state` cost one `observe()` per second per `serve` process, not 25. `room.observedAgeMs` in the payload says how old it is. |
| `VOLT_BACKUP_DIR` | none | A folder of `volt backup` directories (or one). Only read by the metrics: `volt_backup_last_age_seconds` is the age of the newest complete one (manifest readable, data file at its recorded size; not a re-hash). |
| `VOLT_ACCESS_LOG` | off | `1` logs every HTTP request (also `volt serve --access-log`). Errors, refused logins and job outcomes are logged either way. |

### Health and disk

| Variable | Default | Meaning |
|---|---|---|
| `VOLT_MIN_FREE_MB` | `2048` | Free-space floor. Under it, proxy and thumbnail work is refused, ingest jobs wait, and health says `degraded`. `0` turns the floor off. |
| `VOLT_THUMB_CACHE_MB` | `1024` | Thumbnail cache cap; the oldest thumbnails go first. `0` = no cap. |
| `VOLT_CACHE_GC_EVERY` | `600` | Seconds between the runner's cleanup passes (thumbnail cap, stale temp files). `0` = off. |
| `VOLT_PART_MAX_AGE_HOURS` | `24` | A `*.part.mp4` in the proxy directory untouched this long is a killed encode's leftover and is removed. |
| `VOLT_RUNNER_STALE_SECONDS` | `120` | A runner with no heartbeat for this long counts as gone. |
| `VOLT_STUCK_JOB_SECONDS` | `14400` | A running job older than this is reported as stuck (an expired lease always is). |
| `VOLT_QUARANTINE_DIR` | none | Where watch-folder ingest copies cards. Purge deletes there only when this names the directory; `volt verify-storage` checks it; health and `volt check` report its disk. |
| `VOLT_INGEST_ROOTS` | none | Folders HTTP ingest and watch may read (OS path separator). Unset: ingest is disabled. |
| `VOLT_WATCH` | off | `1` scans ingest roots from the runner (not a fourth `volt run` process). |
| `VOLT_WATCH_STABLE_SECONDS` | `5` | A watch file must sit still this long before it is requested. The first observation of a file never ingests; `volt watch --once` on a brand-new file queues nothing until a later scan. |
| `VOLT_WATCH_EVERY` | `2` | Seconds between watch scans. |
| `VOLT_WATCH_SEEN` | `<db>.watch-seen.json` | Where `volt watch` stores fingerprints between processes. Unset uses a file next to the SQLite database. A Postgres DSN needs this set, or `--once` cannot remember a first observation. The runner periodic does not use this file. |

### Deletion and retention

Deleting an asset is a soft delete (tombstone); a purge removes its files after a window. Details in
`docs/specs/retention.md`. `volt check` validates these and says whether purging is on.

| Variable | Default | Meaning |
|---|---|---|
| `VOLT_PURGE_EVERY` | `0` (off) | Seconds between the runner's purge passes. Off means tombstoned assets are never purged automatically. |
| `VOLT_PURGE_AFTER_DAYS` | `30` | Days a tombstone waits before purge. `VOLT_PURGE_AFTER_DAYS_<CLASS>` overrides it for one `retention_class`; `0` = at once. |

### Runner and speech

| Variable | Default | Meaning |
|---|---|---|
| `VOLT_ASR_ENGINE` | none | `faster-whisper`. No engine: transcript jobs fail loudly. |
| `VOLT_ASR_MODEL`, `VOLT_ASR_DEVICE` | engine default, `auto` | Model and device. |
| `VOLT_ASR_CORRECTIONS` | none | Corrections file. |
| `VOLT_ASR_WINDOW`, `VOLT_ASR_OVERLAP` | `900`, `15` | Seconds per window and overlap for long files. |
| `VOLT_ASR_VAD` | `1` | `0` turns voice-activity filtering off. |
| `VOLT_ASR_VOCAB_MODE` | `initial_prompt` | `hotwords`, `initial_prompt` or `none`. |
| `VOLT_ASR_MIN_WORD_PROB`, `VOLT_ASR_SILENCE_GUARD` | none | Hallucination guards. |
| `VOLT_ASR_CPU_THREADS` | `0` | `0` = engine default. |
| `VOLT_ASR_IDLE_RELEASE` | `900` | Seconds idle before the model is unloaded; `0` = never. |
| `VOLT_REFRESH_URLS_EVERY`, `VOLT_REFRESH_URLS_WITHIN` | `0` (off), `86400` | Re-sign expiring storage URLs. |

### Storage (`VOLT_STORAGE`, default `local`)

`local`, `s3`, `azure` or `gcs`. Credentials come from the provider's own chain (environment, profile, role);
Volt never reads a key from its own settings. `VOLT_STORAGE_SCRATCH` is the local directory where cuts wait for
upload. Details and what is proven are in `docs/specs/storage.md`.

| Backend | Variables |
|---|---|
| s3 | `VOLT_S3_BUCKET` (required), `VOLT_S3_PREFIX`, `VOLT_S3_REGION`, `VOLT_S3_ENDPOINT_URL`, `VOLT_S3_URL_EXPIRES` (default 7 days), `VOLT_S3_PUBLIC_URL_BASE`, `VOLT_S3_PART_MB` (16), `VOLT_S3_SSE` (`aws:kms` or `AES256`), `VOLT_S3_KMS_KEY_ID` |
| azure | `VOLT_AZURE_CONTAINER` (required), `VOLT_AZURE_ACCOUNT_URL` or `AZURE_STORAGE_CONNECTION_STRING` (one required), `VOLT_AZURE_PREFIX`, `VOLT_AZURE_URL_EXPIRES` (7 days), `VOLT_AZURE_PUBLIC_URL_BASE`, `VOLT_AZURE_BLOCK_MB` (16) |
| gcs | `VOLT_GCS_BUCKET` (required), `VOLT_GCS_PREFIX`, `VOLT_GCS_PROJECT`, `VOLT_GCS_ENDPOINT_URL`, `VOLT_GCS_URL_EXPIRES` (7 days), `VOLT_GCS_PUBLIC_URL_BASE`, `VOLT_GCS_CHUNK_MB` (16) |

### Local Postgres in a container

`deploy/postgres/compose.yaml` runs Postgres 16 on loopback port 5433 (override with `VOLT_PG_PORT`) with one named volume, `restart: unless-stopped` and a health check.

```
cd deploy/postgres
cp postgres.env.example postgres.env        # set VOLT_PG_PASSWORD (postgres.env is git-ignored)
docker compose --env-file postgres.env up -d
export VOLT_DB='postgresql://volt:<password>@127.0.0.1:5433/volt'
volt check
```

The volume lives in Docker's data disk. Check free space on the drive that holds it before a large load (on the dev box that disk is on D:). `volt backup` and `volt restore` still need `pg_dump` and `pg_restore` on the machine that runs Volt; the container's copies are not used.

Run against it on 2026-10-30 (Windows 11, Docker Desktop 29.8.2, postgres:16, Python 3.12.10, psycopg 3.3.6): the full suite with `VOLT_TEST_PG_DSN` set ran 1434 tests, OK, 44 skipped (428 skipped without it); `volt check` passed the database line; `volt.serve` with `VOLT_PG_POOL_MAX=4` created schema v16, `/api/health` reported `postgres` current, a POSTed show was in the table and was still there after a container restart. Backup drill, same day, with `pg_dump` and `pg_restore` 16.10 from the EnterpriseDB binaries zip (extracted to a folder and pointed at with `VOLT_PG_DUMP` / `VOLT_PG_RESTORE`) against the 16.15 server: two shows created through the API, `volt backup` (schema v16, 20 rows, SHA-256 in the manifest), `volt verify` (exit 0), `volt restore` into a fresh database (2 shows in source and copy). A dump with one byte appended failed `volt verify` with exit 2 ("is 24153 bytes, the manifest says 24152"), and `volt restore` onto the now non-empty copy was refused with exit 2 until `--overwrite`. Runner and clock, same day: `volt run` (serve, runner and clock, `VOLT_PG_POOL_MAX=4`) against a scratch database at schema 17. An `ingest` POST ran the ingest, proxy and ASR jobs through the runner (ingest and proxy `done`; ASR `failed` with "no ASR engine configured", since no engine was installed). A show item scheduled 40 seconds ahead was taken by the clock at its scheduled second (`schedule_state` fired, item on air, `as_run_in` set) and the room, a stub HTTP server, received `graphics.lowerThird.set`. A real CoreVideo Pro core was not in this run. Not run: `--with-media`, `--overwrite`, `volt verify-storage`, a restore from an older schema, two runners contending for one job, a clock restart mid-claim, and a managed cloud Postgres.

### Postgres pool

Off by default. `VOLT_PG_POOL_MAX` (> 0 turns it on; use 2 or more), `VOLT_PG_POOL_MIN`, `VOLT_PG_POOL_TIMEOUT`,
`VOLT_PG_POOL_MAX_IDLE`, `VOLT_PG_POOL_MAX_LIFETIME`.

### Measured on one box (the state poll)

`python -m bench.load_state` (see its docstring) drives N desks, each polling `GET /api/state` every 2 s. **These figures are one machine's result and a guess for any other**: Windows 11, 28 logical CPUs, Python 3.12.10, SQLite on a local disk, `serve` as its own process and the load generator as another on the same machine, no room configured, 2026-11-03. Nothing here was run against Postgres, a network, a proxy or real desks' browsers.

| Library | Desks | Result |
|---|---|---|
| 100 assets | 50, 30 s, keep-alive | 25.0 requests/s sustained (the 50 x 0.5 Hz offered), p50 2.5 ms, p95 4.1 ms, p99 13 ms, 0 errors; 32 SQL statements per request |
| 10,000 assets (2.6 MB per response) | 1, back to back | about 21 requests/s, 44 ms each |
| 10,000 assets | 50, 30 s, keep-alive | **4.1 requests/s, p50 14 s: saturated** (50 desks offer 25/s). 3 desks back to back already fall to 9.7/s with 300 ms each |
| 10,000 assets | 50, 30 s, a new connection per poll | 8.3 requests/s, p95 17 s: also saturated |
| 100,000 assets (26.6 MB per response) | 1, back to back | 514 ms each; at 50 desks the first poll of every desk timed out at 30 s |

What this says, and does not: one `serve` process serves a 100-asset library to 50 desks with room to spare, and **cannot** serve a 10,000-asset library to 50 desks, because every poll rebuilds and sends the whole asset list (the spec's open item: paginate `assets`, or conditional requests). Concurrent polls cost far more than the sum of one (a Python process with an interpreter lock doing CPU work per request); more `serve` processes behind a balancer is the lever the design already supports, but a second process was not measured here. Two defects were found and fixed by this measurement: the asset list scanned the whole `essence` table once per asset (about 2.8 s at 10,000 assets; migration 36 adds the index; 0.03 s after), and on SQLite every request re-ran the schema bootstrap and took the write lock (now once per file per process). The keep-alive figure at 10,000 assets is not better than the new-connection figure: the limit is the work per response, not the connection. The room cache (`VOLT_ROOM_OBSERVE_TTL`) was exercised by tests with a fake room, not in this run.

### Several organisations in one deployment (tenancy, hosted only)

Off by default, Postgres only, and installed single-tenant use never touches it. A hosted deployment gives each organisation its own **schema** in one Postgres database (or, for one that must be kept apart, its own database), plus a registry in a control schema (`volt_control`, with its own numbered migrations and `control_schema_version`). Row-level `tenant_id` was rejected: a missed filter is a silent cross-tenant read, whereas a missing table fails loudly.

Platform operators work from the command line only (there is no platform HTTP surface and no platform role inside a tenant). Each action is appended to `control_audit` with the operating-system user and a reason; a DSN or password never goes in it.

```
volt tenant create --slug acme --domain acme.volt.example     # schema t_acme, every migration, a registry row, the first domain
volt tenant adopt --slug legacy --schema public --domain volt.example   # register today's data where it is; nothing moves
volt tenant list
volt tenant suspend acme --reason "unpaid"   /   volt tenant resume acme
volt tenant limit acme max_assets 50000      # max_assets, max_storage_bytes, max_jobs_per_day, max_running_jobs (enforced by a later slice)
volt tenant domain add acme news.acme.example
volt tenant setting acme room_url http://core.acme.internal:8011   # also room_token_env NAME (the variable holding the token)
volt tenant user acme add admin1 admin       # the token is printed once
volt tenant migrate [--tenants all|acme,legacy]   # idempotent; a tenant that fails is reported and the rest still run
```

How a connection is kept inside its tenant: every `Store` for a tenant sets `search_path` to that schema when it is checked out, prefixes advisory-lock names with the schema (two tenants never serialise on `show:1`), and resets `search_path` before the connection returns to the pool. A connection that cannot be reset is closed, not lent on. The pool stays keyed by cluster DSN, never by tenant. This is **application-level isolation**: one pool and one database role serve every tenant, so it does not defend against SQL that names another schema. Per-tenant Postgres roles would; they are not done.

Proven by `control/tests/test_tenancy.py` on Postgres 16 only (a pooled connection handed from tenant A to tenant B shows none of A's tables; lock names do not collide across tenants; control migrations forward and refuse a newer schema). **On the request path** (`VOLT_TENANCY=on`, with `VOLT_CONTROL_DSN` or `--db` naming the Postgres cluster):

- The tenant comes from the `Host` header **before authentication** and from nothing else (not a body, a query or another header). An unknown host gets the plain 404 an unknown route gets, with nothing that confirms a tenant exists. A suspended tenant gets `403 tenant_suspended` on everything except `/health` and `/ready`. A registry that cannot be read is `503 tenant_unavailable`. A resolved tenant is cached 60 s, but the registry epoch is re-read every 5 s and any platform action bumps it, so a suspend takes effect within about 5 s.
- The token is then looked up in **that tenant's** `volt_user` table, so a valid token from tenant A on tenant B's host is 401. A tenant with no users is locked, never the open loopback mode. `VOLT_TOKEN` is refused (`serve` exits 78), as is `VOLT_OIDC_CONFIG`: browser sign-in is not built for tenant mode, use per-user tokens (`volt tenant user <slug> add <name> <role>`).
- Per tenant: the media and prompter ticket key (`HMAC(VOLT_MEDIA_KEY, slug)`, so a ticket for tenant A never verifies on B even for the same asset id; `VOLT_MEDIA_KEY` is the master and must be the same on every node), the thumbnail and publish folder (`VOLT_OUT/<slug>`), the quarantine folder, the storage prefix (`tenants/<slug>/`) and the room (`volt tenant setting <slug> room_url <url>`; a room token is never stored, `room_token_env` names the environment variable that holds it; the process-wide `VOLT_ROOM_URL` is never a fallback). `/metrics` and `/api/health` on a tenant host read that tenant's tables, omit the process-wide request series, and show no database path or other tenant's folder.
- Every `Store(` in `serve.py` goes through `open_request_store` (a test parses the source and fails on any other).

Proven by `control/tests/test_tenant_http.py` on Postgres 16 with two tenants and a fake room. **Not proven or not built:** browser (cookie) sign-in on a tenant host; per-tenant proxy, publish and quarantine folders in the runner and ingest (they still read the process environment, so a worker serving tenants needs the tenant-aware runner slice); quotas and rate limits; Postgres roles per tenant; a real hosted deployment. Migration from today's data: `volt tenant adopt --schema public` registers it where it is, then set `VOLT_TENANCY=on`, remove `VOLT_TOKEN` and add a user per tenant first.

### More than one serve process

Two or more `volt serve` processes behind a balancer are supported on **Postgres only** (SQLite is one host; the guarantees below are tested on Postgres). Set `VOLT_PG_POOL_MAX` on every node, and give every node the same `VOLT_MEDIA_KEY` (or `VOLT_TOKEN`) so a media ticket from one verifies on the other. Pool arithmetic: `nodes * VOLT_PG_POOL_MAX + runners * 2 + reserve` must not exceed the server's `max_connections`.

What keeps two nodes from fighting: go, skip, kill and take decide under the show's advisory lock (and the `taking` row) in the database, so there is no process lock to share. Record start and stop take a lease row named `record:<room>` in `exclusive_lease` (TTL 120 s, released when the call ends; a node that dies mid-call simply stops renewing and the row expires). A second Record click while the first is in flight is refused with `409 record_busy`; before this it queued behind the first. Lease expiry compares epoch seconds written by each node, so keep node clocks within a few seconds (use NTP).

Proven by `control/tests/test_multi_serve.py`: threads on two connections, and two real `serve` subprocesses on loopback sharing one database and one fake core. **Unvalidated:** a real load balancer, two hosts, a network partition, a Postgres failover. Rehearse before relying on it.


## Users and tokens

Three roles (see [access](specs/access.md) for groups, per-show access and sessions). **viewer** reads everything the desk shows and writes nothing but presence, its own comments and notification reads. **operator** runs the show: go, take, hold, release, skip, kill, reorder, attach, record, clear/legal-hold/restore
rights, publish, clip edits, rundown and cue writes, ingest, and every read. **admin** does all of that plus managing
users and tokens, and the destructive actions: delete an asset, delete a show, delete a clip, unpublish, and re-detect shots.
The full table is `ROUTE_ROLES` in `control/volt/serve.py`; a test fails when a route is not classified.

```
volt user add alice --role operator     # prints the token once; it cannot be recovered
volt user add root --role admin
volt user list [--json]                 # names, roles, state; never tokens
volt user disable alice                 # revoked on her next request
volt user enable alice
volt user rotate alice                  # prints a replacement once; the old token stops working at once
```

Tokens are random, stored only as a sha256 hash (table `volt_user`, migration 17) and compared in constant time. The
audit actor for a per-user token is the user's name; `X-Volt-Actor` is ignored for it. A role violation is `403
forbidden_role`; a bad or disabled token is `401 unauthorized`. The last enabled admin cannot be disabled unless
`VOLT_TOKEN` is set. `volt user` reads `VOLT_DB` (or `--db`) and works on the database directly, so it also recovers a
locked-out server. An admin can do the same over HTTP (`POST /api/users`, `/api/users/{name}/disable|enable|rotate`,
`GET /api/users`).

A users-only setup (no `VOLT_TOKEN`) may bind beyond loopback once one enabled admin exists. As soon as any user row
exists, every non-static request needs a token. Media tickets need a signing key: set `VOLT_MEDIA_KEY` when you have no
`VOLT_TOKEN`. Tokens cross the LAN in the clear without TLS, as before.

### Newsroom approval (set this for the first customer)

Approval is a deployment setting, not a code default: `VOLT_REQUIRE_APPROVAL` is off and `VOLT_APPROVERS` is empty unless you set them. For the first customer set both:

```
VOLT_REQUIRE_APPROVAL=1
VOLT_APPROVERS=alice,carol
```

- `VOLT_REQUIRE_APPROVAL=1`: a story that is not approved at its current revision cannot go to air (Go, Take, the clock, the cue path, the prompter).
- `VOLT_APPROVERS` (comma-separated user names): only these people can approve a story or take an approval back. The shared `VOLT_TOKEN` and open loopback cannot approve once the list is set (their actor name is only a header); sign in as a named user.
- **A writer cannot approve their own story.** With `VOLT_APPROVERS` set, the user who saved the current revision of a story is refused (`403 own_story`); another listed approver must approve it. The last editor is the actor on the current revision's history row: after someone else edits, the earlier writer may approve; a revert is a save, so whoever reverts becomes the last editor. Text no save has touched (as first written) has no last editor. Taking an approval back is not restricted by this rule. Without `VOLT_APPROVERS` (a one-person desk) the rule does not apply.

## Start

Foreground, everything, supervised:

```
volt run --env-file /etc/volt/volt.env
```

or with flags: `volt run --db postgresql://... --port 8765 --log-dir /var/log/volt`. Ctrl-C or SIGTERM stops
serve, runner and clock together (the runner finishes the job in flight, up to `--stop-grace`, default 20 s, then
it is killed and its lease recovers the job). A crashed child is restarted after 1 s, 2 s, 4 s, up to 60 s
(`--backoff-max`); the wait resets after a child has stayed up for a minute. A child that exits with code 78
(configuration error) is not restarted and `volt run` stops with 78.

### As a service

Linux, systemd: `deploy/systemd/volt.service` (steps are in its header). It runs `volt run`, restarts it on
failure, does not restart on exit 78, and stops with SIGTERM.

Windows: Task Scheduler, which needs nothing installed. From an elevated PowerShell:

```
.\deploy\windows\install-volt-task.ps1 -Python C:\Volt\venv\Scripts\python.exe -DryRun   # print the plan
.\deploy\windows\install-volt-task.ps1 -Python C:\Volt\venv\Scripts\python.exe
notepad C:\ProgramData\Volt\volt.env                       # token, database, room URL
volt check --env-file C:\ProgramData\Volt\volt.env
Start-ScheduledTask -TaskName Volt
```

The task runs at boot as SYSTEM, has no time limit, and Task Scheduler restarts `volt run` every minute if it
exits with a failure. `Stop-ScheduledTask -TaskName Volt` ends it; that is a hard stop, not a Ctrl-C, so a job in
flight is recovered from its lease on the next start (no work is lost; the job is retried). For a graceful stop
run `volt run` in a console and press Ctrl-C. `-Uninstall` removes the task and keeps the data.

Why not a Windows service: `sc.exe create` cannot run an ordinary program as a service (it must speak the
service control protocol), so it needs a wrapper such as NSSM or WinSW. If you prefer a real service, NSSM works
with `volt run --env-file ...` as the application and is optional; nothing in Volt depends on it.

## Health

| Route | Auth | Returns |
|---|---|---|
| `GET /health` | none | `{"status":"ok","version":"..."}`. Liveness only, no data. Use it for the supervisor or load balancer. |
| `GET /api/health` | operator token (or loopback with no token) | Full report, JSON. `503` when the database cannot be read. |
| `GET /metrics` | same | The same numbers as Prometheus text. |

`/api/health` fields: `status` (`ok`, `degraded`, `down`), `problems` (plain sentences), `database` (kind,
`schemaVersion` against `expectedSchemaVersion`), `runners` (newest heartbeat age, alive count, each runner),
`queue` (counts by job type and state, `queued`, `queuedDue`, `oldestQueuedAgeSeconds`, `running`, `failed`,
`stuck`), `asRun` (last as-run time and its age), `disk` (free bytes and floor for out, proxy, publish,
quarantine, scratch and database directories), `thumbCache`, `storage`, `indexing` (whether text search has its
full-text index, assets and documents indexed, and the queued and failed `search` and `ai-index` jobs; see
[search.md](specs/search.md)).

`degraded` means a person should look: jobs are waiting and no runner has checked in, a job has an expired lease
or has run longer than `VOLT_STUCK_JOB_SECONDS`, a directory is under the disk floor, the schema is behind or
newer than this build, or the storage configuration is invalid. The storage check does not call the cloud: it
proves the configuration parses and the SDK imports, so a provider outage does not hang the endpoint.

Metrics: `volt_up`, `volt_info{version}`, `volt_degraded`, `volt_schema_version`,
`volt_runner_heartbeat_age_seconds`, `volt_runners_alive`, `volt_jobs{type,state}`,
`volt_queue_oldest_queued_age_seconds`, `volt_jobs_stuck`, `volt_last_as_run_age_seconds`,
`volt_disk_free_bytes{dir}`, `volt_disk_low{dir}`, `volt_thumb_cache_bytes`, `volt_storage_ok{backend}`.

Request, pool and job series (added to the same page; in memory per `serve` or runner process, so Prometheus sums nodes; see `control/volt/telemetry.py`):
`volt_http_requests_total{route_class,status_class}`, `volt_http_request_seconds{route_class}` (histogram), `volt_http_inflight`,
`volt_db_checkout_seconds` (histogram), `volt_db_pool_in_use` and `volt_db_pool_waiting` (Postgres with `VOLT_PG_POOL_MAX` only),
`volt_take_seconds{phase="room"|"overhead"}` (the room's time and ours, apart), `volt_job_seconds{type}`,
`volt_job_queue_wait_seconds{type}` and `volt_runner_claims_total{outcome}` (runner process), `volt_backup_last_age_seconds` and
`volt_restore_drill_last_age_seconds` (only when `VOLT_BACKUP_DIR` is set and holds a complete backup or a passing `drill.json`).
`route_class` is one of `state read write heavy health auth static media`, never a path; no metric carries a user, tenant or id. The
fourteen series above are unchanged.

Files: `deploy/monitoring/volt-dashboard.json` (Grafana), `deploy/monitoring/volt-alerts.yml` (Prometheus rules; pages: down, write
errors, no runner, schema mismatch, disk, backup age; tickets: queue age, stuck jobs, drill age, pool waiting, slow state poll) and
`deploy/monitoring/scrape.example.yml`. **Unvalidated:** neither Prometheus nor Grafana was run against them. A test proves every metric
they name is one the code emits; it does not prove the rules are sensible for your traffic or that an alert fires. Rehearse them
before relying on a page.
Prometheus needs the bearer token: `authorization: { credentials_file: ... }` in the scrape config.

Day-of check: `curl -s localhost:8765/health`, then `curl -s -H "Authorization: Bearer $VOLT_TOKEN"
localhost:8765/api/health` and read `status` and `problems`.

## Logs

One line per event: `ts=... level=INFO logger=volt.runner msg="..."` (or JSON). Without `VOLT_LOG_DIR` everything
goes to stderr, which systemd keeps in the journal (`journalctl -u volt`). With it, each process writes its own
file and rotates it at `VOLT_LOG_MAX_MB`, keeping `VOLT_LOG_BACKUPS`. What you will see: `serve started`, one
line per failed or refused request, one `job start` and `job end` line per job with its outcome and duration,
`runner ... started`, and every restart `volt run` performs. Passwords in database URLs, bearer tokens,
`token=`/`password=`/`key=`/`sig=` values, cloud signatures and AWS key ids are blanked from every line,
including tracebacks. Redaction is a safety net: still do not put secrets in free text.

## Disk

Proxy builds and thumbnails are refused while the disk holding their output has less than `VOLT_MIN_FREE_MB`
free (ingest also needs room for the file itself). The refusal names the directory and the setting. A proxy or
ingest job waits and retries without using an attempt (`Defer`); a thumbnail request gets `507 disk_low`. Only
rebuildable files are ever deleted automatically: thumbnails beyond the cap, oldest first, and stale
`*.part.mp4` leftovers. Registered essences (hi-res, proxies, published files) are never touched; free that
space yourself.

## Backup and restore

Provided by `volt.backup` (`python -m volt.backup` does the same). Stop serve and the runner before a restore.

| Command | Does |
|---|---|
| `volt backup <db> <dir> [--with-media]` | Writes a new directory: the data file, `media.json` (closed essences and done versions), and `manifest.json` (schema version, row counts, SHA-256), written last. `--with-media` copies local files under `VOLT_PROXY_DIR`, `VOLT_QUARANTINE_DIR` and `VOLT_PUBLISH_DIR` into `<dir>/media/<asset_id>/`; a missing source is listed in `media.json` `missing` and is not a failure. Object storage is listed, not copied. SQLite uses the online backup API, so it is safe while Volt runs; Postgres needs `pg_dump`. `<db>` defaults to `VOLT_DB`. |
| `volt verify <dir>` | Checks a backup against its manifest without restoring. |
| `volt restore <dir> [db] [--overwrite]` | Restores the database into `db` (default `VOLT_DB`), migrating forward if the backup is older. Does not move media files back: copy them from `<dir>/media/` (or object storage) to the paths in `media.json`, then `volt verify-storage`. Old backups without `media.json` still restore. Refuses a non-empty target without `--overwrite`; Postgres needs `pg_restore`. |
| `volt verify-storage [db] [--deep]` | Read-only check that every file and published object the database references exists. Uses `VOLT_PROXY_DIR`, `VOLT_QUARANTINE_DIR`, `VOLT_PUBLISH_DIR` and `VOLT_STORAGE`. |

See `docs/specs/backup.md`. `volt check` reports whether `pg_dump` and `pg_restore` are available when the database is Postgres.

## Upgrade

1. Read `CHANGELOG.md` for the versions you are crossing.
2. Back up the database: `volt backup <db> <new-dir>` (see Backup and restore below).
3. Stop: `systemctl stop volt`, `Stop-ScheduledTask -TaskName Volt`, or Ctrl-C.
4. `git pull`, then `venv/bin/pip install -e "control[...]"` for the extras you use.
5. `volt check --env-file ...`. A `warn` on the database means it will migrate on start; a `fail` that the
   schema is newer than the build means you are running older code than the database was upgraded by: stop and
   use the newer build. serve, the runner and the clock also refuse to start against such a database (exit code
   78, so `volt run` does not retry), and `volt restore` of a backup made at that version is the way back.
6. Start. Migrations run once, at the first start, inside a transaction. They are forward-only: there is no
   downgrade, so a rollback means restoring the backup taken in step 2.
7. Confirm with `/health` (version) and `/api/health` (`status`, `schemaState` current).

Run the new version on every machine that shares a database; a runner on older code against a newer database
is not blocked from starting.

## Not done yet (decisions and work outside this page)

- **License.** Decided 2026-11-01: proprietary, all rights reserved (`LICENSE`, SPDX `LicenseRef-Proprietary`). Nobody
  outside the owner has a right to install or redistribute Volt without a written agreement. `NOTICE` lists ffmpeg,
  faster-whisper, the optional SDKs and their licences; it is not a grant for Volt. Still open: a customer licence
  agreement and, if a bundle ships psycopg, the LGPL-3.0 steps in `NOTICE`.
- **Auth.** Per-user tokens with three roles (viewer, operator, admin; see "Users and tokens" above), groups, per-show
  access, cookie sessions and sign-in with Google or Microsoft, plus the legacy shared `VOLT_TOKEN` as an admin named
  `desk`. Writes append to `audit` with the user as the actor (`GET /api/audit` is the admin view). Sign-in has only been
  run against a fake provider, and edit leases, presence and comments have only been run by tests and one person with two
  browser profiles: see [collaboration](specs/collaboration.md) for exactly what is unvalidated. No passwords, SAML or SCIM.
- **TLS.** Volt speaks plain HTTP by design. The supported setup is a Caddy or nginx reverse proxy on the same machine with Volt on loopback and `VOLT_TOKEN` set: [tls](tls.md) has the configs and the run that checked them (Caddy 2.11.7, nginx 1.31.8, internal certificate; public-certificate issuance and HTTP/2 not run).
- **Scheduling backups.** `volt backup` exists; nothing runs it on a timer. `--with-media` copies local owned
  folders; object storage is still yours to copy (docs/specs/backup.md). Run a restore drill once before relying on it.
- **Not exercised on real systems:** the Task Scheduler registration and the systemd unit were reviewed and the
  installer's plan was tested with `-DryRun`, but neither was installed on a live machine.

## Sign-in with Google or Microsoft (unvalidated against a real provider)

Nothing here has been run against Google or Microsoft. The flow, the checks and the claim handling are tested against a fake provider only, and what the real providers send (`hd`, `tid`, `email_verified`, the group overage marker, key rotation timing) is from published behaviour as recalled. Try it on a test tenant before relying on it. Tokens keep working and are the recovery path.

It needs TLS (`docs/tls.md`): Google refuses plain-http redirect URIs except on localhost, so a plain-http LAN install keeps tokens only. Register `VOLT_PUBLIC_URL` + `/auth/callback` as the redirect URI at the provider.

`VOLT_OIDC_CONFIG` names a JSON file:

```json
{"providers": [
  {"id": "google", "label": "Google", "issuer": "https://accounts.google.com", "client_id": "...",
   "client_secret_env": "VOLT_GOOGLE_SECRET", "domains": ["example.com"], "auto_provision": false},
  {"id": "microsoft", "label": "Microsoft", "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0", "client_id": "...",
   "client_secret_env": "VOLT_MS_SECRET", "tenants": ["<tenant-id>"], "group_claim": "groups"}
]}
```

The secret is read from the named environment variable and is never in the file, the log or the audit (a `client_secret` key in the file is refused). `scopes` defaults to `openid email profile`; `offline_access` is refused. Microsoft needs one tenant issuer (`common`, `organizations`, `consumers` are refused) and a `tenants` list; Google's `auto_provision` needs `domains`. **An unknown Google or Microsoft sign-in is refused until an admin adds the user** (owner ruling 2026-11-03): with no `auto_provision` key, or `"auto_provision": false`, a first sign-in from anyone the file does not already know ends at `not_provisioned` ("Ask an administrator to add you"), writes a `login_failed` audit row, and creates no user, no identity link and no session. The admin adds the user and sets their address with `POST /api/users/{name}/identity {email}`; the next sign-in links by that address. `auto_provision` exists only as an explicit opt-in (it needs `domains` for Google): set to `true` it creates a `viewer` for an unknown, trusted email. It is off in every example here and nothing turns it on by default.

Who may sign in: a known `(issuer, sub)` always signs in as the same user, whatever email it now carries. An unknown identity is linked to an existing, enabled, not-yet-linked user whose address equals the token's, only when the address can be trusted (Google: `email_verified` and `hd` in `domains`; Microsoft: `tid` in `tenants`); otherwise it is refused as `not_provisioned`. Groups come from the token's group claim through the map an admin sets (`POST /api/groups/{name}/claims`); they replace only oidc-sourced memberships, and a Microsoft overage marker leaves the groups as they were (a log warning; Graph is not called). Membership changes at the provider apply at the next sign-in, bounded by the session limits. Every refusal writes `login_failed` (insert-only; at most one row a second per provider and reason, the skipped ones counted in the next row) and makes no session. Provider endpoints are called with 10 second timeouts, https only, no redirects.

## Editing together, comments and notifications (unvalidated with real users)

No configuration. Leases, presence, comments and notifications use their own tables (migrations 33 and 34) and nothing on the air path (take, go, hold, release, skip, kill) reads them; if they cannot be read the page says "collaboration unavailable" and carries on. An edit lease lasts 45 seconds and the page renews it every 15, so a closed laptop frees an item within a minute; an admin or an operator with edit access can take over a live lease. Presence is a 10 second heartbeat per browser tab, live for 30 seconds. Comments are 4000 characters at most, 30 a minute per author, and notifications older than 90 days are purged by the runner. Access follows the show: nobody who cannot view a show can read, post or be notified about its comments. Comment bodies are never in the audit table, the log or a URL.

Back up the database before upgrading as for any migration. These features have been exercised by tests on SQLite and Postgres and by one person in two browser profiles against a loopback server; they have not been used by a real team, or over a flaky network, or on a LAN with a proxy in front. See [collaboration](specs/collaboration.md).
