# Retention: delete, restore, purge

`Store.delete_asset` queues a `delete` job. The runner's handler now exists, and it only does the first of two steps. Code: `control/volt/retention.py`. Tests: `control/tests/test_data_safety.py`.

## Step 1: soft delete (tombstone)

`asset.deleted_at` is set. The asset disappears from the library list, transcript search, the operator asset and span lists and the transcription backlog, and `assert_publishable` refuses it. Nothing on disk or in storage is touched, and no row is removed. `restore` clears the mark (`POST /api/assets/{asset}/restore`, or `python -m volt.retention <db> restore <asset>`).

Triggers: the runner `delete` job (from `Store.delete_asset`) or `POST /api/assets/{asset}/delete` (synchronous; same checks, `409 delete_refused` with `reasons`).

## Safeguards

Checked when the mark is set, and again when the purge runs (a hold or an attachment can arrive in between). Every reason found is listed; a refusal changes nothing and fails the job with the reasons (`Permanent`, no retries).

| Refused when | Reason text |
|---|---|
| `legal_hold` is set (`Store.delete_asset` also raises `HoldError` before queueing) | `legal hold` |
| `retention_class` is `permanent` | `retention class 'permanent' is never deleted` |
| any essence is open (still recording) | `still recording (an essence is open)` |
| any rundown item uses one of the asset's spans | `attached to N rundown item(s)` |
| the asset has any `version` row (published or queued) | `has N publish version(s)` |
| purge only: a job is `running` on it (other than `delete`) | `N job(s) are running on it` |

Because a publish version blocks a delete, an asset that has been published cannot be deleted today; there is no unpublish yet. A purge can be told to remove versions through the storage seam (`--include-published`, below), but only for an asset that was already tombstoned.

## Step 2: purge

Runs after the retention window: `VOLT_PURGE_AFTER_DAYS` (default 30), or `VOLT_PURGE_AFTER_DAYS_<CLASS>` for one `retention_class` (for example `VOLT_PURGE_AFTER_DAYS_NEWS=90`); `0` means as soon as it is tombstoned. Run it three ways:

- the runner: `VOLT_PURGE_EVERY=<seconds>`. It is **off by default**; nothing is purged automatically unless you turn it on;
- `python -m volt.retention <db> purge [--older-than-days N] [--asset ID] [--include-published]` (exit 1 if any asset was left partial);
- `retention.purge_due(store, roots)` / `purge_asset` from code.

### What a purge deletes

Only what Volt owns, and nothing else:

| Item | Deleted when |
|---|---|
| Proxy essence | the file is under `VOLT_PROXY_DIR` and its name starts with the asset id |
| Hi-res copy made by watch-folder ingest | the file is under `VOLT_QUARANTINE_DIR` and its name starts with the asset id |
| Thumbnails | the directory `VOLT_OUT/thumbs/<asset>` |
| Published versions | only with `--include-published` / `allow_versions=True`: `backend.delete(key)` through the storage seam (local, S3, Azure, GCS), and only when the configured backend's `id` equals the one recorded on the version |
| Database rows | last: `asr_chunk`, `asr_run`, `shot_run`, `job`, `version`, `span`, `essence`, then `asset`, in one transaction |

Never deleted, each recorded as `left_in_place` with the reason: a registered external file (a hi-res named by `record_start`, or anything outside the owned folders), a file not named for the asset, a file another asset's essence also points at, and every file when no owned folder is configured. A `..` or symlinked parent that resolves outside the root is outside it. A directory is never removed except the thumbnail directory.

### Partial failure and resume

Files are removed first, rows last. If any removal fails, the rows stay, the asset stays tombstoned, the result is `partial` (logged; `purge_due` carries on with the others), and the next pass retries: a file already gone counts as `already_missing`, not an error. Two purges of one asset are safe: the second finds nothing left (`KeyError`) or finishes the job.

### Audit

`purge_audit` (migration 11) has one row per object touched and one summary row per purged asset (`kind = 'asset'`, JSON `detail` with owner, show, source key, class, tombstone time and counts). Columns: `purged_at`, `asset_id`, `kind` (`essence`, `thumbs`, `version`, `asset`), `ref`, `role`, `location`, `outcome` (`deleted`, `already_missing`, `left_in_place`, `failed`, `purged`), `detail`. It has no foreign key, so it outlives the asset. Rows within one second have no defined order.

## Not proven

- Real S3, Azure and GCS deletion of a purged version: only the local backend and a failing local subclass were exercised.
- Large data: a purge walks one asset's rows in memory; the tests use a handful of files.
- A hold set in the instant between the last safeguard check and the row delete is not prevented (the check is repeated inside the final transaction, which narrows the window to the file removals).
- Ingesting the same source key again while its asset is tombstoned returns the tombstoned asset (`find_by_source`); restore it first.
