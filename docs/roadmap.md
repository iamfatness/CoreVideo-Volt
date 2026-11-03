# Roadmap

## In the tree

MAM mint and search. Storage quarantine and wrap. Proxy transcode. Rundown and stored cues. Publish versions and URLs on the item. Job runner. Operator API and UI. Speech worker at scale. Playout clock and as-run. S3, Azure Blob and GCS object storage, and Postgres. Cue dispatch into CoreVideo, including `show-input.take`. Live proof against a real CoreVideo Pro core of record, lower third and stream (the stream run of 2026-10-16 is in [rooms.md](rooms.md)). MOS export of the rundown as a roCreate XML file (`GET /api/shows/{show}/mos`, `volt mos export`; unvalidated against an NCS, see [mos.md](specs/mos.md)). Shot spans, detection, transcript marking and NLE export (xmeml and EDL; the import into a real NLE is still a manual, owner-deferred check, see [shot-spans.md](specs/shot-spans.md)). TLS as a supported Caddy and nginx reverse-proxy recipe ([tls.md](tls.md), verified with an internal certificate).

MAM foundation ([spec](specs/foundation.md), [plan](plan-foundation.md)): job payload column; ingest as a runner job (HTTP only enqueues); watch folder for stable files; item status `taking` (dispatch holds no lock; two servers cannot both take); signed media tickets; append-only audit of operator writes; operator pages and schema inside the `volt` package (`/` is the desk); backup writes a media inventory with optional local copy; docs and NOTICE; SQLite-plus-room is a preflight warning.

The operations layer in [product-readiness.md](product-readiness.md) (start command, health, logs, packaging, disk floor, purge, version skew, numbered 0.1.0) is in the tree.

## Next

Search and metadata: typed fields, faceted search, saved searches, bins and the Library Find and Details tabs are built ([search.md](specs/search.md)). Still to do, all needing a model: labels, the ai-index job, people and face templates, the command engine and the Index review and People tabs.

Room track: [next-bundle](next-bundle.md). Default pre-take is `input.assign` then `scene.route.set` (CoreVideo Pro #861 / [PR #953](https://github.com/iamfatness/CoreVideoPro/pull/953)). The live Program take is recorded (2026-10-28) and `show-input.take` is live-proven on the desk.

A MOS sender (TCP 10540/10541) and the other MOS messages, and validation of the export against a real NCS. Hardware playout adapter. Archive tier with restore. A second NLE. Recurring and relative playout schedules. 

Newsroom scripts ([spec](specs/newsroom-scripts.md), [plan](superpowers/plans/2026-11-01-newsroom-scripts.md)): script editor with markup and read time, status and approval, floating, a prompter feed and page, wire intake, templates and a story archive are in the tree, unvalidated against any real newsroom system, agency feed or prompter.

## Identity and collaboration

In the tree (identity plan tasks 1 to 9, migrations 22 to 34): viewer role and groups, activity view, cookie sessions with CSRF, sign-in with Google and Microsoft, per-show access, edit leases and presence, comments with mentions and notifications. Unvalidated against a real provider or a real team: [collaboration](specs/collaboration.md). Open for the owner: whether unknown sign-ins are auto-provisioned (default: refused), single Microsoft tenant versus `common`, asset comments and the viewer mention roster, and hiding write controls from viewers in the page.
