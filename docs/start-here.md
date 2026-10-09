# Start here

The wing is the end state. The first code is not the wing.

Volt is one session and one asset ID. A recording mints that ID while the file is still growing. Search, the rundown, a cue, and a publish all hold the ID. They do not hold a path.

Do not start with playout, MOS, or a cue that fires CoreVideo. A wrong cue is a worse failure than a missing clock. The clock ships after an editor will live in the library for one show.

## Build order

1. Control plane objects. Asset, essence, span, job. Idempotent jobs. Rights flag on day one.
2. Ingest. Card and growing-file record. Checksum. Technical metadata. Promote only after the check passes.
3. Proxy. Matching timecode. Partial proxy on an open file.
4. Library. Search returns a span. Transcript is a span, not a tag on the file.
5. Session shell. Show list and library in one login. CoreVideo record start mints the ID.
6. Rundown. Items point at spans. Script lives on the item.
7. Publish. One approved span, three versions, URLs write back.
8. Clock and cues. Software playout, then CoreVideo cue dispatch.
9. Edges. MOS export, hardware playout, archive restore.

## What matters in the first month

A file can arrive, survive a checksum, grow while a proxy is already scrubbable, and be found by a word in the transcript. If that is not true, nothing else is a product.

## Specs

- [Ingest](specs/ingest.md)
- [Library](specs/library.md)
- [Rundown](specs/rundown.md)
- [Publish](specs/publish.md)
- [Playout](specs/playout.md)
- [Cues](specs/cues.md)

Playout and cues are specified so the objects do not paint us into a corner. They are not the first implementation.
