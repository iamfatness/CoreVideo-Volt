# Search and metadata

What is built of the design in `docs/superpowers/specs/2026-11-01-search-indexing-design.md`: typed asset metadata, one faceted search across speech and metadata, saved searches, bins, and the Library's Find and Details tabs. **Not built, because each needs a model: the label engine, the ai-index job, people and face templates, the command engine and the Index review and People tabs.** Nothing here claims a face, an object or a word on screen can be found.

## How it works

- **Metadata** (`volt/metadata.py`): fields are typed (text, longtext, number, date, bool, enum), global or per show, optionally required and multi-valued, never deleted (retired). One validation function serves the route and the tests. Required means the asset is Incomplete, not that ingest or publish is refused (the design's open question 1; the default stands).
- **The derived index** (`volt/searchindex.py`): `search_doc` holds one row per speech passage (about 20 words, cut at a 1.5 s gap) and one `meta` row per asset (its text, long text and choice values). It is rebuilt from `span` and `asset_meta_value` and is never the source of truth. Label rows have a place in the table and in the query (`label.*` filters, facets, the unreviewed-face rule) but nothing writes them yet.
- **Two full-text engines, one query grammar**: SQLite uses an FTS5 external-content table (`search_fts`, kept in step by triggers, `unicode61` with diacritics folded, prefix indexes 2 and 3); Postgres uses a `tsvector` column with a GIN index (`simple` configuration, `VOLT_SEARCH_CONFIG` overrides). The text is parsed once into terms, quoted phrases and `-term`, quoted again for each backend; a SQLite without FTS5 falls back to LIKE, says `degraded: true`, and caps the page at 25.
- **Results** are assets with up to three hits, facets counted over the filtered set, keyset paging with a cursor that belongs to its search. Ranking is the backend's text score times a weight by kind (metadata 1.5, speech 1.0); the two backends' scores are not comparable.
- **Saved searches and bins** are owner-only unless shared. An admin does not see another person's private ones (an open owner question).
- **The Library** has Find (the landing view) and Details. See [operator-ui.md](operator-ui.md).
- **Health**: `GET /api/health` has an `indexing` block: `ftsAvailable`, `assetsIndexed`, `documents`, and `queued` and `failed` jobs of type `search` or `ai-index`.

## The bench

`control/tests/bench_search.py` (by hand, not in CI):

```
cd control
python -m tests.bench_search --db <path or postgres dsn, empty> --profile news|long --assets 10000 --seed 1 --runs 200
```

It generates a deterministic corpus (Zipf vocabulary, 12 shows, typed metadata, passages; about 450,000 documents for 10,000 news assets) with `executemany`, times 40 queries across nine classes (single term, phrase, prefix, text plus filters, filter only, facet heavy, rare term, common term, person) after a warm-up, and prints p50, p95 and p99 per class with sizes and resident memory as JSON. It does not write word-level `span` rows, so it does not measure the size of the transcript table, which at 10,000 thirty-minute assets would itself be tens of gigabytes. CI runs a 300-asset corpus, checks that the full-text index is used (no scan of the documents or of `span`), that every query class finds something, that the generator is deterministic and that the two backends find the same assets; it never asserts a time.

## Measured

Run on 2026-11-02 on the development machine (Intel Core i7-14700K, 32 GB, local SSD, Windows 11), the news profile, 10,000 assets, 460,500 documents, seed 1, **3 timed runs per query (not 200)**, so the percentiles are over about 20 to 60 samples a class and the p99 is the slowest sample. SQLite 3.49.1 (file 569 MB); Postgres 16.15 in a local container (search table and indexes 393 MB, GIN index 33 MB). Milliseconds.

| Class | SQLite p50 / p95 | SQLite after ANALYZE p50 / p95 | Postgres p50 / p95 |
|---|---|---|---|
| single term | 57 / 59 | 55 / 57 | 287 / 303 |
| phrase | 14 / 22 | 14 / 22 | 203 / 209 |
| prefix (3 letters) | 919 / 1,031 | 911 / 941 | 1,119 / 1,142 |
| text plus filters | 49 / **16,125** | 42 / 54 | 196 / 1,140 |
| filter only | 7 / **12,674** | 7 / 15 | 182 / 206 |
| facet heavy (8 facets) | 504 / 735 | 75 / 99 | 630 / 838 |
| rare term | 7 / 7 | 7 / 7 | 178 / 184 |
| common term | 1,262 / 1,487 | 1,107 / 1,381 | 585 / 635 |
| person | 6 / 11 | 16 / 19 | 23 / 56 |

Resident memory of the bench process was 91 to 108 MB on SQLite. A first full run with 20 timed runs per query gave the same picture (SQLite filter-only p95 13,242 ms, text plus filters 17,401 ms, common term 1,781 ms) and is why the table above uses 3: the slow classes made 20 impractical.

**Findings**

- **On SQLite, a `meta.*` filter takes 9 to 16 seconds at 10,000 assets until the database has had `ANALYZE`**, and about 15 ms after it. The query plan is the same either way (one scan of `asset` and one indexed probe per row); without statistics SQLite's choice inside it is badly wrong. A deployment that never runs `ANALYZE` will see this after the first big ingest. Nothing in Volt runs `ANALYZE` or `PRAGMA optimize` today; that is the owner's call and a one-line change, so it is reported and not slipped into this change. Facet counts on SQLite also dropped from about 500 ms to 75 ms after it.
- **A common word and a short prefix are the slow text queries on both backends**, 0.6 to 1.5 seconds. A single rare or middling word is fast on SQLite (under 60 ms) and 0.2 to 0.3 seconds on Postgres.
- Postgres is slower than SQLite on every filter-only and rare-term query here (about 180 ms against 7 ms). The cause was not investigated (the container, the number of statements one search issues and the planner are all candidates).

**Against the design's targets** (p95, first page of 25, three facets; the bench asked for eight): text query 250 ms on news: SQLite met it for a single, rare or phrase query and missed it for a common word (1.4 s) and a prefix (0.9 s); Postgres missed it for single term, prefix and common word. Filter only 100 ms: SQLite met it after ANALYZE and missed by two orders of magnitude before it; Postgres missed it (206 ms).


## Limits and unproven

- Targets from the design (p95, warm cache, one user, local disk, first page of 25 with three facets): a text query 250 ms on the news profile and 1 s on the long profile, filter-only 100 ms, saving a metadata value 50 ms, rebuilding one asset's rows 2 s. They are targets, not promises. Saving a metadata value and rebuilding one asset's rows are not in the bench.
- The long profile (about 2.3 million passages) was not run. Only 10,000 news assets, and 3 timed runs per query rather than 200, because the slow classes made 200 impractical (the machine and versions are above).
- A common word is cut to its best 200,000 documents on SQLite before ranking (`FTS_DOC_CAP`), so a very common word is a ranking of a subset.
- No real model has run. Everything about faces, on-screen text, objects, scenes, the label review and the people gallery is unbuilt and unvalidated.
- Postgres was one version (the local container); other versions are untested.
