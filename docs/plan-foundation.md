# MAM Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Volt's control plane a system of record a MAM can grow on: ingest is a job, media plays with a ticket, one database writer owns a take, jobs carry a payload, writes are audited, and a wheel finds its pages.

**Architecture:** Keep the frozen objects. Add `job.payload`, item status `taking`, table `audit`, HMAC media tickets, and a runner `ingest` handler plus a watch scan. Do not replace `http.server`, do not invent user accounts, do not wait on CoreVideo Pro #861 to call this done.

**Tech Stack:** Existing Volt: Python 3.12 stdlib, unittest, SQLite + Postgres 16, ffmpeg/ffprobe, `volt.serve` / `volt.runner`.

**Spec:** [docs/specs/foundation.md](specs/foundation.md)

## Global Constraints

- Schema changes: append `Migration` 15 then 16 to `control/volt/migrations.py`; the same columns in `store.SCHEMA` and `control/volt/schema.sql` (after the layout move) in the same order. Never edit a shipped migration.
- Tests: `cd control && python -m unittest discover -s tests -v`. Postgres tests skip unless `VOLT_TEST_PG_DSN` is set; add a `*_pg` class via `pgutil` wherever a race matters (taking, ingest enqueue).
- No new required dependencies. HMAC is `hmac` + `hashlib` in stdlib.
- Copy: refusals stay `{ok: false, error: {code, reason}}`. New codes: `taking`, `ticket_invalid`, `ticket_expired`.
- Actor header: `X-Volt-Actor`, 1–100 chars matching `[A-Za-z0-9._:-]+`, default `desk`.
- Do not put `VOLT_TOKEN` in a query string.
- License file: do not add `LICENSE`. `NOTICE` only.
- Volt writing: docs state what it does; no pitch.

## Review Focus

- A take in process A and a go in process B against one show: one proceeds, the other gets `409 taking` or sees the item already past; never two items on air. Task 4.
- HTTP ingest of a 2 GB file: the POST returns before the copy; the runner does the copy. Task 2.
- A stolen media ticket after expiry, or a ticket for asset A used on asset B: 401, no bytes. Task 5.
- Watch of a growing `.mxf`: no ingest request until size and mtime stay still for the stable window. Task 3.
- `volt check` against a wheel-style tree (pages next to `volt/__init__.py`, no `control/session`): install layout is `ok`. Task 7.

## Files

| File | Role |
|---|---|
| `control/volt/migrations.py` | Migration 15 `job.payload`, 16 `audit` table |
| `control/volt/store.py` | SCHEMA, `enqueue` payload, `Job.payload`, `audit()` |
| `control/schema.sql` then `control/volt/schema.sql` | Same columns |
| `control/volt/ingest.py` | `request()` enqueue; `run_ingest(store, job)` handler body |
| `control/volt/runner.py` | `ingest` handler; optional watch periodic |
| `control/volt/watch.py` | Scan roots, stable files, call `ingest.request` |
| `control/volt/building.py` | HTTP ingest returns queued job, no copy |
| `control/volt/playout.py` | `advance(pending=)`, `commit_take`, skip/kill refuse `taking` |
| `control/volt/take.py` | pending advance, dispatch unlocked, commit or rewind |
| `control/volt/rundown.py` | `take(..., status="on air"\|"taking")`; kill/attach refuse `taking` |
| `control/volt/serve.py` | 202 ingest, media-ticket, ticket gate, actor, `/` → operator, drop `_SEQ` as contract |
| `control/volt/media.py` | Ticket issue/verify |
| `control/volt/tickets.py` | HMAC create/check (new, small) |
| `control/session/` → `control/volt/session/` | Pages travel with the package |
| `control/volt/preflight.py` | PACKAGE_ROOT = volt package dir; `sqlite_on_air` warn |
| `control/volt/cli.py` | `volt watch` |
| `control/volt/backup.py` | `media.json`; `--with-media` |
| `control/volt/supervisor.py` | Do not add a fourth process; watch is runner periodic |
| `control/session/operator/operator.js` | Ticket URLs for video and thumbs |
| `control/tests/test_foundation_*.py` | New contract tests (split by task) |
| `docs/areas.md`, `docs/start-here.md`, `README.md`, `docs/roadmap.md`, `docs/specs/ingest.md`, `docs/specs/runner.md`, `docs/specs/operator-api.md`, `docs/operations.md`, `CHANGELOG.md` | Truth |
| `NOTICE` | Third-party |

---

### Task 1: Job payload (migration 15)

**Files:**
- Modify: `control/volt/migrations.py` (append 15), `control/volt/store.py` (`SCHEMA` job table, `Job`, `enqueue`), `control/schema.sql`
- Test: `control/tests/test_store.py`, `control/tests/test_migrations.py` (existing historical-shape loop picks up 15)

**Interfaces:**
- Produces: `Store.enqueue(self, asset_id: str, job_type: str, idempotency_key: str, payload: Optional[dict] = None) -> Job`
- Produces: `Job.payload: Optional[str]` (raw TEXT; handlers parse JSON)
- Produces: conflict on `(asset_id, type, idempotency_key)` does not overwrite payload

- [ ] **Step 1: Write the failing test**

```python
def test_enqueue_writes_payload_once(self):
    job = self.store.enqueue(asset.id, "ingest", "ingest:k", payload={"path": "/a"})
    again = self.store.enqueue(asset.id, "ingest", "ingest:k", payload={"path": "/b"})
    self.assertEqual(job.id, again.id)
    self.assertEqual(json.loads(again.payload)["path"], "/a")
```

- [ ] **Step 2: Run it**

Run: `python -m unittest control.tests.test_store.StoreTests.test_enqueue_writes_payload_once` from `control/` as the other store tests are run.

Expected: FAIL (`enqueue` unexpected keyword or payload None)

- [ ] **Step 3: Implement**

`Migration(15, "job payload", columns=(("job", "payload", "TEXT"),))`. `enqueue` inserts `payload` as `json.dumps(payload)` when not None, else NULL. `ON CONFLICT DO NOTHING` leaves the row. `_job` copies `payload`.

- [ ] **Step 4: Run store + migration tests**

Run: `python -m unittest tests.test_store tests.test_migrations`
Expected: PASS

- [ ] **Step 5: Commit** `feat: job payload column for ingest and later MAM jobs`

---

### Task 2: Ingest is a job

**Files:**
- Modify: `control/volt/ingest.py`, `control/volt/runner.py` (`default_handlers` + `CLAIM_PREFIX` if needed), `control/volt/building.py`, `control/volt/serve.py` (`act_ingest` status 202)
- Test: `control/tests/test_ingest.py`, `control/tests/test_show_building.py`, `control/tests/test_runner.py`, `control/tests/test_operator_api.py`

**Interfaces:**
- Consumes: `Store.enqueue(..., payload=)`
- Produces: `ingest.request(store, source: Path, *, owner: str, quarantine: Path, source_key: Optional[str] = None, show_name: Optional[str] = None, camera: Optional[str] = None) -> IngestResult` with `status="queued"`, `job_id`, `asset_id`, `minted`, `essence_id=""`
- Produces: `runner.Claimed.payload: dict` (JSON of `job.payload`, or `{}`)
- Produces: `ingest.run_ingest(store, job: runner.Claimed) -> None` (raises `runner.Permanent` / `runner.Defer`)
- Produces: HTTP `POST /api/ingest` → 202 `{ok, asset, minted, job, status: "queued"}`; the jobs list may be incomplete until the handler runs
- Source key stays `file:{normcase path}:{size}:{mtime_ns}`

Keep `ingest_file` as a thin wrapper: `request` then `run_ingest` on a fake Claimed, so existing tests that expect a finished ingest on the library call still pass. HTTP and watch use `request` only.

- [ ] **Step 1: Failing tests**

```python
def test_request_does_not_copy(self):
    result = ingest.request(store, src, owner="desk", quarantine=q)
    self.assertEqual(result.status, "queued")
    self.assertFalse((q / f"{result.asset_id}-{src.name}").exists())

def test_handler_copies_and_chains(self):
    result = ingest.request(...)
    ingest.run_ingest(store, Claimed(...))
    self.assertTrue(essence)
    types = {j.type for j in jobs}
    self.assertIn("proxy", types)

def test_http_ingest_returns_before_copy(self):
    # operator API: 202, quarantine dir empty, job queued
```

- [ ] **Step 2: Run those tests — FAIL**

- [ ] **Step 3: Implement `request` / `run_ingest`; HTTP 202**

`serve.py` must send 202 for ingest even though other POSTs are 200. Small exception in the POST dispatcher: if the handler returns a dict with `"httpStatus": 202`, use it and strip the key. Do not change other routes.

Runner: `"ingest": run_ingest` in `default_handlers`. No CLAIM_PREFIX (all `ingest:` keys are runner-owned).

Disk low → `Defer`. Missing source → `Permanent`. Parse fail → fail the job, leave asset, no essence (same as today).

- [ ] **Step 4: Run** `python -m unittest tests.test_ingest tests.test_show_building tests.test_runner tests.test_operator_api`

Expected: PASS. Update any test that asserted ingest HTTP 200.

- [ ] **Step 5: Commit** `feat: runner claims ingest; HTTP only enqueues`

---

### Task 3: Watch folder

**Files:**
- Create: `control/volt/watch.py`
- Modify: `control/volt/cli.py` (`volt watch`), `control/volt/runner.py` (periodic if `VOLT_WATCH` truthy), `control/volt/preflight.py` (roots exist)
- Test: `control/tests/test_watch.py`

**Interfaces:**
- Produces: `watch.scan(store, roots: list[Path], quarantine: Path, *, owner: str = "desk", stable_seconds: float = 5.0, now: Callable[[], float] = time.time, seen: dict) -> list[IngestResult]`
- `seen` maps resolved path → `(size, mtime_ns, first_seen)`. A file is requested when `size` and `mtime_ns` match the last observation and `now - first_seen >= stable_seconds`. New or changed files reset `first_seen`.
- Non-recursive. Skip directories, `.*`, `*.part`.
- `volt watch --once` scans once and exits 0. Loop mode sleeps `VOLT_WATCH_EVERY` (default 2 s).
- Runner periodic: same `scan` when `VOLT_WATCH=1`, using `VOLT_INGEST_ROOTS` and `VOLT_QUARANTINE` / `VOLT_QUARANTINE_DIR`.

- [ ] **Step 1: Failing tests** — growing file not requested; stable file requested once; second scan no new job; path outside roots never called (scan only sees roots)

- [ ] **Step 2: FAIL** then implement `watch.py` + CLI + runner hook

- [ ] **Step 3: Run** `python -m unittest tests.test_watch tests.test_ops_cli`

- [ ] **Step 4: Commit** `feat: watch folder enqueues stable ingest jobs`

---

### Task 4: Taking status (playout writer)

**Files:**
- Modify: `control/volt/rundown.py`, `control/volt/playout.py`, `control/volt/take.py`, `control/volt/serve.py` (`act_take` does not hold `_SEQ` around dispatch; map `taking` → 409), `control/volt/clock.py` (treat `taking` like not ready / 409), `control/volt/operator_state.py` (surface status)
- Test: `control/tests/test_take.py`, `control/tests/test_playout.py`, `control/tests/test_race_kill.py`, `control/tests/test_operator_api.py`

**Interfaces:**
- Produces: `advance(store, show_id, clock=..., precise_clock=..., pending: bool = False) -> Advance`
  - `pending=False`: today's `go` path, status `on air`, as-run stamped
  - `pending=True`: `rundown.take(..., status="taking")`, **no** as-run in; previous item still aired/out-stamped
- Produces: `commit_take(store, adv: Advance, clock=...) -> None` sets target `on air` and stamps as-run in
- Produces: `take_item`: `advance(pending=True)`, dispatch with no `_SEQ`, then `commit_take` or `rewind`
- Produces: while `status='taking'`, `go` / `advance` / `skip` / `kill` / clock take refuse. New `PlayoutError` subclass `TakingError` with `.item_id`
- Produces: API 409 `{code: "taking", item: ...}`

`on_air()` stays `status = 'on air'` only. `/api/state` rundown items include `taking`.

- [ ] **Step 1: Failing tests**

```python
def test_take_item_is_taking_during_dispatch(self):
    # FakeRoom that blocks in invoke; other thread sees status taking; go raises TakingError

def test_failed_cue_rewinds_from_taking(self):
    # unsupported cue: target ready, not taking, not on air

def test_two_processes_cannot_both_pending(self):
    # use racekit / two connections: second advance(pending=True) fails
```

- [ ] **Step 2: FAIL** then implement. Drop the `with _SEQ` around `take_item` in `act_take`. Keep `_SEQ` around `go`/`skip` as optional; the row is the contract.

- [ ] **Step 3: Run** `python -m unittest tests.test_take tests.test_playout tests.test_race_kill tests.test_operator_api tests.test_clock tests.test_dispatch`

- [ ] **Step 4: Commit** `fix: take uses taking status; dispatch holds no lock`

---

### Task 5: Media tickets

**Files:**
- Create: `control/volt/tickets.py`
- Modify: `control/volt/serve.py`, `control/volt/media.py` (if proxy GET lives there), `control/session/operator/operator.js` (path may already be `control/volt/session/` if Task 7 landed first; this task assumes current path until 7)
- Test: `control/tests/test_tickets.py`, `control/tests/test_operator_ui.py` (ticket used when token set — at least a unit of the JS helper, or serve-level)

**Interfaces:**
- Produces: `tickets.issue(secret: str, asset_id: str, kind: str, ttl: int, now: int) -> tuple[str, int]`  # token, exp
- Produces: `tickets.verify(secret: str, asset_id: str, kind: str, token: str, now: int) -> str`  # raises TicketError with code `ticket_invalid` | `ticket_expired`
- Kind is `proxy` or `thumb`. Token format: `exp.hexhmac` (no asset in the token; bound by the path).
- `GET /api/media-ticket?asset=&kind=` token-gated, ttl default 120 max 3600
- Existing media GET accepts `Authorization` **or** valid `ticket` query. Wrong ticket: 401, no body.

Operator JS: when `token` is set, fetch ticket then set `video.src` to returned `url`; thumbs: `<img src=ticket-url>` (cookie-less). Keep blob path if ticket fetch fails with 404 only.

- [ ] **Step 1: Failing tests** — good ticket 206/200 with Range; expired; wrong asset; no ticket and no bearer on token-required server

- [ ] **Step 2: Implement tickets + serve gate + JS**

- [ ] **Step 3: Run** `python -m unittest tests.test_tickets tests.test_media tests.test_operator_api`

- [ ] **Step 4: Commit** `feat: signed media tickets so the player can Range`

---

### Task 6: Audit log

**Files:**
- Modify: `migrations.py` (16), `store.py` SCHEMA + `Store.append_audit` + `Store.list_audit`, `schema.sql`, `serve.py` (`_gate` / POST wrapper)
- Test: `control/tests/test_audit.py`, one assertion in `test_operator_api.py` that take writes `action=take`

**Interfaces:**
- Produces: `Store.append_audit(self, actor: str, action: str, target_kind: str, target_id: str, detail: Optional[str] = None) -> str`
- Produces: `GET /api/audit?target=` last 200 for that `target_id`
- POST reads `X-Volt-Actor` or body `owner` or `desk`. Invalid actor: 422 `bad_actor` before the action.
- Write audit only after a 2xx that changed or confirmed state (`ok: true`). Not on 401/404/409.

- [ ] **Step 1: Failing tests** then migration 16 `CREATE TABLE audit`

- [ ] **Step 2: Run** `python -m unittest tests.test_audit tests.test_migrations tests.test_operator_api`

- [ ] **Step 3: Commit** `feat: append-only audit of operator writes`

---

### Task 7: Pages and schema live in the package

**Files:**
- Move: `control/session/` → `control/volt/session/`; `control/schema.sql` → `control/volt/schema.sql`
- Modify: `serve.ROOT`, `preflight.PACKAGE_ROOT`, `pg.py` / `store.py` schema path if any reads `control/schema.sql`, `pyproject.toml` package-data, tests that open those paths, `control/README.md`
- Test: `control/tests/test_ops_cli.py` or `test_preflight` — layout ok when files sit beside `__init__.py`

**Interfaces:**
- Produces: `volt.paths.package_root() -> Path` equal to `Path(volt.__file__).resolve().parent`
- Produces: `GET /` is the operator HTML (same bytes as `/operator`)
- Delete the old search shell (`index.html` with `/assets` search). Keep GET `/assets` and `/seek` JSON.

Grep the repo for `session/index.html`, `control/session`, `schema.sql` before the move.

- [ ] **Step 1: Failing preflight test** with a temp copy of only the package dir

- [ ] **Step 2: Move files, fix imports, `/` redirect or same static map**

- [ ] **Step 3: Full suite** `python -m unittest discover -s tests`

- [ ] **Step 4: Commit** `fix: ship operator pages and schema inside the volt package`

---

### Task 8: Backup names the bytes

**Files:**
- Modify: `control/volt/backup.py`, `docs/specs/backup.md`, `docs/operations.md`
- Test: `control/tests/test_data_safety.py`

**Interfaces:**
- Produces: backup dir always contains `media.json`: `{essences: [...], versions: [...]}` as in the spec
- Produces: `volt backup --with-media` copies local files under the three dirs into `<dest>/media/<asset_id>/...`; missing source is listed in `media.json` `missing: []` and is not a backup failure
- Restore unchanged besides reading `media.json` if present (ignore if old backup)

- [ ] **Step 1: Failing test** that `media.json` lists the closed hi-res after backup

- [ ] **Step 2: Implement**

- [ ] **Step 3: Run** `python -m unittest tests.test_data_safety`

- [ ] **Step 4: Commit** `feat: backup writes media inventory; optional local copy`

---

### Task 9: Docs truth, air-store warning, NOTICE

**Files:**
- Modify: `docs/areas.md`, `docs/start-here.md`, `README.md`, `docs/roadmap.md`, `docs/specs/ingest.md`, `docs/specs/runner.md`, `docs/specs/operator-api.md`, `docs/operations.md`, `docs/product-readiness.md` (banner: dated 2026-10-16; current gaps are foundation.md), `CHANGELOG.md` Unreleased, `control/volt/preflight.py`
- Create: `NOTICE`
- Test: `control/tests/test_ops_cli.py` — `VOLT_ROOM_URL` + SQLite → warn `sqlite_on_air`

**Interfaces:**
- Roadmap Next becomes the nine tasks of this plan, then the old “after the session is the daily tool” list.
- Areas “Next” lines that are already built are deleted.
- Ingest spec: watch + job; HTTP 202.
- Operator API: 202 ingest, taking 409, media-ticket, audit GET, actor header.
- NOTICE: ffmpeg, faster-whisper, boto3/azure/gcs/psycopg as optional; no license grant for Volt.

- [ ] **Step 1: Preflight test then warning**

- [ ] **Step 2: Docs in the same commit as NOTICE**

- [ ] **Step 3: Commit** `docs: foundation is the gap list; NOTICE for third-party`

---

### Task 10: Room track (docs only unless #861 is already shipped)

**Files:**
- Modify: `docs/rooms.md`, `docs/specs/cues.md` — one paragraph pointing at foundation.md; keep unverified list
- No handler stubs for `record.arm` / `audio.snapshot`

If CoreVideo Pro already exposes a route action when this task is reached: add it to the default `show_input_actions` in a **new** plan, not here. Record a live Program take in `rooms.md` the way the 2026-10-16 stream run is recorded.

- [ ] **Step 1: Docs only**

- [ ] **Step 2: Commit** `docs: room track stays parallel to MAM foundation`

---

## Order and parallelism

1 → 2 → 3 can pipeline (2 needs 1). 4, 5, 6 are independent of ingest once 1 exists; 4 should not land in the same week as a live show without a FakeRoom rehearsal. 7 last among code (path churn). 8 anytime after store is stable. 9 last. 10 anytime.

Do not start Library features, a second NLE, or MOS until Tasks 1–7 are on `main`.

## Execution

Plan complete. Review [docs/specs/foundation.md](specs/foundation.md) and this file.

Which execution approach:

- **Subagent-driven** — a fresh subagent per task, reviewed before the next. Safer for migrations and the taking-status race.
- **Native** — one session implements every task, one review at the end.

Recommend **subagent-driven** because Tasks 1, 2, 4, and 7 each have a schema or path contract a later task will assume.
