# Plan: wave 2

State on `main`: store (SQLite and Postgres), room adapter, live record, cue dispatch, playout with hold and as-run, take, deliverable files, speech worker. 118 tests. Nothing has run against a live core. Operator mockups are in PR #8 and the owner called them a good start.

## Status (2026-10-11)

Wave A and Wave B are on `main`, except B3. The suite is 416 tests, run on SQLite and on live Postgres 16, and CI runs three jobs per PR (ubuntu, windows, postgres).

| Item | PR | State |
|---|---|---|
| A1 Job runner | #18 | Merged |
| A2 Operator API | #16 | Merged |
| A3 Timecode, drop-frame | #17 | Merged |
| A4 show-input.take against the fake room | #15 | Merged. Live action names are config until CoreVideoPro #861 lands. |
| B1 Storage seam, AWS S3 first | #20 | Merged. Verified against MinIO only; real AWS not tried. Azure and GCS are stubs. |
| B2 Schedule clock | #19 | Merged |
| B3 Speech at scale | not yet | In progress: chunked long files, model benchmarks, hallucination guard. Its columns, if any, are migration 7. |
| B4 Postgres hardening | #22 | Merged. Versioned migrations in `volt/migrations.py`. |
| B5 Operator UI | #21, #23 | Merged; #23 makes Space the take key. |

Schema changes now go through `volt/migrations.py`: add the column to `store.SCHEMA` and `schema.sql` in the same order and append one `Migration` with the next number.

Still open after this wave:

- Live proof against a real core (record, stream, lower third). Blocked while another agent owns the core.
- Live `show-input.take`, after CoreVideoPro #861.
- Real AWS check of the S3 backend, once the owner has a bucket. Azure Blob and GCS backends. Re-signing expiring S3 URLs on a schedule.
- Races left across processes: publishing one span from two writers, `kill` racing `go`, `speech.transcribe` replacing transcript spans.
- The record-start proxy and the final proxy can build the same file at once on two runners.
- `GET /rundown?show=<unknown>` drops the connection.
- Failed cues are not persisted server-side (the UI shows them until the next take).
- Shot spans and the NLE panel.

## What the investigation found

1. **Nothing runs the jobs.** Proxy, speech and publish are functions. The job table has queued jobs and no process claims them. A recorded clip does not become searchable text and three files on its own. This is the biggest gap and it blocks the "one asset ID" story.
2. **Nothing can drive the show.** `serve.py` is read-only. Every operator action (go, skip, hold, record, rights, publish) exists as a function with no endpoint.
3. **`show-input.take` cannot be built to spec.** CoreVideo Pro has no control action that routes a source into a scene (CoreVideoPro issue #861). Everything on the Volt side that can be built without it is listed below.
4. **Timecode is non-drop only, in two places.** `deliverables.py` and `speech.py` each carry their own copy. A 29.97 drop-frame source cuts and indexes at the wrong points.
5. **Publish URLs are `file://`.** There is no storage seam, so no live URLs.
6. **A schedule clock does not exist.** Playout moves only when called.
7. **Speech is proven on 14 s of synthetic voice.** Large models, CUDA, real speech and long files are untested. Postgres lacks pooling and concurrent-write tests, and CI does not run it.

## Rules for every agent

- One worktree and one branch each. One PR each, against `main`. Never `git add -A` (it adds `.claude/worktrees`); add named paths.
- Do not merge. The lead merges after both CI jobs pass on the final commit.
- A schema change goes in `store.py` (SCHEMA and `_migrate`), `schema.sql` and `pg.py` `_ADDED_COLUMNS`, in the same column order. The parity test and a live Postgres run must pass.
- Do not touch a live core. Test against `FakeRoom` and the stub HTTP server.
- Say plainly what was not run.

## Wave A (start now, disjoint files)

| Agent | Scope | Owns | Done when |
|---|---|---|---|
| **A1 Job runner** | `volt/runner.py`: claim queued jobs by type, lease and crash recovery for `running` jobs, retry with backoff and a cap, `python -m volt.runner`. Chains wrap to proxy to speech. Postgres claim uses `FOR UPDATE SKIP LOCKED`. | `runner.py`, job columns in store/schema/pg, `test_runner.py` | A wrapped asset becomes searchable with no manual call. Two runners never run one job twice. A killed runner's job is picked up again. |
| **A2 Operator API** | POST endpoints over existing functions: go, skip, hold, release, take, record start/stop, rights, publish. Bearer token off loopback, refuse to start on a LAN bind without one. Each returns the changed state and the reason on refusal. | `serve.py`, `test_serve_*.py` | Every operator function is reachable over HTTP and is tested for refusal cases. No UI yet. |
| **A3 Timecode** | One `volt/timecode.py` with non-drop and drop-frame, used by deliverables and speech. | `timecode.py`, the timecode functions in `deliverables.py` and `speech.py` | A 29.97 drop-frame fixture cuts and indexes at the right frames. Old tests unchanged. |
| **A4 Show-input spec** | Spec and build against `FakeRoom`: a `show-input.take` handler that sets Preview through a pluggable action, takes, and fires only when `programFrame.videoSources[]` contains the source with `sceneIdAttribution == "live"` and `deliverySequence` advanced. Rewinds on failure like the other cues. Action names stay config until #861 lands. | `dispatch.py` handler, `rooms.py` fake additions, `docs/specs/cues.md`, `test_show_input.py` | Cue passes and fails correctly against the fake and the stub server. Notes name every live assumption. |

## Wave B (after A1 and A3 merge)

| Agent | Scope | Depends on |
|---|---|---|
| **B1 Storage seam** | Storage backend interface (local, S3-compatible). Publish writes through it and URLs come back live. | A3 (touches deliverables) |
| **B2 Schedule clock** | `scheduled_at` on items, a clock process that calls `go` at the time, honours hold, writes as-run. | A1 (process pattern), A2 |
| **B3 Speech at scale** | Long files in chunks, large-model and CUDA benchmark, real human speech with noise, vocabulary with and without hotwords (the invented-words finding). | A1 |
| **B4 Postgres hardening** | Connection pool, concurrent write tests, a Postgres service in CI, a real migration step list. | A1 (job claim) |
| **B5 Operator UI** | Revise mockups per owner feedback, then wire the screens to the A2 endpoints. Accessibility scan before done. | A2 and mockup feedback |

## Wave C (needs the owner or the live core)

- **Live proof** of record, stream and lower third against a real core, in the designated test meeting. Blocked while another agent owns the core.
- **Live `show-input.take`**, after CoreVideo Pro ships the route action in #861.
- Shot spans and the NLE panel, after B3 and B1.

## Order and conflicts

A1, A2, A3, A4 touch different files. A1 and B4 both touch the job claim path, so B4 waits. A3 and B1 both touch `deliverables.py`, so B1 waits. The lead merges in the order A3, A1, A4, A2, then Wave B as each unblocks.

## Decisions (owner, 2026-10-11)

1. Mockups: good start, PR #8 merged as the starting point. B5 revises them before wiring.
2. `/rundown` shape: undecided. Default stays `{hold, items}` until the owner picks.
3. Legal hold does not block transcription.
4. Storage: support AWS, Azure and GCP behind one interface. AWS (S3) first, since it is the most common in production workflows. B1 builds the interface and the S3 backend; Azure Blob and GCS follow.
5. Wave A (A1 to A4) approved to start.
