# Areas

## MAM

Ingest mints an asset when a card lands or a CoreVideo recording starts. Metadata, checksum, and rights live on that asset. Search returns a span with source timecode.

Built: mint, essence, span, transcript index, search, rights gate.
Next: speech worker, shot spans, one NLE panel.

## Storage

Hi-res and proxy are located essences. A card waits in quarantine until it parses. A growing ISO is an open essence until wrap.

Built: quarantine copy, checksum, open flag, wrap.
Next: Postgres for the control plane, object store for the bytes.

## Transcoding

Proxy and deliverables are jobs on the existing asset. A refresh replaces the proxy file and keeps the essence ID.

Built: proxy build and refresh before wrap.
Next: house, OTT, and social file workers.

## Orchestration

The session is the show. Items hold spans and cues. A take arms the cues on that item.

Built: rundown, reorder, kill, cue records, arm.
Next: dispatch into CoreVideo, software playout, as-run.

## Distribution

An approved span publishes as three versions. The URLs land on the rundown item.

Built: version rows, retry on the same version, URL on the item.
Next: workers that write the files and return live URLs.
