"""Job runner. Claims queued jobs, runs the handler for their type, and records the outcome on the job row.

Claiming is one atomic UPDATE ... RETURNING. On Postgres the candidate subselect is FOR UPDATE SKIP LOCKED, so
concurrent runners take different rows; on SQLite the single statement holds the write lock, so they cannot
take the same one. A claimed job is `running` with a lease (claimed_by, lease_expires_at). A live runner
extends the lease while the handler works; if the runner dies the lease lapses and the next claim cycle
re-queues the job, counting the lost run as an attempt.

A handler returns normally (done), raises Defer (back to queued, no attempt used: the work is not ready),
raises Permanent (failed now, retrying cannot help) or raises anything else (retry with exponential backoff
until the attempt cap, then failed with the last error left on the row).

Handlers are idempotent per asset. Delivery is at-least-once: a runner that loses its lease mid-job may
finish work another runner has also started, so handlers must tolerate a repeat.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from volt import pg, publish as publish_mod, record, speech, storage
from volt.store import Store

log = logging.getLogger("volt.runner")

DEFAULT_LEASE = 120.0  # seconds; a live runner renews it every third of this
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF = 5.0  # seconds before the 2nd attempt; doubles each time
DEFAULT_BACKOFF_CAP = 300.0
DEFAULT_DEFER = 30.0  # seconds before a deferred job is looked at again

# Only jobs whose key starts with this are claimable for the type. The functions behind these types write
# their own transient queued rows (publish: one per kind; asr when called by hand); a runner must not take those.
CLAIM_PREFIX = {"publish": "publish-request:", "asr": "asr:"}


class Permanent(Exception):
    """Fail the job now. Retrying will not change the outcome (bad configuration, missing input)."""


class Defer(Exception):
    """Put the job back without using an attempt. The work is not ready yet."""

    def __init__(self, reason: str, delay: float = DEFAULT_DEFER):
        super().__init__(reason)
        self.delay = delay


@dataclass
class Claimed:
    id: str
    asset_id: str
    type: str
    key: str
    attempt: int


Handler = Callable[[Store, Claimed], None]


def request_publish(store: Store, span_id: str) -> str:
    """Ask the runner to publish a span. Returns the job id."""
    row = store.conn.execute("SELECT asset_id FROM span WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        raise KeyError(span_id)
    return store.enqueue(row["asset_id"], "publish", f"publish-request:{span_id}").id


# ---- handlers --------------------------------------------------------------------------------------

@dataclass
class Config:
    proxy_dir: Optional[Path] = None
    publish_dir: Optional[Path] = None
    asr_engine: Optional[str] = None  # "faster-whisper", or "fake" for tests; None = not configured
    asr_model: Optional[str] = None
    asr_device: str = "auto"
    corrections: Optional[Path] = None
    storage: Optional[object] = None          # a storage.Backend; None = VOLT_STORAGE, else local under publish_dir
    storage_scratch: Optional[Path] = None    # where cuts wait for upload (non-local storage)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        def path(name):
            return Path(env[name]) if env.get(name) else None

        return cls(
            proxy_dir=path("VOLT_PROXY_DIR"),
            publish_dir=path("VOLT_PUBLISH_DIR"),
            asr_engine=env.get("VOLT_ASR_ENGINE") or None,
            asr_model=env.get("VOLT_ASR_MODEL") or None,
            asr_device=env.get("VOLT_ASR_DEVICE") or "auto",
            corrections=path("VOLT_ASR_CORRECTIONS"),
            storage_scratch=path("VOLT_STORAGE_SCRATCH"),
        )


def _need(value, name: str, flag: str):
    if value is None:
        raise Permanent(f"{name} is not configured (set {flag})")
    return value


def default_handlers(config: Config, engine: Optional[speech.Engine] = None) -> dict[str, Handler]:
    """proxy, asr and publish handlers over the existing functions.

    `engine` overrides the configured one (tests pass a FakeEngine). Otherwise the engine is built on first use
    from config. No engine means asr jobs fail loudly with the reason; they are never skipped."""
    state: dict = {"engine": engine}

    def get_engine() -> speech.Engine:
        if state["engine"] is None:
            kind = config.asr_engine
            if kind is None:
                raise Permanent("no ASR engine configured (set VOLT_ASR_ENGINE=faster-whisper or --asr-engine)")
            if kind != "faster-whisper":
                raise Permanent(f"unknown ASR engine {kind!r} (only faster-whisper)")
            try:
                state["engine"] = speech.FasterWhisperEngine(device=config.asr_device, model=config.asr_model)
            except speech.SpeechError as exc:
                raise Permanent(str(exc)) from exc
        return state["engine"]

    def proxy(store: Store, job: Claimed) -> None:
        proxy_dir = _need(config.proxy_dir, "proxy directory", "VOLT_PROXY_DIR or --proxy-dir")
        try:
            record.build_proxy(store, job.asset_id, proxy_dir, owned=True)
        except FileNotFoundError as exc:
            raise Permanent(f"missing media: {exc}") from exc

    def asr(store: Store, job: Claimed) -> None:
        eng = get_engine()
        asset = store.get_asset(job.asset_id)
        if not asset.cleared:  # the consent rule: wait, do not burn attempts or fail
            raise Defer("waiting: asset is not cleared")
        corrections = speech.parse_corrections(config.corrections.read_text(encoding="utf-8")) \
            if config.corrections else []
        vocabulary = speech.show_vocabulary(store, asset.show_name) if asset.show_name else []
        try:
            speech.transcribe(store, job.asset_id, eng, vocabulary, corrections, job_id=job.id)
        except speech.SpeechError as exc:
            if "still recording" in str(exc) or "no hi-res" in str(exc):
                raise Permanent(str(exc)) from exc
            raise

    def publish(store: Store, job: Claimed) -> None:
        span_id = job.key.split(":", 1)[1]
        try:
            backend = config.storage or storage.from_env(local_root=config.publish_dir)
        except storage.StorageError as exc:
            raise Permanent(str(exc)) from exc
        out_dir = config.publish_dir
        if isinstance(backend, storage.LocalBackend):
            out_dir = _need(out_dir, "publish directory", "VOLT_PUBLISH_DIR or --publish-dir")
        try:
            versions = publish_mod.publish_files(store, span_id, out_dir, backend=backend,
                                                 scratch=config.storage_scratch)
        except KeyError as exc:
            raise Permanent(f"span {span_id} not found") from exc
        except Exception as exc:
            if exc.__class__.__name__ == "NotClearedError":
                raise Permanent("asset is not cleared or is on legal hold") from exc
            raise
        failed = [v for v in versions if v.status != "done"]
        if failed:
            reasons = []
            for v in failed:
                row = store.conn.execute(
                    "SELECT error FROM job WHERE asset_id = ? AND type = 'publish' AND idempotency_key = ?",
                    (job.asset_id, f"publish:{span_id}:{v.kind}")).fetchone()
                reasons.append(f"{v.kind}: {(row['error'] if row else None) or 'failed'}")
            raise RuntimeError("; ".join(reasons))

    return {"proxy": proxy, "asr": asr, "publish": publish}


# ---- the runner ------------------------------------------------------------------------------------

def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Runner:
    store: Store
    handlers: dict[str, Handler]
    target: Optional[str] = None  # db path or DSN; lets the lease heartbeat use its own connection
    name: str = field(default_factory=lambda: f"{os.environ.get('COMPUTERNAME', 'runner')}-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    lease: float = DEFAULT_LEASE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: float = DEFAULT_BACKOFF
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    poll: float = 2.0
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self):
        self._stop = threading.Event()
        self.failed_terminally = 0

    # -- time
    def _t(self, offset: float = 0.0) -> str:
        return _fmt(self.clock() + timedelta(seconds=offset))

    # -- claim / recovery
    def recover_expired(self) -> int:
        """Re-queue running jobs whose lease lapsed. A lost run counts as an attempt; at the cap the job fails."""
        cur = self.store.conn.execute(
            """UPDATE job SET
                 status = CASE WHEN attempt >= ? THEN 'failed' ELSE 'queued' END,
                 attempt = CASE WHEN attempt >= ? THEN attempt ELSE attempt + 1 END,
                 error = ?, claimed_by = NULL, lease_expires_at = NULL, run_after = NULL, updated_at = ?
               WHERE status = 'running' AND lease_expires_at < ?""",
            (self.max_attempts, self.max_attempts, "lease expired: the runner stopped or crashed mid-job",
             self._t(), self._t()))
        self.store.conn.commit()
        if cur.rowcount:
            log.warning("recovered %d job(s) with an expired lease", cur.rowcount)
        return cur.rowcount

    def claim(self) -> Optional[Claimed]:
        """Atomically take the oldest due queued job of a handled type, or None."""
        self.recover_expired()
        clauses, params = [], []
        for job_type in self.handlers:
            prefix = CLAIM_PREFIX.get(job_type)
            if prefix:
                clauses.append("(type = ? AND idempotency_key LIKE ?)")
                params += [job_type, prefix + "%"]
            else:
                clauses.append("type = ?")
                params.append(job_type)
        if not clauses:
            return None
        now = self._t()
        lock = " FOR UPDATE SKIP LOCKED" if self.store.postgres else ""
        row = self.store.conn.execute(
            f"""UPDATE job SET status = 'running', claimed_by = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = (SELECT id FROM job
                            WHERE status = 'queued' AND ({' OR '.join(clauses)})
                              AND (run_after IS NULL OR run_after <= ?)
                            ORDER BY created_at, id LIMIT 1{lock})
                RETURNING id, asset_id, type, idempotency_key, attempt""",
            [self.name, self._t(self.lease), now, *params, now]).fetchone()
        self.store.conn.commit()
        if row is None:
            return None
        return Claimed(row["id"], row["asset_id"], row["type"], row["idempotency_key"], row["attempt"])

    # -- outcome (every write is guarded by the lease, so a runner that lost its lease changes nothing)
    def _settle(self, job: Claimed, sql: str, params: tuple) -> bool:
        cur = self.store.conn.execute(
            sql + " WHERE id = ? AND claimed_by = ? AND status = 'running'", (*params, job.id, self.name))
        self.store.conn.commit()
        return cur.rowcount == 1

    def _done(self, job: Claimed) -> bool:
        return self._settle(job, "UPDATE job SET status = 'done', error = NULL, claimed_by = NULL, "
                                 "lease_expires_at = NULL, run_after = NULL, updated_at = ?", (self._t(),))

    def _defer(self, job: Claimed, reason: str, delay: float) -> bool:
        return self._settle(job, "UPDATE job SET status = 'queued', error = ?, claimed_by = NULL, "
                                 "lease_expires_at = NULL, run_after = ?, updated_at = ?",
                            (reason, self._t(delay), self._t()))

    def _fail(self, job: Claimed, error: str, retry: bool) -> bool:
        if retry and job.attempt < self.max_attempts:
            delay = min(self.backoff_cap, self.backoff * 2 ** (job.attempt - 1))
            return self._settle(job, "UPDATE job SET status = 'queued', attempt = attempt + 1, error = ?, "
                                     "claimed_by = NULL, lease_expires_at = NULL, run_after = ?, updated_at = ?",
                                (error, self._t(delay), self._t()))
        self.failed_terminally += 1
        return self._settle(job, "UPDATE job SET status = 'failed', error = ?, claimed_by = NULL, "
                                 "lease_expires_at = NULL, run_after = NULL, updated_at = ?",
                            (error, self._t()))

    # -- running
    def run_job(self, job: Claimed) -> str:
        """Run one claimed job. Returns done, deferred, retry, failed or lost."""
        beat = _Heartbeat(self, job) if self.target and self.target != ":memory:" else None
        if beat:
            beat.start()
        try:
            self.handlers[job.type](self.store, job)
            outcome, settle = "done", (self._done, ())
        except Defer as exc:
            outcome, settle = "deferred", (self._defer, (str(exc), exc.delay))
        except Permanent as exc:
            outcome, settle = "failed", (self._fail, (str(exc), False))
        except Exception as exc:  # noqa: BLE001 - every handler failure lands on the job row
            self.store.conn.rollback()
            error = str(exc) or exc.__class__.__name__
            outcome = "retry" if job.attempt < self.max_attempts else "failed"
            settle = (self._fail, (error, True))
            log.warning("job %s (%s) attempt %d failed: %s", job.id, job.type, job.attempt, error)
        finally:
            if beat:
                beat.stop()
        if not settle[0](job, *settle[1]):
            log.warning("job %s: lease was lost; outcome %s not recorded", job.id, outcome)
            return "lost"
        return outcome

    def drain(self) -> dict[str, int]:
        """Run due jobs until none is claimable or stop() is called. Returns counts by outcome."""
        counts: dict[str, int] = {}
        while not self._stop.is_set():
            job = self.claim()
            if job is None:
                break
            result = self.run_job(job)
            counts[result] = counts.get(result, 0) + 1
        return counts

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.drain():
                self._stop.wait(self.poll)

    def stop(self) -> None:
        """Ask the loop to end. The job in flight finishes and is recorded; no new job is claimed."""
        self._stop.set()


class _Heartbeat(threading.Thread):
    """Renews the lease while a handler runs. Own connection: store connections are not shared across threads."""

    def __init__(self, runner: Runner, job: Claimed):
        super().__init__(daemon=True)
        self.runner, self.job = runner, job
        self._done = threading.Event()

    def run(self) -> None:
        interval = max(self.runner.lease / 3, 0.05)
        store = Store(self.runner.target, init=False)  # no schema work: it would wait on the handler's locks
        try:
            while not self._done.wait(interval):
                cur = store.conn.execute(
                    "UPDATE job SET lease_expires_at = ? WHERE id = ? AND claimed_by = ? AND status = 'running'",
                    (self.runner._t(self.runner.lease), self.job.id, self.runner.name))
                store.conn.commit()
                if cur.rowcount != 1:
                    break
        except Exception:  # noqa: BLE001 - a failed heartbeat only means the lease may lapse
            log.exception("heartbeat for job %s failed", self.job.id)
        finally:
            store.close()

    def stop(self) -> None:
        self._done.set()
        self.join(timeout=5)


def main(argv: list[str]) -> int:
    import argparse
    env = Config.from_env()
    ap = argparse.ArgumentParser(prog="python -m volt.runner", description="Run queued Volt jobs.")
    ap.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"), help="SQLite path or postgres:// DSN (or VOLT_DB)")
    ap.add_argument("--once", action="store_true", help="run every due job, then exit (cron, tests)")
    ap.add_argument("--proxy-dir", type=Path, default=env.proxy_dir)
    ap.add_argument("--publish-dir", type=Path, default=env.publish_dir)
    ap.add_argument("--asr-engine", default=env.asr_engine, help="faster-whisper (or VOLT_ASR_ENGINE)")
    ap.add_argument("--asr-model", default=env.asr_model)
    ap.add_argument("--asr-device", default=env.asr_device)
    ap.add_argument("--corrections", type=Path, default=env.corrections)
    ap.add_argument("--lease", type=float, default=DEFAULT_LEASE)
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--pg-pool-max", type=int, default=None,
                    help="pool up to N Postgres connections (default: off; or VOLT_PG_POOL_MAX). Use at least 2.")
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config(args.proxy_dir, args.publish_dir, args.asr_engine, args.asr_model, args.asr_device,
                    args.corrections)
    pg.configure_pool_from_env(args.db, max_size=args.pg_pool_max)  # also VOLT_PG_POOL_*; heartbeats borrow
    store = Store(args.db)
    runner = Runner(store, default_handlers(config), target=args.db, lease=args.lease,
                    max_attempts=args.max_attempts, backoff=args.backoff, poll=args.poll)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: runner.stop())
    try:
        if args.once:
            counts = runner.drain()
            print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no due jobs")
            return 1 if runner.failed_terminally else 0
        log.info("runner %s started", runner.name)
        runner.run_forever()
        log.info("runner %s stopped", runner.name)
        return 0
    finally:
        store.close()
        pg.close_pools()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
