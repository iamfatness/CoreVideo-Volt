# Areas

## MAM

Ingest mints an asset when a card lands or a CoreVideo recording starts. Metadata, checksum, and rights live on that asset. Search returns a span with source timecode. HTTP ingest and the watch folder enqueue; the runner copies, probes, and checksums.

Built: mint, essence, span, transcript index, search, rights gate, speech worker, shot spans, NLE export (xmeml and EDL), ingest as a runner job, watch folder, job payload, media tickets, operator audit.

A second NLE and MOS export are later. A live Program take is the parallel room track ([roadmap](roadmap.md)).

## Storage

Hi-res and proxy are located essences. A card waits in quarantine until it parses. A growing ISO is an open essence until wrap. Postgres runs the same store. Bytes go to a local directory or to S3, Azure Blob, or GCS.

Built: quarantine copy, checksum, open flag, wrap, Postgres, object storage, backup `media.json` and optional local copy.

An air machine with `VOLT_ROOM_URL` set against SQLite gets a `volt check` warning (`sqlite_on_air`). Archive tier with restore is later.

## Transcoding

Proxy and deliverables are jobs on the existing asset. A refresh replaces the proxy file and keeps the essence ID. House, OTT, and social files are written by the runner.

Built: proxy build and refresh before wrap; house, OTT, and social workers.

## Orchestration

The session is the show. Items hold spans and cues. A take arms the cues on that item. Item status `taking` is the playout writer across processes. The clock takes scheduled items. As-run is stamped on the item.

Built: rundown, reorder, kill, cue records, arm, dispatch into CoreVideo, software playout, as-run, `taking`.

A live Program take (`show-input.take`) stays a parallel room track ([next-bundle](next-bundle.md)). Hardware playout is later.

## Distribution

An approved span publishes as three versions. The URLs land on the rundown item.

Built: version rows, retry on the same version, URL on the item, workers that write the files and return live URLs.
