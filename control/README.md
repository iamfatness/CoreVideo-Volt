# Control plane

MAM, storage, transcoding, orchestration, and distribution on one asset ID.

```bash
cd control
python3 -m unittest discover -s tests -v
python3 -m volt.serve
```

The session is at http://127.0.0.1:8765. Postgres tables are in `schema.sql`.
