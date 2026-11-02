# Volt: search, metadata and AI indexing, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

## Why

A desk can find a spoken word today and nothing else. `library.search` (`control/volt/library.py`) is `lower(text) LIKE '%needle%'` over transcript spans: it scans every word of every asset, returns a span, and knows nothing about who is in the picture, what is on screen, or what the producer filed the clip under. An asset carries four fixed fields (`show_name`, `camera`, `owner`, `retention_class`) plus the `room`, `cleared` and `legal_hold` columns (`asset` in `control/volt/schema.sql`). The Library screen works on one chosen asset at a time (`#lib-asset` in `index.html`); there is no way to look across assets, save a question, or keep a bin of clips for tomorrow's show.

The products a news team or a production house will compare Volt against (Iconik, Dalet, Avid MediaCentral, Cantemo, Moments Lab) are generally understood to offer custom typed metadata with controlled vocabularies, faceted search, saved searches, bins or collections, and AI indexing of faces, on-screen text, objects and scenes with a way to review what the index found. **Caveat: that comparison is from general knowledge of those products, not an audit. No trial, manual or demo was consulted. Treat it as a hypothesis about what buyers expect, and let the first customer correct it.**

This document designs three things that belong together: a metadata schema the desk can define, a faceted search that spans transcript, metadata and AI labels, and a pluggable AI indexing job with a human review step. It stays inside what Volt already is: a stdlib Python control plane, SQLite and Postgres, a runner with leases, and a static operator UI.

## Goals

1. A producer can define typed fields (per show or global), with required flags and controlled vocabularies, and fill them per asset. Values are validated on the server.
2. One search box and a facet rail find assets and moments across transcript, metadata and AI labels, with pagination, ranking, and counts by show, camera, field value, label kind and person.
3. A producer can save a search, and keep a bin (a static collection) of assets or clips.
4. A runner job analyses an asset's proxy at sampled frames and writes typed, time-coded, confidence-scored labels: faces of enrolled people, on-screen text, objects, scene descriptions. The engine behind it is one interface with a local implementation first and an optional cloud adapter later.
5. Every label can be reviewed: confirmed, rejected, corrected or added by hand, with the reviewer on the audit trail. Review work survives a re-run of the index.
6. At 10,000 assets a search page returns inside a stated budget on both databases, and a harness measures it.

## Non-goals

- Anything that needs a real model, a GPU or a cloud account to prove. Those are written as unvalidated (see "What the tests will and will not prove").
- Face recognition of people nobody enrolled: Volt does not detect-and-store unknown faces, cluster them, or offer "who is this?".
- Vector or "semantic" search, natural-language questions, or auto-generated summaries. The label and metadata model leaves room for them; this release does not build them.
- Replacing `/seek` and `library.search`: they stay as they are. The new search is additive.
- OR and NOT groups in the query grammar, boolean metadata expressions typed by users, and saved-search alerts.
- Uploading photos to the server. Enrolment takes a frame of an existing asset (below).
- Moving the four fixed asset fields into the new schema, or changing rights, retention or the rundown.
- Speaker diarisation, sentiment, brand or logo detection, and ratings. They fit the label kinds later.
- Auto-erasing people on a schedule.

## Anti-goals

Failure even if it works:

- A face name shown or searched as fact when no person has confirmed it.
- Any frame, template, name or label leaving the building without an adapter the operator configured by name, and any face data ever reaching a remote adapter.
- A face template, a person's name, or a face label surviving an erase, in the tables, the search index, the audit detail or an API response.
- A review decision lost because the index ran again.
- A search index that disagrees with the rows it was built from and cannot be rebuilt from them.
- Two backends giving different answers to the same query, or one that was never run.
- A new write route that is not in `ROUTE_ROLES` or not audited; a schema change without a numbered migration tested forward on SQLite and Postgres.
- A new UI control that is keyboard-only-broken, colour-only, or loads anything outside same origin.
- A claim that a model works, written down before a model has run.

## Constraints

- Stack as today: Python standard library plus existing optional extras. The control plane imports no model library. SQLite and Postgres both; the new schema is one or more numbered `Migration` entries in `control/volt/migrations.py` (head today is 21), with the matching change to `store.SCHEMA` and `schema.sql`, a forward test from every historical shape in `control/tests/historical_shapes.py`, and a Postgres run (`VOLT_TEST_PG_DSN`).
- Every new POST is in `serve.ROUTE_ROLES` (a missing one is refused as admin-only and a test fails) and writes the audit log through the generic path in `do_POST`, with `detail` added where this spec says so (`Store.append_audit` takes one).
- Job work is a runner handler: lease, heartbeat, `Defer`, `Permanent`, at-least-once (see `docs/specs/runner.md`). It uses `disk.require_space` for the disk floor.
- The operator UI stays three same-origin static files under the CSP in `Handler._static`: no inline script or style, no `http(s)://`, icons from the sprite, state as icon plus word plus colour, tokens in both themes, existing keys unchanged, `docs/brand-guide.md` followed. No new `innerHTML`: label text comes from a model and from the pictures it read, so it is untrusted.
- Request bodies are capped at `MAX_BODY` (64 KB). That alone rules out image upload as JSON.
- Tests use fakes: a fake engine, a fake frame source, a fake remote adapter. No test touches the network, a GPU, or a real model.

## The design

### Owner decisions (recorded as the owner's)

1. First customer is a daily news and live-show team; a production-house MAM team is second. Defaults favour a fast desk: review is quick, search is forgiving, nothing blocks ingest.
2. AI indexing is pluggable: a local engine first, an optional cloud adapter later, behind one interface. Nothing leaves the building unless an adapter is explicitly configured.
3. Faces are sensitive personal data. Identification is opt-in, for named people the desk has added, with deletion and an audit trail.
4. Tests use fake engines, never the network or a GPU. Anything that needs a real model or cloud account is written as unvalidated.
5. The UI is static files under the same-origin CSP, following `docs/brand-guide.md` and `docs/specs/operator-ui.md`. Every new POST is classified in `ROUTE_ROLES` and audited. Schema changes are numbered migrations tested forward on both databases.

### Data model (one new migration, or a few; numbers assigned when built)

New tables, created by `store.SCHEMA` and `schema.sql` on a fresh database and by `Migration` statements on an old one (the pattern migration 21 uses for new tables). `migrations._TYPES` gains `REAL` (double precision on Postgres) and `BLOB` (bytea) for the confidence and template columns. Timestamps are `TIMESTAMP`, flags `BOOLEAN`.

**Metadata**

- `field_def(id, key, label, type, show_name, required, multi, position, retired_at, created_by, created_at)`. `key` unique, `^[a-z][a-z0-9_]{0,39}$`. `type` is one of `text`, `longtext`, `number`, `date`, `bool`, `enum`. `show_name` NULL means every asset; otherwise it applies to assets whose `asset.show_name` equals it (the same free-text link the asset already has to a show; `show` the table is rundown-only). `multi` allows several values (enum or text only).
- `field_term(field_id, term, position, retired_at)` PK `(field_id, term)`: the controlled vocabulary of an `enum` field. A retired term stays valid on values already stored and is refused on new ones.
- `asset_meta_value(asset_id, field_id, ord, v_text, v_num, v_date, updated_by, updated_at)` PK `(asset_id, field_id, ord)`. One row per value; typed columns so a range filter or a sort never parses text and a facet is a `GROUP BY`. Index `(field_id, v_text)` and `(field_id, v_num)`.

**Search**

- `search_doc(id, asset_id, kind, ref_id, tc_in, tc_out, frame_in, frame_out, body, weight, status, created_at)`. A derived table, never authoritative. `kind` is `transcript` (one row per passage), `meta` (one per asset), or a label kind (`face`, `text`, `object`, `scene`; one row per label). `ref_id` is the first transcript span id or the label id, so a hit names a span or a label. `status` mirrors the label's review state (NULL for transcript and meta). Full-text columns are engine-specific: an FTS5 table on SQLite, a `tsvector` column with a GIN index on Postgres (below).
- `saved_search(id, name, owner, shared, query, created_at, updated_at)`. `query` is the request JSON as text.
- `bin(id, name, owner, shared, created_at)` and `bin_member(bin_id, asset_id, span_id, position, added_by, added_at)`. `span_id` is nullable and has no foreign key: detection and transcription delete and rewrite spans, and a bin must never block them (`shots.detect` and `speech.transcribe` do so today). A member whose span is gone is returned as `gone: true`.

**AI indexing**

- `label(id, asset_id, run_id, kind, value, value_norm, confidence, frame_in, frame_out, tc_in, tc_out, box, person_id, status, source, engine_value, reviewed_by, reviewed_at, created_at)`. `kind` is `face`, `text`, `object`, `scene`. `status` is `proposed`, `confirmed` or `rejected`. `source` is `engine` or `manual`. `box` is normalised `x,y,w,h` as JSON text or NULL. `value_norm` is NFKC casefolded for facets. `engine_value` keeps what the engine said when a human corrected it. `person_id` is set only on `face` labels. No foreign key to `span`, for the reason above; the shot a label falls in is found by frame overlap on the existing index `span(asset_id, kind, frame_in)`.
- `index_run(job_id, asset_id, essence_id, engine, engine_version, model, device, remote, kinds, every_seconds, frames_analysed, label_count, suppressed, unmatched_faces, created_at, settings)`. The `asr_run` and `shot_run` shape: one provenance row per run. `unmatched_faces` is a bare count; the faces themselves are not kept.
- `index_batch(job_id, idx, fingerprint, frame_from, frame_to, detections, created_at)` PK `(job_id, idx)`: the `asr_chunk` idea, so a crashed run resumes at its first unfinished batch.
- `person(id, display_name, basis, review_by, created_by, created_at, disabled_at, erased_at)`. `basis` is required text, 10 to 500 characters, saying why this person may be matched (for example "staff anchor, consent form on file 2026-10"). `person_template(person_id, engine_fingerprint, template, created_at)` holds the engine's opaque template; no route returns it.

The existing `span` kinds (`transcript`, `shot`, `clip`) are unchanged. **Why labels are not spans:** spans are what rundown items, versions, NLE export and overlap rules point at (`item.span_id`, `version.span_id`, `shots.overlaps_shot`, `media.ALL_KINDS`); an AI label needs confidence, a review state, a box and a person, and thousands of them per asset would crowd the table that already holds one row per spoken word. A label is promoted to a real object the normal way: the operator marks a clip at its time with the existing marking tools.

**Relation to the transcript.** The transcript stays in `span`. `search_doc` carries it as passages: consecutive word spans ordered by `frame_in`, cut at a gap over 1.5 s, 25 words or 12 s. A passage hit returns the first word's span id and in point, so the player seeks as it does today, and the library spec's acceptance line ("steal" returns the span and the asset ID; reindexing does not duplicate) holds for the new search too.

**Purge.** `retention.purge_asset` deletes a fixed list of tables by hand. Every new table with an `asset_id` must be added to that list or Postgres refuses the asset delete on the foreign key. This is a tested requirement (below).

### The engine interface

A new module `control/volt/indexing.py` (name is the builder's). Stdlib only; engines are loaded lazily like `FasterWhisperEngine`.

```
Frame(index: int, seconds: float, path: Path)
Detection(kind, value, confidence, seconds, box=None, person_id=None)
class IndexEngine(Protocol):
    def describe(self) -> dict      # engine, version, model, device, kinds, remote
    def analyse(self, frames, kinds, people=()) -> list[Detection]
    def enrol(self, image: Path, box=None) -> bytes      # an opaque template; raises AmbiguousFace
```

- `analyse` takes one batch of sampled frames (JPEG files on disk, at most 960 px on the long side) and the set of kinds wanted. For `face`, `people` is the gallery: `(person_id, template)` for each enrolled, enabled person whose template matches this engine's fingerprint. The engine returns a `face` detection only for a face that matches a gallery person; a face it cannot match is dropped inside the engine boundary and counted, never returned. A detection is `kind`, `value` (the person's `display_name` for faces; text read, object name, scene phrase otherwise), `confidence` in 0..1, `seconds` on the proxy, and an optional normalised `box`.
- The control plane treats engine output as untrusted: confidence outside 0..1, value empty or over 200 characters, an unknown kind, a time outside the batch, or a `person_id` not in the gallery makes that detection discarded and counted in `index_run.settings`, not an error.
- `describe()["remote"]` is true for any engine that sends frames off the machine. The runner refuses a remote engine unless its name is also in `VOLT_INDEX_REMOTE_OK`, refuses to pass it the `face` kind or any template whatever the env says, and writes an `index_remote` audit row (actor `runner`, target the asset, detail the adapter name and frame count) before the first frame is sent.
- Implementations: `FakeIndexEngine` (scripted, for tests); `CommandEngine`, which starts a configured program (`VOLT_INDEX_CMD`) and speaks a documented JSON-lines protocol on its stdin and stdout, one request per batch, with a timeout per batch. The local engine and any cloud adapter are such programs, so the control plane never imports torch or a vendor SDK and a model crash kills a child process, not the runner. The reference local worker lives outside `control/volt` (`tools/index-worker/`) and chooses its models; those choices, and their licences, are the builder's to check before a default is named. Known trap, from general knowledge and unchecked: several popular face and object model weights are published for non-commercial or copyleft use, which matters to a commercial news customer.

### The index job

Job type `ai-index`; key `ai-v1:<hash of engine fingerprint, kinds, sample interval, confidence floors>`. The same configuration on the same asset is a no-op; any change is a new run, as `asr-v2` and `shots-v1` work. Registered in `runner.default_handlers`.

Rules, in order:

1. No engine configured (`VOLT_INDEX_ENGINE` unset): the job fails `Permanent` with "no index engine configured", never skipped, as `asr` does.
2. The asset must have a closed proxy essence on disk. Otherwise `Defer` (60 s): a proxy is built by the `proxy` job. **The job reads the proxy only and never the hi-res**, so it is not wrapped in `needs_hi_res`; an archived asset's proxy stays local by design (`docs/specs/archive.md`), so it can be indexed while archived. If its proxy is missing and the hi-res is cold (`archive.is_cold`), it defers with the rehydrate hint, like the other hi-res readers. An open essence defers until wrap.
3. Rights: scene, object and text analysis runs on any asset that is not deleted, as shot detection and marking do. Face matching also needs `cleared` (the speech rule), at least one enrolled and enabled person, and `face` in `VOLT_INDEX_KINDS`. Uncleared means the face part is skipped with a note on the run; the rest proceeds.
4. Disk floor: `disk.require_space(scratch, "index frames", need=batch estimate)`; low disk is `Defer`, as the proxy handler does. Scratch is `VOLT_INDEX_DIR` (default under `VOLT_OUT`).
5. Memory floor (new; there is none today): before starting and between batches, if available memory is under `VOLT_INDEX_MIN_FREE_MEM_MB` (default 1024) the job defers. Available memory is read from `/proc/meminfo` on Linux and `GlobalMemoryStatusEx` on Windows through `ctypes`; where it cannot be read the check passes, the same rule `disk.require_space` uses for an unreadable disk.
6. Sampling: one frame every `VOLT_INDEX_EVERY_SECONDS` (default 2), widened so an asset never exceeds `VOLT_INDEX_MAX_FRAMES` (default 5400); the interval used is recorded. Frames are extracted and analysed in batches of `VOLT_INDEX_BATCH` (default 32); a batch's frames are deleted when it finishes. Memory and scratch use are bounded by the batch, not the asset.
7. Resume: each finished batch writes an `index_batch` row (fingerprinted by configuration and proxy path) before the next starts. A restart reads the rows that still apply and continues from the first unfinished batch; rows from another configuration or past a gap are discarded, as `speech._saved_windows` does.
8. Merge: consecutive samples with the same `(kind, value_norm)` (and the same person) within two sample intervals become one label spanning those frames, confidence the maximum. Detections under the floors (`VOLT_INDEX_MIN_CONF` 0.30; faces 0.50) are dropped and counted. A run keeps at most `VOLT_INDEX_MAX_LABELS` (20,000) labels, highest confidence first, and says so on the run record.
9. Write: one transaction under `store.transaction(f"index:{asset_id}")` with the job row held and the lease confirmed (the pattern in `shots.detect`). It deletes this asset's `proposed` engine labels from earlier runs, never `confirmed`, `rejected` or `manual` ones, and inserts the new labels except those that overlap in time and value a `rejected` label (counted as `suppressed`), then writes `index_run`, clears `index_batch`, and rebuilds the asset's label rows in `search_doc`. A lost lease writes nothing.
10. Timecode: a label's frames are the essence start plus the proxy seconds times the hi-res rate, labelled through `shots.Facts.label`, which also does drop-frame. The proxy is assumed to share start and rate with the hi-res, the assumption the NLE panel already makes and warns about.
11. Queuing: a runner `Periodic` (`index_pending_task`, off unless an engine is configured; `VOLT_INDEX_SCAN_EVERY`, default 300 s) enqueues up to 50 assets per pass that have a proxy and no `index_run`, oldest first. `POST /api/assets/{id}/index` queues one on demand.
12. After the `asr` handler finishes a transcript, and after `ai-index` writes labels, the handler enqueues a `search` job (key `search-v1:<asset>:<latest run ids>`) that rebuilds that asset's transcript and label rows in `search_doc`. It needs no GPU and no hi-res.

### Metadata schema: model and validation

- Defining fields is a schema act; the routes and roles are below. A field's `type` cannot change once any asset has a value (`type_locked`, 409). A field is never deleted, only retired: hidden from forms and search, its stored values kept and still listed by an admin, so no history is lost.
- Validation is one function, `metadata.validate(store, asset, field_key, values)`, used by the route and by tests. Limits: text 1,000 characters, longtext 10,000, number a finite float within ±1e15, date `YYYY-MM-DD` that parses, bool, enum a term of the field that is not retired. At most 60 active fields, 500 terms per field, 20 values per multi field. A field whose `show_name` is not the asset's is `unknown_field` for that asset.
- A value set replaces the field's values for that asset in one transaction. Clearing is `values: []`. Clearing a required field is `required_field` (422).
- **Required** means the asset is incomplete without it, not that ingest is refused: assets arrive from the watch folder with no metadata. `GET /api/assets/{id}/metadata` returns `missing: [keys]`; search filters on `incomplete`; the Library shows an Incomplete chip. Whether a missing required field should also block publish or clearing is the owner's call (open question 1).
- Free text in `text` and `longtext` fields and the `enum` terms go into the asset's one `meta` row in `search_doc`, so a query on a guest name finds the asset.

### The query model

One request shape for search and for saved searches:

```
q        free text, optional
scope    all | transcript | labels | metadata       (default all)
filter   {"all": [clause, ...]}                      (AND only)
facets   comma list of facet names
sort     relevance | recent | show                   (relevance needs q)
group    asset | hit                                 (default asset)
include  comma list: proposed                        (see faces)
limit    1..100 (default 25)       after   opaque cursor
```

A clause is `{field, op, value}` or `{field, op, from, to}`. Fields: built-in `show`, `camera`, `room`, `owner`, `retention`, `cleared`, `legalHold`, `created`, `incomplete`; `meta.<key>`; `label.face`, `label.text`, `label.object`, `label.scene`. Ops: `eq`, `ne`, `in`, `range` (number, date), `exists`, `missing`. A `label.*` clause may carry `min_confidence` and `status` (`confirmed` or `proposed`). The filter is parsed by one function that returns a validated tree or a `bad_input` naming the clause; unknown fields and ops are refused, never ignored.

Text is never passed raw to the full-text engine. A parser turns it into terms, quoted phrases and `-term`, quotes each, and makes the last bare term a prefix; its output is then compiled to FTS5 `MATCH` or Postgres `to_tsquery`. A syntax a backend cannot take is `bad_input`, not a 500.

**Results.** `group=asset` (default) returns one entry per asset with its metadata summary, the matching facets, and up to 3 hits; `group=hit` returns moments. A hit is `{kind, spanId|labelId, tcIn, tcOut, frameIn, frameOut, snippet, status, confidence, score}`. Each asset carries `cleared`, `legalHold` and `blockedBy` in the shape `GET /api/assets/{id}/spans` uses, `archived: true` when its hi-res is cold, and never a file location. Deleted (tombstoned) assets are excluded, as `library.search` does today.

**Ranking.** Text relevance from the backend (bm25 on SQLite, `ts_rank_cd` on Postgres), normalised within the query, times a weight by kind: metadata 1.5, confirmed label 1.2, transcript 1.0, proposed label 0.8 times its confidence; ties by newest asset then id. The scores of the two backends are not comparable and are not meant to be; tests assert order only where the fixture makes it unambiguous. With no `q` the order is `recent`. Rejected labels never rank or match.

**Pagination.** Keyset: the cursor encodes `(sort key, id)` the way `media._encode_cursor` does, is checked on read, and a bad one is `bad_input` ("not a cursor this server issued"). The result set may shift between pages if the index changes; that is stated, not hidden. `limit` over 100 is refused.

**Facets.** Counted over the filtered asset set, at most 20 values each plus an `other` count: `show`, `camera`, `owner`, `retention`, `cleared`, `meta.<key>` (enum and bool; text fields by value), `label.kind`, `label.person` (confirmed only unless `include=proposed`), `year-month` of `created`. A facet is a `GROUP BY` over the candidate asset ids, not a pass through hits.

**Faces.** A proposed face is a guess about a real person. Search and facets treat `face` labels as absent unless they are `confirmed`, or the caller passes `include=proposed`, which the UI offers as an explicit "Include unreviewed faces" switch, off by default. Other kinds include proposed labels by default, shown with their confidence. A search whose filter names a person writes one audit row (`search_person`, target the person id, no name), so looking for someone is on the record; this is the one read that writes.

### Full text: FTS5 or tsvector

Both, behind one small adapter, because the two backends have no common syntax and the existing rule is that each runs the same objects. Python's bundled SQLite on the development machine reports version 3.49.1 with FTS5 and its trigram tokenizer available; whether every deployment's SQLite has FTS5 is checked at open and by `volt.preflight`, and without it search falls back to `LIKE` over `search_doc.body` with a stated warning and a lower page-size cap.

- SQLite: an FTS5 table over `search_doc.body` as an external-content table keyed by the row id, tokenizer `unicode61 remove_diacritics 2`, prefix indexes `2 3`, so a delete by asset does not scan the FTS table.
- Postgres: a `tsvector` column and a GIN index, text search configuration `simple` by default (no stemming: proper nouns such as "Okonkwo" are what a newsroom searches; `VOLT_SEARCH_CONFIG` overrides), prefix terms as `term:*`.
- Parity is a tested contract: the same fixture returns the same asset and hit sets on both. Ranking order is not required to match.

### Saved searches and bins

A saved search is a named request (the JSON above) owned by the caller; `shared` makes it visible to every operator. Only the owner or an admin edits or deletes it. Running it is the ordinary search call with its stored body, so a saved search can never return more than the caller may already see. A bin is a static list of assets or clips with the same owner and sharing rules; adding is idempotent, removing is not destructive to the asset. Limits: 200 saved searches and 200 bins per owner, 5,000 members per bin, names 1 to 100 characters.

### Faces, privacy and retention

- Enrolment is by frame, not by upload: the operator pauses the Library player on a face, names the person, and the server extracts that frame from the asset's proxy and gives it, with an optional box, to `engine.enrol`. One face in the frame needs no box; more than one without a box is `ambiguous_face` (422). The frame image is not kept; only the engine's template is, with the engine fingerprint. A change of engine or model makes a person's template stale: they show "needs re-enrolment" and are skipped by runs with a note.
- Who may match: only enrolled, enabled people whose `basis` is filled in. `review_by` is an optional date; the UI shows overdue people. Nothing is erased automatically.
- Disable stops matching and hides the person's labels from search; the labels stay. **Erase** (admin) deletes, in one transaction, the person's templates, every `face` label with their `person_id`, their `search_doc` rows and facet contribution, and replaces `display_name` with a fixed placeholder while `erased_at` is set. The row stays so audit ids still resolve.
- Audit: enrol, disable, enable, erase, confirm, reject, correct and manual-add on a face each write an audit row. **Audit detail carries the person id, never the name**, because the audit table is insert-only and a name written there could not be erased. The runner writes an audit row per run that used faces, with counts only.
- No route returns a template. `GET /api/people` returns id, display name, basis, review-by, state, and a count of labels.
- Face labels follow the asset: tombstone hides, purge deletes (`retention.purge_asset` list). A backup made before an erase still contains the template and labels. This is true of every table, is stated in the operator documentation, and is the owner's backup-rotation matter (`docs/specs/backup.md`).
- A remote adapter never receives the `face` kind or a template. There is no override in this release.

### API

Reads need a valid token (any role); POSTs are classified as shown. Errors use the existing `{code, reason}` pattern (`ApiError`). New codes are marked.

| Route | Role | Purpose | Errors |
|---|---|---|---|
| `GET /api/search` | any | search, facets, paging | `bad_input` 422 (also bad filter, bad cursor, limit) |
| `GET /api/schema` | any | field defs and terms | none |
| `POST /api/schema/fields` | admin | create or edit a field (key, label, type, show, required, multi) | `bad_input`; `field_exists` 409 new; `type_locked` 409 new |
| `POST /api/schema/fields/{key}/retire` | admin | retire a field | `not_found` |
| `POST /api/schema/fields/{key}/terms` | operator | add terms to an enum | `not_found`; `bad_input` |
| `POST /api/schema/fields/{key}/terms/retire` | admin | retire a term | `not_found` |
| `GET /api/assets/{id}/metadata` | any | values and `missing` | `not_found` |
| `POST /api/assets/{id}/metadata` | operator | set one field's values | `unknown_field`, `type_mismatch`, `not_in_vocabulary`, `required_field` (all 422, new); `not_found` |
| `POST /api/assets/{id}/index` | operator | queue an index run | `index_disabled` 503 new; `essence_open` 409; `no_proxy` 409; `archived` 409 |
| `GET /api/assets/{id}/labels` | any | labels, paged, with the run and job status | `bad_input`; `not_found` |
| `POST /api/assets/{id}/labels` | operator | add a label by hand | `bad_input`; `outside_source` 422 |
| `POST /api/labels/{id}/review` | operator | `{action: confirm / reject / reopen / correct, value?, personId?}` | `not_found`; `bad_input`; `person_unavailable` 409 new |
| `GET /api/people` | any | people, no templates | none |
| `POST /api/people` | operator | add a person (name, basis, review-by) | `bad_input`; `faces_disabled` 503 new |
| `POST /api/people/{id}/enrol` | operator | add a template from `{assetId, frame, box?}` | `ambiguous_face` 422 new; `no_proxy` 409; `index_disabled` |
| `POST /api/people/{id}/disable`, `/enable` | operator | stop or resume matching | `not_found` |
| `POST /api/people/{id}/erase` | admin | erase a person | `not_found` |
| `GET /api/searches`, `POST /api/searches`, `POST /api/searches/{id}/delete` | any / operator / operator | saved searches | `bad_input`; `forbidden_role` 403 for another owner's |
| `GET /api/bins`, `GET /api/bins/{id}`, `POST /api/bins`, `POST /api/bins/{id}/add`, `/remove`, `/delete` | any / operator | bins | `bad_input`; `not_found`; `bin_full` 409 new |

`_AUDIT_KIND` in `serve.py` gains kinds for `schema`, `labels`, `people`, `searches` and `bins`, and `_audit_fields` gets the matching action names; metadata, label review and person routes add old and new values (ids, never person names) as audit `detail`. Index runs are also visible on the existing health output: the count of assets indexed, queued and failed.

### UI

Everything is in the Library screen so the redesign's workspaces and keys hold; the Editor and Producer layouts both include it.

- **Find** (new first Library tab, and the landing view when no asset is chosen). A search field, a scope switch (All, Speech, Labels, Details), a facet rail (checkbox lists with counts, a "Show N more" for long lists), a results list (thumbnail from the containing shot's existing thumb route when there is one, asset name, snippet with the match, kind chip, in point, confidence, and the rights chips already used), "Include unreviewed faces" off by default, and Save search / Saved searches / Bins menus. Choosing a hit selects the asset, opens its Shots tab and seeks the player to the in point. `/` focuses the search field on the Library screen (unused today); Arrow keys move through results; Enter opens; Escape clears the field.
- **Details** (new tab). The asset's metadata as a form generated from the schema: text, number, date, a checkbox, a select or a token list for an enum. Required fields marked in words ("Required"), an Incomplete chip in the header, a refusal shown beside the field with its `code`. A producer sees "Define a field" (admin only) and "Add a term" for enums.
- **Index review** (new tab). Header: engine, model, local or Cloud (icon plus word), when it ran, labels found, `proposed / confirmed / rejected` counts, a Run index button (Re-run when one exists) with the status line pattern the Detection bar uses (queued, running, failed with the job's reason, last run). The list is grouped by kind and sorted least-sure first: a thumbnail, the value, a confidence bar with the number as text, in/out, and a state chip. Rows act by keyboard and mouse: `C` confirm, `R` reject, `E` correct, Enter seeks, Up/Down move, and a reject is undone with Reopen (no two-press needed because it is reversible). Faces show the person name only after confirmation; proposed faces read "Proposed: possible match" with the name beside the confidence. A "Confirm visible" button does not exist (anti-goal: no confirm-all); selecting up to 50 rows and confirming them is allowed, and each is audited. "Add label" takes a kind, a value and the current playhead.
- **People** (Library, admin and operator): list with state and review-by; Add person asks for the name and the basis; "Enrol from this frame" sits beside the player when a person is chosen; Erase is a two-press action that names what will be deleted.
- New state chips on the existing `STATE` map: Proposed (pending), Confirmed (ok), Rejected (neutral), Incomplete (pending), Cloud (alert-style, because frames left the machine). New sprite icons: search, tag, person, text-on-screen, bin. All have tokens in both themes and pass the contrast test; `Icons.SPRITE_IDS` and the node tests check them. Axe runs clean on each new tab in both themes.

### Performance at 10,000 assets

Sizes below are arithmetic, not measurements. Two corpora: a news profile (10,000 assets averaging 6 minutes: about 900 words each, 9 million word spans) and a long profile (averaging 30 minutes: about 4,500 words each, 45 million word spans, which would put the existing `span` table itself at tens of gigabytes; if the harness shows that table is the first thing to fail it is a finding for the owner, not something this release fixes).

- Passages of about 20 words: roughly 450,000 documents on the news profile, 2.3 million on the long profile; body text about 130 bytes each. Expected index size at about 1 to 1.5 times the text: 60 to 90 MB and 300 to 450 MB. Labels at about 100 per asset: a million rows. Metadata values at 10 fields: 100,000 rows.
- Targets (p95, warm cache, one user, local disk, first page of 25 with three facets): text query 250 ms on the news profile and 1 s on the long profile; filter-only 100 ms; saving a metadata value 50 ms; rebuilding one asset's index rows 2 s. These are targets to be checked, not promises.
- Measure with `control/tests/bench_search.py` (run by hand, not in CI): a deterministic generator (fixed seed, Zipf-distributed vocabulary, realistic show/camera/field spread) that bulk-inserts the corpus with `executemany`, never through the API; a fixed query set of 40 (single term, phrase, prefix, text plus 2 filters, filter-only, facet-heavy, rare term, common term, person); 200 timed runs each after a warm-up; it prints p50, p95, p99 per query class, database and index size (SQLite: file and `dbstat`; Postgres: `pg_total_relation_size`), and process RSS, as JSON. It runs on SQLite (WAL file) and on `VOLT_TEST_PG_DSN`. The tests in CI use a 300-asset corpus and assert query plans (the FTS table or index is used; no scan of `search_doc` or `span` for a text query), result parity between backends, and counts; they never assert wall time.

## What the tests will and will not prove

Prove, with fakes and no network: migrations forward from every historical shape on SQLite and Postgres; field and vocabulary validation, including type lock and required; search parity across backends on a fixture, the query grammar and its refusals, facet counts, keyset paging with no gap or repeat, deleted assets excluded; saved search and bin ownership; the index job's rules (defers, floors, resume after a killed batch, lost lease writes nothing, rejection survives a re-run, label cap, merge, drop-frame timecode, untrusted output discarded); the remote guard (no remote engine unless named, never faces, audit row first); erase leaves no template, label, search row or name anywhere including audit; purge removes every new table's rows; every new POST in `ROUTE_ROLES` and audited; UI markup, keys, sprite names and contrast through the existing node-driven tests; query plans.

Do not prove: that any real model finds a face, a word on screen, an object or a scene; accuracy, false-positive rate on faces, or behaviour on broadcast footage with graphics and lower thirds; real-time cost of an index run on any GPU or CPU; that a cloud adapter works or is safe; that the budgets hold on a customer's hardware; Postgres versions other than the one the suite uses; that a producer finds review fast enough on a live desk; or compliance with any privacy law. The `CommandEngine` protocol is proven against a stub program only. Every one of these is unvalidated until a run on real footage says otherwise, and the docs say so in those words.

## Left to the builder

Exact column order, index definitions and the FTS5 and GIN DDL; the exact JSON-lines protocol of `CommandEngine` (write it down in `docs/specs`); the reference worker's models after a licence check; the frame-extraction command line (batch extraction with ffmpeg, injected as a callable so tests use a fake); the cursor encoding; screen layout inside the redesign's rules; wording of refusals; whether a thumbnail route for labels is worth adding or the containing shot's is enough; the memory probe on macOS; whether `bin_member.span_id` is shown with the clip's current label.

## Calls I made, for the owner to check

- Labels get their own table rather than new span kinds, and carry no foreign key to spans.
- Search is a derived index over passages, not a scan of word spans, and it is rebuildable from the source rows.
- FTS5 on SQLite and tsvector on Postgres, `simple` configuration, no stemming. Fall back to `LIKE` with a warning if FTS5 is missing.
- Required fields mark an asset incomplete; they do not block ingest. Whether they block publish is open question 1.
- The index reads the proxy, never the hi-res, so an archived asset can still be indexed. You asked for deferral while archived; I read that as "do not touch the archived hi-res" and defer only when the proxy is missing. Say so if you meant it literally.
- Scene, object and text analysis run on any asset; faces need `cleared`, matching the speech consent rule.
- Unmatched faces are never stored, not even a box. Remote adapters never get faces, with no override.
- Faces are searchable only when confirmed unless the caller opts in, and searching for a person is audited (the one read that writes).
- Enrolment is from a frame of an existing asset; the image is discarded and only the engine's template is kept. A model change means re-enrolment.
- Operators may add and disable people and must state a basis; only an admin may erase.
- Audit detail never contains a person's name.
- The engine is a child process speaking JSON lines, so the control plane stays stdlib-only and a model crash cannot take down the runner.
- Rejected labels are remembered and suppress the same finding on a re-run.
- The 10,000-asset targets, the sampling default (every 2 s, 5,400 frames) and the floors (1 GB memory, 30% and 50% confidence) are my numbers, not measured ones.
- Saved searches and bins are per-owner with a shared flag, not role-gated; bins are static and saved searches are the dynamic ones.

## Open questions

1. Should a missing required field block publishing, or clearing for air? Default here: no, it only marks the asset Incomplete. A yes touches `Store.assert_publishable` and the rights gates, so it should be decided before the metadata task starts.
2. Is there a policy on storing face templates for staff and guests (written consent, retention period, jurisdiction)? Some places treat face templates as biometric data with specific consent and retention rules; I am not a lawyer and have not researched any. Default here: a recorded basis per person, a review-by date, nothing deleted automatically, erase by an admin on request.
3. Which kinds does the first customer want first? Default here: scene, object and text on by default, faces off until someone enrols a person. Dropping faces from the first release entirely would cut the largest risk.
