# Start here

Build in this order.

1. Control plane objects. Asset, essence, span, job. Done.
2. MAM ingest and library search. Done.
3. Storage promote and open-file wrap. Done.
4. Proxy transcode and house, OTT, and social files. Done.
5. Orchestration rundown, cue records, clock, and as-run. Done.
6. Distribution versions and live URLs. Done.
7. Speech worker, Postgres, object storage, CoreVideo dispatch, operator desk. Done.
8. MAM foundation: ingest as a job, `taking`, media tickets, audit, package layout, backup inventory. Spec [foundation](specs/foundation.md); plan [plan-foundation.md](plan-foundation.md).

The operator desk is `GET /`. Specs: [ingest](specs/ingest.md), [library](specs/library.md), [rundown](specs/rundown.md), [publish](specs/publish.md), [playout](specs/playout.md), [cues](specs/cues.md). Shot spans and the NLE panel: [spec](specs/shot-spans.md); slices 1 (schema and spans core), 2 (read API and media) and 3 (detection job, split and merge) built, the rest design only or in its own PR.
