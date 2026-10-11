"""Job runner. SQLite always; the same claim/lease/retry tests run on Postgres when VOLT_TEST_PG_DSN is set."""

import os
import subprocess
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from volt import runner as runner_mod
from volt.library import search
from volt.record import record_start, wrap
from volt.runner import Config, Defer, Permanent, Runner, default_handlers, request_publish
from volt.speech import FakeEngine, Word
from volt.store import Store

DSN = os.environ.get("VOLT_TEST_PG_DSN")
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _clip(path: Path) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
         "-c:a", "aac", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _job(store, job_id):
    row = store.conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    store.conn.commit()  # end the read transaction; an open one would block another connection's schema bootstrap
    return row


class ClaimBehaviour:
    """Mixed into a SQLite and a Postgres case. Subclasses provide self.target and self.open_store()."""

    def make_runner(self, handlers, clock=None, **kw):
        store = self.open_store()
        r = Runner(store, handlers, target=None, clock=clock or Clock(), name=kw.pop("name", "r1"), **kw)
        return r

    def enqueue(self, store, n=1, job_type="proxy"):
        asset = store.mint_asset(owner="desk")
        jobs = [store.enqueue(asset.id, job_type, f"k{i}") for i in range(n)]
        store.conn.commit()  # enqueue ends with a read; on Postgres that holds a lock other connections wait on
        return jobs

    def test_two_runners_never_run_one_job_twice(self):
        seed = self.open_store()
        jobs = self.enqueue(seed, 40)
        ran, lock = [], threading.Lock()

        def handler(store, job):
            with lock:
                ran.append(job.id)

        errors = []

        def work(name):
            store = Store(self.target)  # a connection belongs to the thread that made it
            try:
                Runner(store, {"proxy": handler}, name=name).drain()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=work, args=(f"r{i}",)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ran), sorted(j.id for j in jobs))
        self.assertEqual(len(ran), len(set(ran)))
        self.assertEqual({_job(seed, j.id)["status"] for j in jobs}, {"done"})

    def test_only_handled_types_are_claimed(self):
        store = self.open_store()
        self.enqueue(store, 1, "ingest")
        r = Runner(store, {"proxy": lambda s, j: None})
        self.assertIsNone(r.claim())

    def test_expired_lease_is_requeued_and_counts_an_attempt(self):
        clock = Clock()
        store = self.open_store()
        (job,) = self.enqueue(store)
        dead = Runner(store, {"proxy": lambda s, j: None}, clock=clock, lease=60, name="dead")
        claimed = dead.claim()  # then the runner "crashes": never settles
        self.assertEqual(claimed.id, job.id)
        row = _job(store, job.id)
        self.assertEqual((row["status"], row["claimed_by"]), ("running", "dead"))

        other = Runner(self.open_store(), {"proxy": lambda s, j: None}, clock=clock, lease=60, name="live")
        clock.advance(30)
        self.assertIsNone(other.claim())  # lease still held
        clock.advance(31)
        again = other.claim()
        self.assertEqual((again.id, again.attempt), (job.id, 2))
        self.assertEqual(_job(store, job.id)["claimed_by"], "live")

    def test_a_runner_that_lost_its_lease_cannot_overwrite_the_new_owner(self):
        clock = Clock()
        store = self.open_store()
        (job,) = self.enqueue(store)
        slow = Runner(store, {"proxy": lambda s, j: None}, clock=clock, lease=60, name="slow")
        stale = slow.claim()
        clock.advance(61)
        fast = Runner(self.open_store(), {"proxy": lambda s, j: None}, clock=clock, lease=60, name="fast")
        fast.claim()
        self.assertFalse(slow._done(stale))
        self.assertEqual(_job(store, job.id)["claimed_by"], "fast")

    def test_crash_loop_hits_the_cap_and_stays_failed_with_the_reason(self):
        clock = Clock()
        store = self.open_store()
        (job,) = self.enqueue(store)
        r = Runner(store, {"proxy": lambda s, j: None}, clock=clock, lease=10, max_attempts=2, name="a")
        r.claim()
        clock.advance(11)
        r.claim()  # attempt 2 claimed
        clock.advance(11)
        self.assertIsNone(r.claim())
        row = _job(store, job.id)
        self.assertEqual((row["status"], row["attempt"]), ("failed", 2))
        self.assertIn("lease expired", row["error"])

    def test_retry_with_backoff_then_cap(self):
        clock = Clock()
        store = self.open_store()
        (job,) = self.enqueue(store)
        calls = []

        def boom(s, j):
            calls.append(j.attempt)
            raise RuntimeError("encoder died")

        r = Runner(store, {"proxy": boom}, clock=clock, max_attempts=3, backoff=10, name="a")
        self.assertEqual(r.drain(), {"retry": 1})
        row = _job(store, job.id)
        self.assertEqual((row["status"], row["attempt"], row["error"]), ("queued", 2, "encoder died"))
        self.assertEqual(r.drain(), {})  # backoff: not due yet
        clock.advance(9)
        self.assertEqual(r.drain(), {})
        clock.advance(2)
        self.assertEqual(r.drain(), {"retry": 1})  # attempt 2 fails, waits 20s
        clock.advance(19)
        self.assertEqual(r.drain(), {})
        clock.advance(2)
        self.assertEqual(r.drain(), {"failed": 1})
        row = _job(store, job.id)
        self.assertEqual((row["status"], row["attempt"], row["error"]), ("failed", 3, "encoder died"))
        clock.advance(10_000)
        self.assertEqual(r.drain(), {})  # a failed job is not claimed again
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(r.failed_terminally, 1)

    def test_permanent_error_fails_at_once_and_defer_uses_no_attempt(self):
        clock = Clock()
        store = self.open_store()
        a, b = self.enqueue(store, 2)

        def handler(s, j):
            if j.id == a.id:
                raise Permanent("no engine")
            raise Defer("waiting: asset is not cleared", 60)

        r = Runner(store, {"proxy": handler}, clock=clock, name="a")
        self.assertEqual(r.drain(), {"failed": 1, "deferred": 1})
        ra, rb = _job(store, a.id), _job(store, b.id)
        self.assertEqual((ra["status"], ra["attempt"], ra["error"]), ("failed", 1, "no engine"))
        self.assertEqual((rb["status"], rb["attempt"], rb["error"]), ("queued", 1, "waiting: asset is not cleared"))
        self.assertEqual(r.drain(), {})
        clock.advance(61)
        self.assertEqual(r.drain(), {"deferred": 1})

    def test_stop_ends_the_loop_after_the_job_in_flight(self):
        store = self.open_store()
        self.enqueue(store, 3)
        started, release = threading.Event(), threading.Event()

        def handler(s, j):
            started.set()
            release.wait(5)

        box = {}

        def loop():
            store = Store(self.target)
            box["r"] = Runner(store, {"proxy": handler}, poll=0.05)
            box["r"].run_forever()
            store.close()

        t = threading.Thread(target=loop)
        t.start()
        self.assertTrue(started.wait(5))
        box["r"].stop()
        release.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        statuses = [row["status"] for row in store.conn.execute("SELECT status FROM job ORDER BY created_at, id")]
        self.assertEqual(sorted(statuses), ["done", "queued", "queued"])  # in-flight finished, rest untouched

    def test_publish_and_asr_claim_only_runner_requests(self):
        store = self.open_store()
        asset = store.mint_asset(owner="desk")
        store.enqueue(asset.id, "publish", "publish:span1:house")  # the transient row publish() writes itself
        store.enqueue(asset.id, "asr", "asr-v2:abc")
        r = Runner(store, {"publish": lambda s, j: None, "asr": lambda s, j: None})
        self.assertIsNone(r.claim())
        store.enqueue(asset.id, "publish", "publish-request:span1")
        store.enqueue(asset.id, "asr", f"asr:{asset.id}")
        self.assertEqual(r.drain(), {"done": 2})


class SqliteClaims(ClaimBehaviour, unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.target = str(Path(self.tmp.name) / "volt.db")
        self.stores = []

    def tearDown(self):
        for s in self.stores:
            s.close()
        self.tmp.cleanup()

    def open_store(self):
        s = Store(self.target)
        self.stores.append(s)
        return s


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres runner tests")
class PostgresClaims(ClaimBehaviour, unittest.TestCase):
    def setUp(self):
        import psycopg

        self.schema = "volt_r_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        sep = "&" if "?" in DSN else "?"
        self.target = f"{DSN}{sep}options={quote('-csearch_path=' + self.schema)}"
        self.stores = []
        self.addCleanup(self._drop)

    def _drop(self):
        import psycopg

        for s in self.stores:
            s.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def open_store(self):
        s = Store(self.target)
        self.stores.append(s)
        return s

    def test_claim_uses_skip_locked(self):
        seen = []
        store = self.open_store()
        real = store.conn.execute
        store.conn.execute = lambda sql, params=(): (seen.append(sql), real(sql, params))[1]
        Runner(store, {"proxy": lambda s, j: None}).claim()
        self.assertTrue(any("FOR UPDATE SKIP LOCKED" in q for q in seen))


class Chaining(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.make_store()

    def make_store(self):
        return Store(str(self.root / "volt.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def config(self, **kw):
        return Config(proxy_dir=self.root / "proxy", publish_dir=self.root / "out", **kw)

    def wrapped_asset(self, cleared=True):
        iso = self.root / "iso-3.mp4"
        _clip(iso)
        started = record_start(self.store, "cv:iso-3", iso, owner="desk", camera="iso-3")
        self.store.set_cleared(started.asset_id, cleared)
        wrap(self.store, started.asset_id, settle=0)
        return started.asset_id

    def jobs(self, asset_id):
        return {(r["type"], r["idempotency_key"].split(":")[0]): (r["status"], r["error"]) for r in
                self.store.conn.execute("SELECT type, idempotency_key, status, error FROM job WHERE asset_id = ?",
                                        (asset_id,))}

    def test_wrapped_asset_becomes_searchable_with_no_manual_call(self):
        asset_id = self.wrapped_asset()
        engine = FakeEngine([Word("they", 0.2, 0.4), Word("steal", 0.4, 0.9)])
        r = Runner(self.store, default_handlers(self.config(), engine))
        counts = r.drain()
        self.assertEqual(counts, {"done": 2})  # final proxy, asr (wrap closed the record-start proxy job)
        hit = search(self.store, "steal")[0]
        self.assertEqual(hit.asset_id, asset_id)
        self.assertEqual(engine.calls, 1)
        roles = {row["role"] for row in self.store.conn.execute(
            "SELECT role FROM essence WHERE asset_id = ?", (asset_id,))}
        self.assertEqual(roles, {"hi-res", "proxy"})
        self.assertTrue((self.root / "proxy" / f"{asset_id}.mp4").exists())
        # nothing is left to run, and running again changes nothing
        self.assertEqual(r.drain(), {})
        wrap(self.store, asset_id, settle=0)
        self.assertEqual(r.drain(), {})
        self.assertEqual(engine.calls, 1)

    def test_uncleared_asset_waits_then_runs_once_cleared(self):
        asset_id = self.wrapped_asset(cleared=False)
        clock = Clock()
        engine = FakeEngine([Word("steal", 0.4, 0.9)])
        r = Runner(self.store, default_handlers(self.config(), engine), clock=clock)
        self.assertEqual(r.drain(), {"done": 1, "deferred": 1})
        status, error = self.jobs(asset_id)[("asr", "asr")]
        self.assertEqual((status, error), ("queued", "waiting: asset is not cleared"))
        self.assertEqual(engine.calls, 0)
        self.assertEqual(search(self.store, "steal"), [])
        self.store.set_cleared(asset_id, True)
        clock.advance(31)
        self.assertEqual(r.drain(), {"done": 1})
        self.assertEqual(len(search(self.store, "steal")), 1)
        self.assertEqual(self.jobs(asset_id)[("asr", "asr")], ("done", None))

    def test_no_engine_fails_the_job_loudly_not_silently(self):
        asset_id = self.wrapped_asset()
        r = Runner(self.store, default_handlers(self.config()))  # no engine, none in config
        r.drain()
        status, error = self.jobs(asset_id)[("asr", "asr")]
        self.assertEqual(status, "failed")
        self.assertIn("no ASR engine configured", error)
        self.assertEqual(r.failed_terminally, 1)

    def test_failing_engine_leaves_its_reason_on_the_job_and_retries(self):
        asset_id = self.wrapped_asset()
        engine = FakeEngine(error="CUDA out of memory")
        r = Runner(self.store, default_handlers(self.config(), engine), max_attempts=2, backoff=1,
                   clock=(clock := Clock()))
        r.drain()
        self.assertEqual(self.jobs(asset_id)[("asr", "asr")], ("queued", "CUDA out of memory"))
        clock.advance(2)
        r.drain()
        self.assertEqual(self.jobs(asset_id)[("asr", "asr")], ("failed", "CUDA out of memory"))
        self.assertEqual(engine.calls, 2)
        self.assertEqual(search(self.store, "steal"), [])

    def test_missing_proxy_dir_fails_the_proxy_job_with_the_reason(self):
        asset_id = self.wrapped_asset()
        r = Runner(self.store, default_handlers(Config(), FakeEngine()))
        r.drain()
        status, error = self.jobs(asset_id)[("proxy", "proxy-final")]
        self.assertEqual(status, "failed")
        self.assertIn("proxy directory is not configured", error)

    def test_publish_request_cuts_the_files(self):
        asset_id = self.wrapped_asset()
        span_id = self.store.add_span(asset_id, "clip", "00:00:00:05", "00:00:01:00")
        job_id = request_publish(self.store, span_id)
        r = Runner(self.store, {"publish": default_handlers(self.config())["publish"]})
        self.assertEqual(r.drain(), {"done": 1})
        self.assertEqual(_job(self.store, job_id)["status"], "done")
        urls = [row["url"] for row in self.store.conn.execute(
            "SELECT url FROM version WHERE span_id = ? AND status = 'done'", (span_id,))]
        self.assertEqual(len(urls), 3)

    def test_publish_of_an_uncleared_asset_fails_with_the_reason(self):
        asset_id = self.wrapped_asset(cleared=False)
        span_id = self.store.add_span(asset_id, "clip", "00:00:00:05", "00:00:01:00")
        job_id = request_publish(self.store, span_id)
        Runner(self.store, {"publish": default_handlers(self.config())["publish"]}).drain()
        row = _job(self.store, job_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("not cleared", row["error"])

    def test_failed_render_puts_the_kind_and_reason_on_the_job(self):
        asset_id = self.wrapped_asset()
        span_id = self.store.add_span(asset_id, "clip", "00:00:09:00", "00:00:10:00")  # past the 3 s clip
        job_id = request_publish(self.store, span_id)
        Runner(self.store, {"publish": default_handlers(self.config())["publish"]}, max_attempts=1).drain()
        row = _job(self.store, job_id)
        self.assertEqual(row["status"], "failed")
        self.assertRegex(row["error"], r"house: ")


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres runner tests")
class PostgresChaining(Chaining):
    """The same wrap -> proxy + asr -> searchable words, publish and failure tests, on Postgres."""

    def make_store(self):
        import psycopg

        self.schema = "volt_c_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        sep = "&" if "?" in DSN else "?"
        self.addCleanup(self._drop)
        return Store(f"{DSN}{sep}options={quote('-csearch_path=' + self.schema)}")

    def _drop(self):
        import psycopg

        self.store.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")


class Cli(unittest.TestCase):
    def test_once_runs_due_jobs_and_exits_nonzero_on_a_terminal_failure(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "volt.db")
            store = Store(db)
            asset = store.mint_asset(owner="desk")
            job = store.enqueue(asset.id, "asr", f"asr:{asset.id}")
            store.close()
            env = {k: v for k, v in os.environ.items() if not k.startswith("VOLT_")}
            run = subprocess.run(
                [os.sys.executable, "-m", "volt.runner", db, "--once"], capture_output=True, text=True, env=env,
                cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("failed=1", run.stdout)
            store = Store(db)
            row = _job(store, job.id)
            store.close()
            self.assertIn("no ASR engine configured", row["error"])


class ConfigEnv(unittest.TestCase):
    def test_from_env(self):
        c = Config.from_env({"VOLT_ASR_ENGINE": "faster-whisper", "VOLT_PROXY_DIR": "p"})
        self.assertEqual((c.asr_engine, c.proxy_dir, c.publish_dir, c.asr_device), ("faster-whisper", Path("p"), None, "auto"))


if __name__ == "__main__":
    unittest.main()
