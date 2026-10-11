# Control plane

MAM, storage, transcoding, orchestration, and distribution on one asset ID.

```bash
cd control
python3 -m unittest discover -s tests -v
python3 -m volt.serve
```

The session is at http://127.0.0.1:8765. Postgres tables are in `schema.sql`.

SQLite is the default. To run on Postgres, `pip install -r requirements.txt` and set `VOLT_DB=postgresql://user:pw@host/db` (the schema is applied on start). To run the live Postgres tests, set `VOLT_TEST_PG_DSN` to a scratch database; each test uses its own schema and drops it.
