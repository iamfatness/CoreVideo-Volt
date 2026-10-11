# Control plane

MAM, storage, transcoding, orchestration, and distribution on one asset ID.

```bash
cd control
python3 -m unittest discover -s tests -v
python3 -m volt.serve
```

The session is at http://127.0.0.1:8765. Postgres tables are in `schema.sql`.

SQLite is the default. To run on Postgres, `pip install -r requirements.txt` and set `VOLT_DB=postgresql://user:pw@host/db` (the schema is applied on start). For many requests or several runners, set `VOLT_PG_POOL_MAX=8` (and optionally `VOLT_PG_POOL_MIN`, `VOLT_PG_POOL_TIMEOUT`, `VOLT_PG_POOL_MAX_IDLE`, `VOLT_PG_POOL_MAX_LIFETIME`) to share a bounded pool of connections; `python -m volt.runner` also takes `--pg-pool-max` (use at least 2: the lease heartbeat borrows a second). Off by default. Schema changes go through the numbered list in `volt/migrations.py`. To run the live Postgres tests, set `VOLT_TEST_PG_DSN` to a scratch database; each test uses its own schema and drops it.

Jobs: `python -m volt.runner <db or dsn>` (add `--once` for cron). Set `VOLT_PROXY_DIR`, `VOLT_PUBLISH_DIR` and `VOLT_ASR_ENGINE=faster-whisper`; see `docs/specs/runner.md`.
