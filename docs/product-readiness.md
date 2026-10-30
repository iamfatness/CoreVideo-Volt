# Product readiness audit

> Dated 2026-10-16 against `origin/main` at b3554ff. This page is that day's snapshot, not the current gap list. Foundation work in [foundation](specs/foundation.md) is in the tree. What is next is the parallel room track and the later list in [roadmap](roadmap.md).

Date 2026-10-16, against `origin/main` at b3554ff. Question: what does the repo have and lack to be installed and run every day by a small broadcast team?

Short answer: the control plane is well tested and honest about its limits. It is not yet a product. There is no installer, no service definition, no backup story, no health endpoint, no license, and no way to know which person did what. Those are operations gaps, not design gaps; most are small to medium.

**Update, same day (branch `ops-run-as-a-product`):** gaps 2 (start command, service files), 3 (health), 4 (logs), 5 (packaging, `volt` command, extras), 6 (disk floor and thumbnail cache cap, not the quarantine or publish folders), 10 (operator doc) and 12 (version, changelog) are addressed; the `--help` trap in gap 14 is fixed. Gaps 1, 7 and 9 were addressed by the backup, retention and version-skew work merged separately. Gap 8 (per-user tokens, two roles) is addressed on branch `per-user-tokens`. Open: 11 (license, not chosen), 13 (CI), TLS. See [operations](operations.md).

## How to read this

- **Ran** means I executed it on this Windows 11 box (Python 3.12.10, ffmpeg present, no psycopg, boto3 or faster-whisper installed).
- **Read** means I read the code, config or docs and did not run it.
- Status is **present**, **partial** or **absent**.

What I ran: the full contract suite (`python -m unittest discover -s tests`: 1078 tests, OK, 363 skipped, 319 s; the skips are the Postgres, cloud-SDK and live-service tests that need drivers or services this box lacks); an in-process server on port 0 against a temp database and temp `VOLT_OUT`; `python -m volt.runner --help`. I did not run Postgres, any cloud backend, a runner against real media, the clock against a core, or anything against the CoreVideo core on 8011. Nothing outside a temp directory was written.

One finding from running: `python -m volt.serve --help` does not print help. `serve.main()` has no argument parsing, so it starts a server on the default port 8765 against `control/volt.db`. I stopped it and deleted the file it made. It is a small trap for anyone who types `--help` (S).

## The audit

| Area | Status | Evidence |
|---|---|---|
| **Install and packaging** | | |
| Package metadata (`pyproject.toml`, `setup.py`) | absent | Read: no such file in the tree (`git ls-files`). The code runs from `control/` as `python -m volt.X`. |
| Entry points / console scripts | absent | Read: only `python -m volt.serve`, `volt.runner`, `volt.clock`, plus `publish` and `speech` modules that have a `__main__`. No `volt` command. |
| Single command to start the stack | absent | Read: three separate processes are needed for a full session: `volt.serve`, `volt.runner <db>` and `volt.clock <db>` (the clock needs `VOLT_ROOM_URL`). `control/README.md` shows serve and runner on separate lines; nothing starts them together. |
| Optional-extras story | partial | Read: `control/requirements.txt` pins `psycopg[binary]==3.3.6` and `psycopg_pool==3.3.3`; boto3, azure-storage-blob, azure-identity and google-cloud-storage are commented out and unpinned. SDKs import lazily and a missing one raises a named `StorageConfigError`. faster-whisper is not listed anywhere in requirements; the runner doc says set `VOLT_ASR_ENGINE=faster-whisper`. No extras groups, no lockfile. |
| ffmpeg / ffprobe | partial | Read and ran: found on PATH here. It is required for proxy, thumbnails, detection and publish. Install docs do not list it. `detect` answers `503 engine_missing` when it is absent; the other paths fail per job. |
| **Running as a service** | | |
| Service files (systemd unit, NSSM or Windows service, Docker) | absent | Read: no unit, wrapper, compose or Dockerfile in the tree. |
| Restart on crash | partial | Read: nothing restarts a process. The runner recovers jobs after its own death (lease expiry, attempt cap, `runner.md`), so no work is lost, but something must start it again. Graceful SIGINT/SIGTERM is handled in the runner; `serve` only catches `KeyboardInterrupt`. |
| Runner plus serve process model | present | Read: documented (`runner.md`, `architecture.md`); multiple runners are safe (atomic claim, `SKIP LOCKED`). Two `serve` processes are not: the `_SEQ` sequencing lock is in-process (`architecture.md` says so). |
| Log files and rotation | absent | Read: `serve` suppresses its access log (`log_message` returns) and prints tracebacks to stderr; runner and clock call `logging.basicConfig` to stderr at INFO; no file handler, no rotation, no log level setting. |
| Config via env vs file | partial | Read: environment and CLI flags only; no config file. 64 distinct `VOLT_*` names appear in `control/volt`. |
| Documented env vars | partial | Read: auth and server vars in `operator-api.md`, storage in `storage.md`, runner in `runner.md`, Postgres in `control/README.md`. No single reference. `VOLT_OUT` appears only in the operator API and shot-spans docs, and `VOLT_FW_PYTHON` appears in code with no doc. |
| **Backup and restore** | | |
| SQLite backup / restore | absent | Read: `grep -i backup` over docs and code finds nothing. `store.py` sets `journal_mode=WAL`, so a plain copy of the `.db` file alone, taken while the server runs, can miss committed data in the `-wal` file; no `sqlite3 .backup` or `VACUUM INTO` is documented. |
| Postgres backup / restore | absent | Read: not mentioned. |
| Object storage backup | absent | Read: `storage.md` covers delete and verify; no replication, versioning or restore guidance. Azure/GCS soft delete and versioning are listed as "not proven". |
| Proxy, publish and thumbnail directories | absent | Read: local paths (`VOLT_PROXY_DIR`, `VOLT_PUBLISH_DIR`, `VOLT_OUT`) with no statement of what is rebuildable (thumbnails are, `media.py` says so) and what is not. |
| **Schema upgrades** | | |
| Migrate on startup | present | Read and tested: `migrations.run` under a Postgres advisory lock at `serve` and `Store` open; `test_migrations.py` migrates each historical shape forward; CI runs it on SQLite and Postgres 16. |
| Downgrade | absent | Read: forward-only list; no down steps, none documented. |
| Version skew (runner / server / database) | absent | Read: `migrations.current_version` exists and nothing calls it. A runner or server on older code against a newer database runs anyway; an old build ignores unknown versions. The runner also upgrades the schema on open, so whichever starts first wins. |
| **Auth** | | |
| Operator token | present | Ran and read: `VOLT_TOKEN`, bearer, constant-time compare, 401 with `WWW-Authenticate`; guards GET data routes too (`operator-api.md`, `serve._gate`). |
| Required by default | partial | Ran: on loopback with no token the server answers with no auth (`/api/state` returned 200). Non-loopback bind without a 16+ char token refuses to start (`check_bind`). Loopback-without-token is the shipped default; fine for one box, wrong on a shared workstation. |
| Rotation | present | Per-user tokens rotate (`volt user rotate`) and disable (`volt user disable`) with effect on the next request, no restart; the legacy `VOLT_TOKEN` still needs a restart. Was: change the variable and restart. |
| Bind / LAN | present | Read: `VOLT_HOST`, default `127.0.0.1`. Ran: a foreign `Host` header with no token got 403 `bad_host` (DNS-rebinding guard). |
| TLS | absent | Read: plain `http.server`; no ssl import. A bearer token over LAN HTTP is readable on the wire. Needs a documented reverse proxy. |
| CORS / CSRF | present | Ran and read: no CORS header is sent; POST needs `Content-Type: application/json` (415 otherwise, ran); 64 KiB body cap; operator pages carry a strict CSP and `frame-ancestors 'none'`. |
| Multi-user and roles | present | Ran: per-user tokens with operator and admin roles (`volt user`, migration 17); the audit actor is the token's user; tests cover 403 `forbidden_role`, immediate revocation and the route-role table. Was: one shared token, no users. `shot-spans.md` open question 11 records "any token holder can delete or re-detect". |
| Audit trail of who did what | absent | Read: no actor on any write; NLE export logs span IDs and sha256 to stderr only (`operator-api.md`). `as_run_in/out` on items is a playout record, not an operator audit. |
| **Secrets** | | |
| Handling | present | Read: cloud credentials are never accepted by Volt; they come from the SDK chains (`storage.md`). Error text blanks `sig=`, `AccountKey=`, tokens. `serve` prints only "(token required)". |
| Storage of `VOLT_TOKEN`, `VOLT_ROOM_TOKEN`, Postgres DSN | partial | Read: plain environment variables. The DSN carries the password and is also a command-line argument to `volt.runner` and `volt.clock`, so it shows in process listings. No file-based secret (`*_FILE`) option. Presigned URLs stored on versions embed an access key id and act as bearer links (`storage.md`). |
| **Monitoring** | | |
| Health endpoint | absent | Ran: `/health`, `/healthz`, `/metrics` all `404`. `/api/state` works as a crude liveness check, and sits behind the token. |
| Metrics | absent | Read: none. |
| Job / queue depth, stuck jobs | partial | Read: job rows carry status, attempt, error and lease; the Library shows a detection and indexing job with `ageSeconds`. Nothing aggregates depth or alerts on a job stuck `running`, or on a dead runner. |
| As-run log export | partial | Read: as-run times are written on the item (`playout.py`). I found no export route or report. |
| **Resource limits** | | |
| Thumbnail cache | partial | Read: `VOLT_OUT/thumbs/<asset>/<span>-<frame>.jpg`, safe to delete, never pruned. A trim makes a new file name, so churn accumulates. Request time is capped (15 s). |
| Quarantine, part and temp files | partial | Read: part files are removed on failure (`record.py`, `deliverables.py`); orphans from a killed process are not swept. Quarantine copy (`ingest.py`) has no space check or expiry. |
| Disk-space guard | absent | Read: no `disk_usage` or free-space check anywhere in `volt/`. |
| Concurrency limits | partial | Read: lease, attempt cap, backoff, ASR idle release; `ThreadingHTTPServer` is unbounded; body cap 64 KiB. |
| **Retention and deletion** | | |
| Legal hold | present | Read and tested: blocks air and publish and `delete_asset` (`store.py`); export and marking stay allowed by owner ruling. |
| Asset deletion | partial | Read: `Store.delete_asset` only enqueues a `delete` job. The runner's handlers are `proxy`, `asr`, `publish`, `span-frames` and `shots` (`runner.default_handlers`); nothing claims `delete`. No API route calls it. The tests assert the job is queued. |
| Retention classes | partial | Read: `asset.retention_class` is stored (default `show`) and used nowhere else. |
| Essence cleanup in object storage | partial | Read: backends implement `delete`; nothing calls it for a deleted asset. |
| **Docs** | | |
| Developer docs | present | Read: seven areas, thirteen specs, honest "proven / not proven" sections. |
| Operator docs | absent | Read: `start-here.md` is a build order; `README.md` and `control/README.md` give a dev run. No install guide, no day-of checklist, no recovery runbook. |
| **Legal** | | |
| License | absent | Read: no LICENSE file, no license field. |
| Third-party notices | absent | Read: none. ffmpeg (LGPL or GPL by build), faster-whisper models and the optional SDKs carry terms a distributor must honour. |
| **Release** | | |
| Versioning | absent | Read: no `__version__` (`volt/__init__.py` has none); `git tag` in this clone lists none. |
| Changelog / release process | absent | Read: none; PR history is the changelog. |
| **CI** | | |
| Contract tests, Ubuntu and Windows (SQLite) | present | Read: `.github/workflows/test.yml`; Windows runs the same suite. |
| Postgres 16 | present | Read: `postgres` job with `VOLT_TEST_PG_DSN`. Other Postgres versions unproven (`architecture.md`). |
| Not in CI | absent | Read: Windows + Postgres; the cloud SDK paths (tests use fakes; SDKs are not installed in any job); faster-whisper; any install-from-scratch smoke test; the `bench/*_live_check.py` live scripts (S3, record, stream, cue); lint, type checks and dependency scanning; macOS. |

## Ranked gaps

Severity is for a team running a live show: **Critical** loses data or a broadcast, **High** stops a team operating without a developer on hand, **Medium** blocks wider adoption or trust. Effort: S is a day or less, M is a few days, L is a week or more.

| # | Gap | Severity | Effort | Why it ranks here |
|---|---|---|---|---|
| 1 | No backup or restore for the database, object storage or local essence folders, and no restore drill | Critical | M | The system of record is a file or a database nobody is told to copy. One disk failure ends the archive. |
| 2 | No single start command and no service definitions or restart on crash (Windows and Linux); three processes to babysit | Critical | M | A crashed `serve` during a show stays down until someone notices. Runner work survives; the operator surface and the clock do not. |
| 3 | No health endpoint, queue depth or stuck-job signal | High | S | The first sign of a dead runner is a clip that never gets a transcript. Add `/health` (unauthenticated, no data) plus job counts and oldest-running age. |
| 4 | No log files, rotation or level control; access log suppressed | High | S | After an incident there is nothing to read. |
| 5 | No packaging: no `pyproject.toml`, entry points, extras, lockfile; ffmpeg and faster-whisper not in the install story | High | M | Every install is a hand-built environment, and the cloud SDKs are unpinned. |
| 6 | No disk-space guards or cleanup (thumbnail cache, quarantine, orphan part files, publish folder) | High | M | A full disk fails recording wrap and publish at the worst moment. |
| 7 | Asset deletion and retention do not run: no handler for `delete`, no route, `retention_class` unused, storage objects never removed | High | M | The legal-hold promise is half of a feature; a team will assume delete works. |
| 8 | ~~No audit trail, one shared token, no roles~~ Addressed: audit table, actor, and per-user tokens with operator/admin roles | Medium | L | Fine for one trusted room; not for a team where "who took that item" matters. Start with an actor header and an append-only table, then per-user tokens. |
| 9 | No schema version-skew guard | Medium | S | A rolled-back server or old runner against a migrated database fails in strange ways. Refuse to start when the database is newer than the code. |
| 10 | No operator install and runbook docs | Medium | M | The specs are written for builders. |
| 11 | No license or third-party notices | Medium | S | Blocks any install outside this owner. |
| 12 | No version number, changelog or release process | Medium | S | Support requests cannot name the build. |
| 13 | CI gaps: Windows plus Postgres, packaging smoke test, cloud SDKs installed, a scheduled live check | Medium | M | The Windows runner cannot host a Postgres service; a self-hosted or `pip install` job on Windows against a service elsewhere would close it. |
| 14 | No TLS and no documented reverse proxy; `--help` starts a server | Medium | S | The token crosses the LAN in the clear. |

## Recommended order

1. **Visibility first, because it is cheap and everything else leans on it.** Gaps 3, 4 and 9 (health, logs, version guard). About two days together. They also make the next steps testable.
2. **Make it survive.** Gap 2 (a start command and service files) with gap 5 (packaging and a `volt` entry point), since the service files need a stable executable name. Then gap 1 (backup and restore), with the drill actually performed and recorded. Do not call the product ready before a restore has been run once.
3. **Make it safe to leave running.** Gaps 6 and 7 (disk guards, deletion and retention), then 14 (TLS recipe).
4. **Make it a product someone else can install.** Gaps 10, 11, 12 and 13.
5. **Make it a team tool.** Gap 8, last because it is the largest and the single-token model is acceptable on one trusted LAN with TLS in front.

## What I could not check

- Postgres in any form (not installed here; read the CI job only), and any backup behaviour of either database.
- Real S3, Azure or GCS from this box; boto3 and the other SDKs are not installed here.
- Running the runner on real media, faster-whisper, the clock, or anything against a live CoreVideo core. I did not touch port 8011.
- The Windows service model: no service wrapper exists to test, so "restart on crash" is judged from code.
- The operator UI in a browser; I only fetched its static files over HTTP.
- Behaviour under load or on a full disk.
- Whether a GitHub release exists on the remote; I checked local tags and version strings only.
