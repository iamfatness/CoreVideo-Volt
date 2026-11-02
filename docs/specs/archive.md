# Archive tier

Status: built (`volt/archive.py`, migration 20). Moves an asset's hi-res essence to a cold object store and back. Proxy and thumbnails stay local on purpose, so an editor can still browse an archived asset.

## What it does

- **Archive** (`POST /api/assets/{id}/archive`, operator role, audited): queues an `archive` job. The runner hashes the hi-res file, uploads it to the archive store under `archive/<asset>/<essence><ext>` with the store's own verification, checks that what arrived has the same size and sha256, moves the object to the cold class, **records** `essence.tier = 'archived'`, `archive_key`, `archive_sha256` and `asset.archived_at` in one transaction, and only then **deletes the local file**. Every failure before the record leaves the local file untouched; a crash after the record and before the delete leaves two copies, and running the job again finishes the delete. There is never a moment with zero copies.
- **Restore** (`POST /api/assets/{id}/rehydrate`; `/restore` is taken and un-deletes a tombstoned asset): queues a `restore` job. The runner asks the store to rehydrate (once), waits (the job is deferred without using an attempt), downloads to `<location>.restore`, checks the sha256 against `archive_sha256`, then moves it into the essence's own path and sets the tier back to `online`. A download that does not match is never put in place.
- **Refusals.** Archive is refused (409 `archive_refused`, with `reasons`) when the asset is deleted, under legal hold, has no hi-res, is still recording, or is already archived; a hold placed after the request stops the job. Restore is refused when the hi-res is not archived.
- **While cold** (`archived` or `restoring`) the hi-res cannot air, publish, be proxied or be exported: `Store.assert_publishable` raises `ArchivedError` (409 `archived` on take and publish; the item's media shows `blockedBy: "archived"`), `record.build_proxy` raises it, an hi-res NLE export is refused with `archived` (the proxy export still works), and the runner defers proxy, ASR, publish, span-frame and detection jobs for that asset instead of failing them.
- **Delete and purge.** A purge of an archived asset also deletes the archived object (audited); with no archive store configured the purge is `partial` and retried.

## Where it goes

`VOLT_ARCHIVE_DIR` (a dedicated folder, any mounted disk or NAS), else the configured object store (`VOLT_STORAGE`: S3, Azure Blob or GCS), else a folder under the publish directory. `VOLT_ARCHIVE_CLASS` overrides the cold class; the defaults are S3 `DEEP_ARCHIVE`, Azure `Archive`, GCS `ARCHIVE`, and `cold` for the local simulation.

Backends gained four methods: `get_file`, `set_storage_class`, `restore` and `restore_state` (`online`, `archived`, `restoring`). S3 moves the class with a managed copy and restores with `restore_object`; Azure sets the blob access tier and rehydrates to Hot; GCS changes the class (reads stay immediate, so a restore reports `online` at once); the local backend simulates the states with a marker file beside the object.

## UI

The Library asset header shows an Archived or Restoring chip and, for a local, closed, un-held asset, an Archive button that needs two presses; an archived asset shows Restore. A job in flight shows Archiving or Restoring instead of a button; a failed job leaves the button available.

## What the tests prove, and what they do not

Proved with fakes: the state machine, the never-zero-copies ordering (put and class-change failures, a crash before the delete, a store that returns a wrong digest, a corrupt download), refusals and their codes, idempotence, per-backend call shapes (mock clients) and the local backend's behaviour, the runner's deferral, purge, and the UI wiring. **Not proved:** real retrieval times, cost or restore semantics on a live S3, Azure or GCS account; those depend on the provider and the tier.
