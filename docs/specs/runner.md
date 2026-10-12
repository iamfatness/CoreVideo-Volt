# Job runner

Nothing used to claim queued jobs. `volt/runner.py` does: `python -m volt.runner <db or dsn> [--once]`.

## Claim

One atomic `UPDATE ... RETURNING` takes the oldest due `queued` job of a handled type and marks it `running` with `claimed_by` and `lease_expires_at`. On Postgres the candidate subselect is `FOR UPDATE SKIP LOCKED`; on SQLite the single statement holds the write lock. Two runners never take one job.

Only runner-owned keys are claimable for two types, because the functions behind them write their own short-lived `queued` rows: `publish` jobs need the key prefix `publish-request:<span id>` (use `request_publish`), `asr` jobs the prefix `asr:`. Rows written by `publish()` (`publish:<span>:<kind>`) and by a hand-run `transcribe()` (`asr-v2:...`) are never claimed.

## Lease and crash recovery

The lease is renewed by a heartbeat thread (own connection) every third of `--lease` (default 120 s) while a handler runs. Every claim cycle first re-queues `running` jobs whose lease has lapsed. A lost run counts as an attempt, so a job that kills its runner every time reaches the cap and stays `failed` with `lease expired`. Every outcome write is guarded by `claimed_by` and `status = 'running'`, so a runner that lost its lease cannot overwrite the new owner. Delivery is at-least-once; handlers are idempotent per asset.

## Outcomes

| Handler | Result |
|---|---|
| returns | `done` |
| raises `Defer` | back to `queued` after a delay, no attempt used (error holds the reason) |
| raises `Permanent` | `failed` now (bad config, missing input) |
| raises anything else | `queued` again after `5 s * 2^(attempt-1)` (cap 300 s), until `--max-attempts` (default 5); then `failed` |

The last error is always left on `job.error`. `job` gained `claimed_by`, `lease_expires_at` and `run_after`; `attempt` is the number of the current or next run.

## Chaining

`record.wrap()` queues the follow-on work when it closes the file (and again, harmlessly, if called on an already closed asset, which heals a crash between closing and queueing): `proxy` key `proxy-final:<asset>` and `asr` key `asr:<asset>`. They read the closed hi-res and do not depend on each other, so they are queued together rather than proxy-then-asr; a failed proxy does not hold up the words. A wrapped asset becomes searchable with no manual call. Publishing is requested, not chained.

## ASR and consent

The engine comes from `VOLT_ASR_ENGINE=faster-whisper` (plus `VOLT_ASR_MODEL`, `VOLT_ASR_DEVICE`) or `--asr-engine`. With none configured the asr job fails at once with `no ASR engine configured`; it is never skipped. An asset that is not cleared is deferred (`waiting: asset is not cleared`) and runs by itself once cleared. Legal hold does not block transcription. Publish of an uncleared or held asset fails with the reason; the operator requests it again after clearing.

## Config

`VOLT_DB`, `VOLT_PROXY_DIR`, `VOLT_PUBLISH_DIR`, `VOLT_ASR_ENGINE`, `VOLT_ASR_MODEL`, `VOLT_ASR_DEVICE`, `VOLT_ASR_CORRECTIONS`, or the matching flags.

## Stop

SIGINT or SIGTERM (or `Runner.stop()`): the job in flight finishes and is recorded, no new job is claimed, the process exits 0. `--once` drains every due job and exits; it returns 1 if any job ended `failed` in that run.

## Two proxy builds of one asset

The record-start proxy (`proxy:<source>`) and the final proxy (`proxy-final:<asset>`) are separate jobs, so two runners can build the same asset's proxy at once. Job keys and leases are unchanged. `record.build_proxy` now has ffmpeg write a private temp name (`<asset>.<token>.part.mp4`) and renames it over `<asset>.mp4` with `os.replace`, so two runners never write the same path, a reader sees the old proxy or a complete new one, and a failed or killed build leaves the old file alone (before, `-y` truncated the live proxy and a failed build left a partial file that counted as success). The proxy essence row is checked and inserted under a per-asset lock (`proxy-essence:<asset>`), so two builds leave one row. Tests: `tests/test_race_proxy.py`.

## Not covered

A manually re-enqueued failed job past the attempt cap gets one more run.

A publish claim is renewed while the renderer runs. A second proxy build of one asset does not encode, and an open-file build that finishes after wrap does not replace the final proxy. A second transcribe of one asset does not start the engine.
