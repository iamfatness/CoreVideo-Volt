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

SQLite runs the contract tests. Postgres is the running store, same tables in `control/schema.sql`.
