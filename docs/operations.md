# Operations

For the person who installs and runs Volt. Developer docs are in [architecture](architecture.md) and
[start-here](start-here.md); what is still missing before more MAM work is in
[foundation](specs/foundation.md). [product-readiness](product-readiness.md) is a dated audit (2026-10-16), not the current gap list.

## What runs

| Process | Command | Needs | What it does |
|---|---|---|---|
| serve | `volt serve` | database | Operator API, `/` operator desk, health. Two processes serialise a take on item status `taking`. |
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

Third-party terms for ffmpeg, faster-whisper, and the optional SDKs are in `NOTICE`. There is no LICENSE for
Volt until the owner names an SPDX id.

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
| `VOLT_TOKEN` | none | Operator bearer token. Required (16+ characters) to bind beyond loopback. With no token the server answers loopback `Host` headers only. POSTs may send `X-Volt-Actor` (default `desk`). |
| `VOLT_MEDIA_KEY` | none | HMAC key for media tickets. Unset: tickets are signed with `VOLT_TOKEN`. |
| `VOLT_ROOM_URL` | none | CoreVideo Pro core for cues, recording and the clock. Without it room-backed routes answer `no_room` and `volt run` skips the clock. |
| `VOLT_ROOM_TOKEN` | none | Token for that core, if it needs one. |
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

### Postgres pool

Off by default. `VOLT_PG_POOL_MAX` (> 0 turns it on; use 2 or more), `VOLT_PG_POOL_MIN`, `VOLT_PG_POOL_TIMEOUT`,
`VOLT_PG_POOL_MAX_IDLE`, `VOLT_PG_POOL_MAX_LIFETIME`.

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
quarantine, scratch and database directories), `thumbCache`, `storage`.

`degraded` means a person should look: jobs are waiting and no runner has checked in, a job has an expired lease
or has run longer than `VOLT_STUCK_JOB_SECONDS`, a directory is under the disk floor, the schema is behind or
newer than this build, or the storage configuration is invalid. The storage check does not call the cloud: it
proves the configuration parses and the SDK imports, so a provider outage does not hang the endpoint.

Metrics: `volt_up`, `volt_info{version}`, `volt_degraded`, `volt_schema_version`,
`volt_runner_heartbeat_age_seconds`, `volt_runners_alive`, `volt_jobs{type,state}`,
`volt_queue_oldest_queued_age_seconds`, `volt_jobs_stuck`, `volt_last_as_run_age_seconds`,
`volt_disk_free_bytes{dir}`, `volt_disk_low{dir}`, `volt_thumb_cache_bytes`, `volt_storage_ok{backend}`.
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

- **License.** There is no LICENSE file: the owner has not chosen one. Until then nobody outside the owner has a
  right to install or redistribute Volt. `NOTICE` lists ffmpeg, faster-whisper, and the optional SDKs; it is not
  a grant for Volt.
- **Auth.** One shared bearer token, no users or roles. Operator writes take `X-Volt-Actor` (default `desk`) and
  append to `audit`; `GET /api/audit?target=` is the read.
- **TLS.** Plain HTTP. Put a reverse proxy in front for anything beyond one machine.
- **Scheduling backups.** `volt backup` exists; nothing runs it on a timer. `--with-media` copies local owned
  folders; object storage is still yours to copy (docs/specs/backup.md). Run a restore drill once before relying on it.
- **Not exercised on real systems:** the Task Scheduler registration and the systemd unit were reviewed and the
  installer's plan was tested with `-DryRun`, but neither was installed on a live machine.
