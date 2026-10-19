# CoreVideo Volt

Volt is the CoreVideo show session. One asset ID runs through MAM, storage, transcoding, orchestration, and distribution.

## MAM

Record start and card ingest mint the asset. A span holds the in and out. Search returns that span. Rights sit on the asset.

Speech writes word spans. Shot spans and an NLE export (xmeml, EDL) are in the tree. More MAM hangs on [foundation](docs/specs/foundation.md).

## Storage

A card stays in quarantine until it parses and the checksum matches. A CoreVideo ISO is an open essence until wrap. Hi-res and proxy are two essences of the same asset.

Postgres runs the same store: pass a `postgres://` DSN (`VOLT_DB` for the session server; needs `psycopg`). Tables are in `control/volt/schema.sql`. Bytes go to a local directory or to S3, Azure Blob, or GCS. `volt check` warns `sqlite_on_air` when a room URL is set against SQLite.

## Transcoding

A proxy is built before wrap and can be refreshed while the file is open. The essence ID stays put.

House, OTT, and social files are written by the runner.

## Orchestration

A show is an ordered list of items. An item points at a span and holds its cues. A take arms those cues.

Dispatch into CoreVideo, the software playout clock, and as-run are in the tree. A take uses item status `taking` so two servers cannot both advance. A live Program take is a parallel room track.

## Distribution

An approved span becomes a house version, an OTT version, and a social version. The URLs write back onto the rundown item.

Workers write the files and return live URLs.

## Run

From `control/`:

```bash
python3 -m unittest discover -s tests -v
python3 -m volt.serve
```

The session is at http://127.0.0.1:8765 (`GET /` is the operator UI).

## Code

| Path | What it does |
|---|---|
| `control/volt/store.py` | Asset, essence, span, job, show, item, version, cue |
| `control/volt/ingest.py` | Enqueue ingest; runner copies into quarantine |
| `control/volt/record.py` | Record start, proxy, wrap |
| `control/volt/library.py` | Word spans and search |
| `control/volt/rundown.py` | Show, item, attach, take, kill |
| `control/volt/cues.py` | Cues on the item |
| `control/volt/publish.py` | Versions and URLs |
| `control/volt/session/` | List, seek, attach, publish; operator pages |

Running it day to day (install, every setting, services, health, logs, upgrades): [docs/operations.md](docs/operations.md).

More in [docs/areas.md](docs/areas.md), [docs/rooms.md](docs/rooms.md), [docs/architecture.md](docs/architecture.md), and [docs/data-model.md](docs/data-model.md).
