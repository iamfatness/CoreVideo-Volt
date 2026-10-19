# Backup, restore and storage verification

`python -m volt.backup --help`. Functions for a `volt` umbrella command: `backup(source, dest)`, `verify_backup(dir)`, `restore(dir, target, overwrite=False)`, `verify_storage(store, backend, ...)`, and `main(argv)`. Code: `control/volt/backup.py`. Tests: `control/tests/test_data_safety.py`.

## What a backup is

A directory with the data file, `media.json`, and `manifest.json`, written last: no manifest, not a backup. A backup never writes into a directory that already has files.

Manifest: `format`, `kind` (`sqlite` or `postgres`), `file`, `created_at`, `source` (path or DSN without the password), `schema_version` (what the database held), `code_schema_version` (what the code writing it knows), `row_counts` (every table), `sha256` and `size` of the data file, and for SQLite `integrity_check: ok`.

`media.json` names every closed essence (`id`, `role`, `location`, `checksum`) and every `done` version (`id`, `backend`, `key`, `sha256`). That is the inventory of bytes the restore will need; restore does not invent files.

`volt backup --with-media` copies local hi-res, proxy and published files whose paths sit under `VOLT_PROXY_DIR`, `VOLT_QUARANTINE_DIR` or `VOLT_PUBLISH_DIR` into `<dest>/media/<asset_id>/`. Object storage is listed, not copied. A source that should have been copied but is not there is listed in `media.json` `missing: []` and is not a backup failure. Files outside those three directories are listed only. Thumbnails under `VOLT_OUT` are a rebuildable cache. `verify-storage` after restore is the check that the bytes are back where the database points.

## SQLite

`backup <db> <dir>` uses the SQLite online backup API, so it is a consistent snapshot while `serve` and the runner are writing (WAL included; a test writes during three backups). The copy is checked with `PRAGMA integrity_check`, its schema version and row counts are read back into the manifest, and it is saved as a single file (journal mode DELETE).

## Postgres

`pg_dump --format=custom` and `pg_restore --single-transaction`, found on `PATH`, in the usual install folders, or from `VOLT_PG_DUMP` / `VOLT_PG_RESTORE`. Without them it refuses with a message and writes nothing. The password goes in `PGPASSWORD`, never on the command line, and is scrubbed from error text. The dump is the whole database named by the DSN; the manifest counts come from the connected schema.

## Verify

`verify <dir>` checks, without restoring: manifest present and well formed, data file size and SHA-256, and for SQLite `integrity_check`, schema version and row counts against the manifest. For Postgres it also runs `pg_restore --list` when the tool exists. A flipped byte, a truncated file, a missing manifest or an edited manifest is refused.

## Restore

`restore <dir> <db>` verifies first, then:

- refuses a backup whose schema is newer than this code (`SchemaTooNewError`);
- refuses a target that holds any rows, or is not a readable SQLite file, unless `--overwrite`. With it, a SQLite file is moved aside to `<db>.pre-restore-<time>` (with its `-wal`/`-shm`), never deleted; a Postgres restore uses `--clean --if-exists`;
- SQLite is restored to a temporary file beside the target, migrated forward there (an older schema runs the missing migrations), integrity-checked and row-count-checked, and only then renamed into place, so a failure leaves the target as it was. Postgres is restored in one transaction, then migrated and counted;
- stop `serve` and the runner first. Restoring under a running process is not detected.

A restore of an older backup reports which migrations ran (`migrated`). Restore does not move media files back; put them where `media.json` `location` / `key` say, then `volt verify-storage`. If `media.json` is absent or unreadable (an old backup), restore ignores it and still restores the database.

## Verify storage (read only)

`verify-storage <db> [--deep]` lists every essence location and every `done` version the database references and checks it. Settings come from the usual `VOLT_STORAGE`, `VOLT_PUBLISH_DIR`, `VOLT_PROXY_DIR`, `VOLT_QUARANTINE_DIR`. It reports:

- **missing**: a local essence file or a stored object that is not there;
- **empty**: a zero-byte file the database says exists;
- **mismatched**: a version whose backend SHA-256 differs from `version.storage_sha256`; with `--deep`, a closed local essence whose SHA-256 differs from its stored checksum;
- **orphaned**: objects the backend can list (local, S3) that no version references, and files in the proxy and quarantine folders no essence references;
- **errors**: a backend call that failed, with the reason.

Size is checked only as "exists and is not empty": the database stores no expected size. The SHA-256 is compared only where the backend exposes one (local, S3 single-part, Azure written by Volt); GCS and S3 multipart objects get existence and size only, and the report says so. Exit code 1 if anything is missing, empty, mismatched or unreadable. It writes and deletes nothing.

## Version-skew guard

Every `Store` open, `serve` and `runner` start runs `migrations.run`, which now raises `SchemaTooNewError` (naming both versions) when the database records a schema newer than the code knows, before changing anything. `serve` exits with the message; the runner exits 3. An older database migrates forward as before.

## Not proven

- Postgres backup ran against a throwaway `postgres:16` container through a wrapper that ran the container's own `pg_dump` and `pg_restore` 16; not with client tools installed on the host.
- No real S3, Azure or GCS `stat` or `list_keys`: fakes only. Azure and GCS cannot be listed for orphans.
- Large databases and long dumps (the dump has a one-hour timeout).
- Restore under a running server, point-in-time recovery and off-site copies are out of scope. Copy the backup directory somewhere else, and practise a restore.
