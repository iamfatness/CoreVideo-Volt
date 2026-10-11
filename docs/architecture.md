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

`volt.serve` opens one connection per request. It applies the schema once at startup, under a Postgres advisory lock so concurrent starters do not collide.

What is proven: the full contract suite (76 tests, run with `Store()` redirected to a Postgres 16 container) passes on Postgres except one SQLite-only trace test; `tests/test_postgres.py` holds the live tests, which run only when `VOLT_TEST_PG_DSN` is set and skip otherwise; `volt.serve` answered `/assets` from Postgres. What is not proven: write concurrency beyond schema bootstrap (no Postgres version of the threaded mint test), connection pooling, migrations of an existing Postgres database (`_migrate` is SQLite only; new columns need an `ALTER` in `schema.sql` or a migration step), and any Postgres other than 16.

Jobs are run by `volt/runner.py` (`python -m volt.runner <db or dsn>`): atomic claim (`FOR UPDATE SKIP LOCKED` on Postgres), lease with crash recovery, retry with backoff and a cap, and `wrap` queues the final proxy and the transcript. See `docs/specs/runner.md`.
