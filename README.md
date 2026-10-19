# CoreVideo Volt

Volt is the CoreVideo show session. One asset ID runs through MAM, storage, transcoding, orchestration, and distribution.

## MAM

Record start and card ingest mint the asset. A span holds the in and out. Search returns that span. Rights sit on the asset.

Next: a speech worker that writes the word spans, and a Premiere panel that imports one.

## Storage

A card stays in quarantine until it parses and the checksum matches. A CoreVideo ISO is an open essence until wrap. Hi-res and proxy are two essences of the same asset.

Postgres runs the same store: pass a `postgres://` DSN (`VOLT_DB` for the session server; needs `psycopg`). Tables are in `control/volt/schema.sql`. Next: an object store for the bytes.

## Transcoding

A proxy is built before wrap and can be refreshed while the file is open. The essence ID stays put.

Next: house, OTT, and social file workers.

## Orchestration

A show is an ordered list of items. An item points at a span and holds its cues. A take arms those cues.

Next: dispatch into CoreVideo, software playout, and as-run on the item.

## Distribution

An approved span becomes a house version, an OTT version, and a social version. The URLs write back onto the rundown item.

Next: workers that write the files and return live URLs.

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
| `control/volt/ingest.py` | Card into quarantine |
| `control/volt/record.py` | Record start, proxy, wrap |
| `control/volt/library.py` | Word spans and search |
| `control/volt/rundown.py` | Show, item, attach, take, kill |
| `control/volt/cues.py` | Cues on the item |
| `control/volt/publish.py` | Versions and URLs |
| `control/volt/session/` | List, seek, attach, publish; operator pages |

Running it day to day (install, every setting, services, health, logs, upgrades): [docs/operations.md](docs/operations.md).

More in [docs/areas.md](docs/areas.md), [docs/rooms.md](docs/rooms.md), [docs/architecture.md](docs/architecture.md), and [docs/data-model.md](docs/data-model.md).
