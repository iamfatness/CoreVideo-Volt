# Library

Status: first implementation, after ingest can mint an ID.

## Job

Find a moment, not a file. Search returns a span on source timecode.

## In scope

- Asset list and span list in the session.
- Technical metadata as fields, not a blob.
- Span markers with in and out.
- ASR with word-level timecode, stored as spans. Whisper-class is enough.
- Shot or scene cuts as spans, after ASR is trusted.
- Proxy playback that seeks to the span.
- Rights chip. Publish and rundown both read it. Search can filter it.

## Out of scope

Face gallery. A 200-field newsroom schema. Dublin Core as a user-facing form. Relink in both Premiere and Resolve. Premiere panel is a later slice of this spec: one NLE, one proxy codec, import span, relink to the hi-res essence.

## Acceptance

- Search "timeout" jumps to the word, not the file start.
- A span dragged toward the rundown carries the asset ID and the in/out, not a path.
- A held asset is visible and cannot be deleted.
- Proxy and hi-res are two essences of one asset.

## Depends on

Ingest. OpenSearch or equivalent over the span documents. The media stays on the store.
