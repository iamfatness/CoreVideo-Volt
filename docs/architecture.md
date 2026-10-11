# Architecture

One session. One asset ID. Orchestration, distribution, transcoding, storage, and MAM share it.

```text
Session
  rundown · library · proxy player · publish
        │
Control plane
  asset · essence · span · item · cue · version · job
        │
Workers
  ingest · proxy · ASR · deliverable · cue dispatch
        │
Storage
  quarantine · hi-res · proxy · deliverables
        │
CoreVideo
  record start · inputs · graphics · audio snapshots
```

The control plane is the system of record. Workers can be swapped. CoreVideo is the room the session cues.

An asset ID is minted when a recording starts or a card is quarantined. Premiere, a rundown item, a cue, and a publish version hold that ID.

SQLite is the default and runs the contract tests. Postgres is the same objects: `Store` takes a `postgres://` DSN (or `VOLT_DB` for `volt.serve`) and applies `control/schema.sql` on first connect.

The seam is `control/volt/pg.py`, a connection adapter that gives psycopg 3 the surface the modules already use on a sqlite3 connection: `?` placeholders, rows by name or index, `commit`/`rollback`, and the backend's `IntegrityError` as `store.IntegrityError`. Timestamps are written and read as `YYYY-MM-DDTHH:MM:SSZ` text on both backends (TIMESTAMPTZ on Postgres). Flags are written as Python bools and compared with `TRUE`/`FALSE` in SQL, never `0`/`1`. `psycopg` is imported only when a DSN is used, so SQLite needs no install.

`volt.serve` opens one store per request. It applies the schema once at startup, under a Postgres advisory lock so concurrent starters do not collide. With `VOLT_PG_POOL_MAX` set, a `psycopg_pool` pool (imported only then) backs those stores: a connection is health-checked on checkout, so one the server dropped is replaced and never handed out; a returned connection is rolled back; `volt.serve` and `volt.runner` close the pool on exit.

**Transactions.** psycopg opens a transaction on a bare SELECT and holds it until commit, which the read-only paths never reached; the open transaction held locks and blocked another connection's schema bootstrap (found by A1). `PgConnection` now ends the transaction itself after a plain read with no write pending. A read-modify-write goes through `Store.transaction(lock)`: one atomic unit, serialized per lock name with a Postgres advisory lock (SQLite takes `BEGIN IMMEDIATE`), commits from the code inside held until the block ends. Used for next-position inserts (`add_item`, `add_cue`), `reorder`, `take`, `advance`/`go`, `skip`. `enqueue` and `hold` are single conditional statements instead (`ON CONFLICT DO NOTHING`, `WHERE held_at IS NULL`).

**Migrations.** `volt/migrations.py` holds one numbered list for both backends and a `schema_version` table. Base tables first (`CREATE TABLE IF NOT EXISTS`), then the legacy `pg._ADDED_COLUMNS` list, then each numbered entry not yet recorded, all in one transaction under the bootstrap lock. A column that already exists costs a catalog read and no ALTER, so a steady-state start takes no table lock. To change the schema: update `store.SCHEMA` and `schema.sql` (same column order) and add one `Migration` at the end of `MIGRATIONS`.

What is proven: the full suite on Postgres 16, including threaded races on both backends (mint, enqueue, job claim, rundown positions, reorder, go, take, hold), the pool (bounded connections, dropped-connection replacement, timeout, clean close), and each historical schema shape migrating forward; CI runs it on every push (`postgres` job). What is not proven: any Postgres other than 16, pool behavior across a real server restart (only a terminated backend), publish of one span from two writers at once (version rows rely on the unique constraint and the job state, not a lock), and `kill` racing `go` (the API serializes them in-process with `_SEQ`; two server processes would not).

Jobs are run by `volt/runner.py` (`python -m volt.runner <db or dsn>`): atomic claim (`FOR UPDATE SKIP LOCKED` on Postgres), lease with crash recovery, retry with backoff and a cap, and `wrap` queues the final proxy and the transcript. See `docs/specs/runner.md`.
