# Control plane

The first code. Asset, essence, span, job. SQLite so the contract runs without a database server. Postgres is a later store, not a different model.

```bash
cd control
python3 -m unittest tests/test_store.py
```

Mint happens when the recording starts. A proxy is a second essence, not a second asset. Replaying a failed job increments the attempt and does not mint. A hold blocks delete. An uncleared asset cannot publish. A span stores timecode, not a path.
