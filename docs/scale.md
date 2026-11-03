# Running Volt at scale

> **Unvalidated until the rehearsal for each shape in this page has been run.** The compose and Kubernetes files were never started, linted or applied; they are text, checked only for the probe, the drain settings and the disruption budget. Every capacity figure here is one machine's measurement or a labelled guess. Design and reasoning: [scale-operations-design](superpowers/specs/2026-11-01-scale-operations-design.md). What each setting means: [operations](operations.md).

## Three deployment shapes

1. **Single box** (the installed default, and the first hosted pilot). `volt run` under systemd or Task Scheduler, SQLite or local Postgres, Caddy in front ([tls](tls.md)). A hosted pilot adds `VOLT_TENANCY=on`, Postgres and a nightly per-tenant backup to another disk and another place. No redundancy: a dead box is a restore.
2. **Compose, one host** (`deploy/compose/`): Caddy, Postgres 16, two `serve`, two runners, one clock, a `migrate` step that runs first. Survives a crashed process and allows a rolling restart; does not survive the host. Proxies and thumbnails need a volume every node can read, or object storage.
3. **Kubernetes** (`deploy/kubernetes/`): at least three `serve` pods with `/ready` as the readiness probe, a `preStop` pause and a grace period longer than the drain, a PodDisruptionBudget, runners and clocks as their own Deployments, a Job for `volt migrate`, an Ingress that passes `Host` through. Postgres is the provider's managed HA service; media is on object storage.

More than one `serve` needs Postgres. Pool arithmetic: `nodes * VOLT_PG_POOL_MAX + runners * 2 + reserve` must stay under the server's `max_connections`.

## Upgrading without stopping

Migrations are **expand** (add a nullable column or default, a table, an index: the previous release still works) or **contract** (drop, rename, narrow: `breaking=True`). A breaking change is split over two releases: N stops using the old shape, N+1 drops it. The database records the oldest code allowed to run against it (`schema_version.min_code`), so an older node keeps running against a database that only gained additive migrations (and can restart in the middle of a rollout) and refuses one that gained a breaking migration.

**Bridge release.** The first release that contains this rule cannot be rolled onto nodes older than it: their guard is the old one ("the database is newer than I am") and refuses any newer database. Moving to it is a one-time stop-and-start. Every release after it can roll.

The procedure (hosted and compose; an installed single box simply stops and starts):

1. `volt upgrade plan` lists the pending migrations and whether each is an expand or breaking. Exit 0: a rolling upgrade is safe. Exit 2: it is not (a pending migration is breaking), so stop everything instead. Exit 78: configuration.
2. Take a verified backup (per tenant, and the control schema).
3. `volt migrate` (or `volt migrate --tenants all`) as its own step: idempotent, under the existing advisory lock, tenant by tenant, a failed tenant reported and the rest still done. Nodes no longer need to be the thing that migrates.
4. Roll `serve` nodes one at a time. SIGTERM starts the drain: `/ready` answers 503 so the balancer stops sending, the node waits up to `VOLT_DRAIN_SECONDS` (20) for in-flight requests, then exits. A second signal stops at once. Requests that still arrive while draining are served and told to close the connection; show control is never shed.
5. Roll the runners (each finishes its job; leases cover the rest) and the clock.
6. `volt upgrade plan --verify` confirms the database is at this build's schema. (Listing the nodes and their versions needs the status view, which is not built, so a mixed-version state is checked by hand and by the `volt_schema_version` alert.)

Rollback is forward-only. An expand-only release can be rolled back by running the previous build (the skew rule allows it); anything else is a restore. `/api/health` reports `mediaKeyFingerprint`, a short one-way tag of the media signing key: every node of one deployment must show the same one, or a ticket signed by one node will not verify on another.

## Load-testing a deployment

`python -m bench.load_state` drives N desks polling `GET /api/state`. Seed a scratch database (`--seed`), start `serve` as its own process, and point `--url` at it; the in-process mode is the pessimistic figure because the generator and the server share one interpreter. Measure on the target machine size, with the library sizes you expect (100, 10,000, 100,000 assets), keep-alive on and off, and record the box next to every number.

## Limits table

These are the only measurements made so far, **on one development machine** (Windows 11, 28 logical CPUs, Python 3.12.10, SQLite on a local disk, `serve` and the generator as separate processes on it, 2026-11-03). They are not limits for any other box and say nothing about Postgres, a network or real browsers. The table fills in as rehearsal R5 is run.

| Question | Measured here | Not measured |
|---|---|---|
| Desks one `serve` process carries at a 100-asset library | 50 desks (25 requests/s), p95 4 ms | other library sizes at other desk counts |
| The same at a 10,000-asset library (2.6 MB per poll) | **not 50**: 4.1 requests/s sustained, 21/s for a single desk back to back | where between 1 and 50 it stops; two processes |
| Assets before the poll payload is too large | 26.6 MB and 514 ms at 100,000 for one desk | pagination or conditional requests (not built) |
| Tenants per cluster before migration time or pool size bites | not measured | everything |
| Jobs per second, claim latency, duplicates after `kill -9` | not measured | everything |

Capacity planning guesses until R5 replaces them: desks per node about 25 (a guess), pool arithmetic as above, disk per hour of recorded media from the codec.

## Rehearsals (everything above is unvalidated until these have been run)

| | What | Pass condition |
|---|---|---|
| R1 | Compose on one machine: `kill -9` a `serve` and a runner mid-load | no accepted take lost, no duplicated job, the balancer recovers within 10 s |
| R2 | kind or a small cluster: a rolling upgrade across an expand migration with the poll test running | zero failed requests that were not in flight on a stopping node |
| R3 | Provider-managed Postgres: failover | time to recover, count of failed requests, the pool reconnects |
| R4 | Hosted restore: a drill on a copy of a real tenant | restored and verified within the RTO |
| R5 | The load test on the target VM size | the limits table above filled in |
| R6 | Two real sites over a real link: replicate 50 GB, interrupt the link, resume, verify | the copy verifies after resume (needs the storage-location slice, not built) |
