"""Health and metrics: /health (open), /api/health and /metrics (behind the operator token)."""

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from volt import __version__, health, migrations, serve
from volt.runner import Runner
from volt.store import Store, _now

from tests.pgutil import PgCase

TOKEN = "t" * 24


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = str(self.dir / "volt.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.out = self.dir / "out"
        env = mock.patch.dict(os.environ, {"VOLT_MIN_FREE_MB": "1", "VOLT_PROXY_DIR": str(self.dir / "proxy"),
                                           "VOLT_PUBLISH_DIR": str(self.dir / "pub")})
        env.start()
        self.addCleanup(env.stop)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self.path, None, self.out
        self.addCleanup(lambda: setattr_all(serve, ("DB_PATH", "TOKEN", "OUT_DIR"), self._saved))

    def job(self, kind="proxy", status="queued", **fields):
        asset = self.store.mint_asset(owner="desk")
        job = self.store.enqueue(asset.id, kind, f"k-{asset.id}")
        sets = {"status": status, **fields}
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.store.conn.execute(f"UPDATE job SET {cols} WHERE id = ?", (*sets.values(), job.id))
        self.store.conn.commit()
        return job

    def collect(self, now=None):
        return health.collect(self.store, self.out, db_target=self.path, now=now)


def setattr_all(module, names, values):
    for name, value in zip(names, values):
        setattr(module, name, value)


class Report(Base):
    def test_empty_database_is_ok(self):
        r = self.collect()
        self.assertEqual(r["status"], "ok", r["problems"])
        self.assertEqual(r["version"], __version__)
        self.assertEqual(r["database"]["schemaVersion"], migrations.LATEST)
        self.assertEqual(r["database"]["schemaState"], "current")
        self.assertEqual(r["queue"]["queued"], 0)
        self.assertIsNone(r["queue"]["oldestQueuedAgeSeconds"])
        self.assertEqual(r["runners"]["count"], 0)
        self.assertIsNone(r["asRun"]["lastAsRun"])

    def test_queue_depth_by_type_and_state_and_oldest_age(self):
        now = datetime.now(timezone.utc)
        self.job("proxy", created_at=_stamp(now - timedelta(seconds=300)))
        self.job("proxy", created_at=_stamp(now - timedelta(seconds=10)))
        self.job("asr", status="failed")
        self.job("asr", status="done")
        q = self.collect(now)["queue"]
        self.assertEqual(q["byTypeAndState"]["proxy"], {"queued": 2})
        self.assertEqual(q["byTypeAndState"]["asr"], {"failed": 1, "done": 1})
        self.assertEqual((q["queued"], q["failed"]), (2, 1))
        self.assertAlmostEqual(q["oldestQueuedAgeSeconds"], 300, delta=2)

    def test_backoff_wait_is_not_counted_as_due(self):
        now = datetime.now(timezone.utc)
        self.job("proxy", run_after=_stamp(now + timedelta(minutes=5)))
        q = self.collect(now)["queue"]
        self.assertEqual((q["queued"], q["queuedDue"]), (1, 0))
        self.assertEqual(self.collect(now)["status"], "ok")

    def test_waiting_work_with_no_runner_is_degraded_and_a_heartbeat_clears_it(self):
        self.job("proxy")
        r = self.collect()
        self.assertEqual(r["status"], "degraded")
        self.assertTrue(any("no runner" in p for p in r["problems"]), r["problems"])
        runner = Runner(self.store, {}, target=self.path)
        runner.beat(force=True)
        r = self.collect()
        self.assertEqual(r["runners"]["alive"], 1)
        self.assertLess(r["runners"]["newestHeartbeatAgeSeconds"], 5)
        self.assertEqual(r["status"], "ok", r["problems"])
        later = datetime.now(timezone.utc) + timedelta(minutes=10)  # the runner stopped beating
        r = self.collect(later)
        self.assertEqual(r["runners"]["alive"], 0)
        self.assertGreater(r["runners"]["newestHeartbeatAgeSeconds"], 500)
        self.assertEqual(r["status"], "degraded")

    def test_beat_is_throttled_and_idempotent(self):
        runner = Runner(self.store, {}, target=self.path)
        runner.beat()
        runner.beat()
        runner.beat(force=True)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM runner_heartbeat").fetchone()[0], 1)

    def test_expired_lease_and_long_running_jobs_are_stuck(self):
        now = datetime.now(timezone.utc)
        self.job("proxy", status="running", claimed_by="r1", lease_expires_at=_stamp(now - timedelta(seconds=30)),
                 updated_at=_stamp(now - timedelta(seconds=60)))
        self.job("asr", status="running", claimed_by="r2", lease_expires_at=_stamp(now + timedelta(seconds=60)),
                 updated_at=_stamp(now - timedelta(hours=6)))
        self.job("asr", status="running", claimed_by="r2", lease_expires_at=_stamp(now + timedelta(seconds=60)),
                 updated_at=_stamp(now - timedelta(minutes=5)))
        q = self.collect(now)["queue"]
        self.assertEqual(q["running"], 3)
        self.assertEqual(q["stuckCount"], 2)
        reasons = sorted(s["reason"] for s in q["stuck"])
        self.assertTrue(reasons[0].startswith("lease expired"))
        self.assertTrue(reasons[1].startswith("running for more than"))
        self.assertEqual(self.collect(now)["status"], "degraded")

    def test_stuck_threshold_is_configurable(self):
        now = datetime.now(timezone.utc)
        self.job("asr", status="running", claimed_by="r", lease_expires_at=_stamp(now + timedelta(seconds=60)),
                 updated_at=_stamp(now - timedelta(minutes=5)))
        with mock.patch.dict(os.environ, {"VOLT_STUCK_JOB_SECONDS": "60"}):
            self.assertEqual(self.collect(now)["queue"]["stuckCount"], 1)

    def test_last_as_run(self):
        self.store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s','S',?)", (_now(),))
        self.store.conn.execute(
            "INSERT INTO item (id, show_id, position, slug, status, created_at, as_run_in, as_run_out) "
            "VALUES ('i','s',1,'x','aired',?,?,?)", (_now(), "2026-10-10T20:00:01Z", "2026-10-10T20:00:09Z"))
        self.store.conn.commit()
        r = self.collect(datetime(2026, 10, 10, 20, 1, 9, tzinfo=timezone.utc))["asRun"]
        self.assertEqual((r["lastAsRun"], r["lastAsRunAgeSeconds"]), ("2026-10-10T20:00:09Z", 60))

    def test_low_disk_degrades_and_names_the_directory(self):
        with mock.patch.dict(os.environ, {"VOLT_MIN_FREE_MB": "999999999"}):
            r = self.collect()
        self.assertEqual(r["status"], "degraded")
        self.assertTrue(any(d["low"] for d in r["disk"]))
        self.assertTrue(any("low disk space on out" in p for p in r["problems"]), r["problems"])

    def test_disk_lines_cover_the_configured_directories(self):
        names = {d["name"] for d in self.collect()["disk"]}
        self.assertTrue({"out", "proxy", "publish", "database"} <= names, names)

    def test_database_newer_than_the_code_is_flagged(self):
        self.store.conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (?, 'future', ?)",
                                (migrations.LATEST + 5, _now()))
        self.store.conn.commit()
        r = self.collect()
        self.assertEqual(r["database"]["schemaState"], "newer than this code")
        self.assertEqual(r["status"], "degraded")

    def test_unreadable_database_is_down(self):
        self.store.conn.close()
        r = self.collect()
        self.assertEqual(r["status"], "down")
        self.assertFalse(r["database"]["reachable"])
        self.assertIn("volt_up 0", health.prometheus(r))

    def test_storage_config_errors_are_reported_not_raised(self):
        with mock.patch.dict(os.environ, {"VOLT_STORAGE": "s3", "VOLT_S3_BUCKET": ""}):
            r = self.collect()
        self.assertFalse(r["storage"]["ok"])
        self.assertEqual(r["status"], "degraded")
        with mock.patch.dict(os.environ, {"VOLT_STORAGE": "tape"}):
            self.assertFalse(self.collect()["storage"]["ok"])
        self.assertTrue(self.collect()["storage"]["ok"])  # local publish dir under the temp dir

    def test_thumb_cache_size_is_reported(self):
        (self.out / "thumbs" / "a").mkdir(parents=True)
        (self.out / "thumbs" / "a" / "1.jpg").write_bytes(b"x" * 123)
        health._size_cache.clear()
        self.assertEqual(self.collect()["thumbCache"]["bytes"], 123)

    def test_report_never_contains_secrets(self):
        with mock.patch.dict(os.environ, {"VOLT_TOKEN": "operator-token-xyz-123456",
                                          "VOLT_DB": "postgres://u:dbpass-9876@h/d"}):
            text = json.dumps(self.collect()) + health.prometheus(self.collect())
        self.assertNotIn("operator-token-xyz-123456", text)
        self.assertNotIn("dbpass-9876", text)


class Prometheus(Base):
    def test_text_format(self):
        self.job("proxy")
        self.job("asr", status="failed")
        text = health.prometheus(self.collect())
        self.assertIn("# TYPE volt_jobs gauge", text)
        self.assertIn('volt_jobs{type="proxy",state="queued"} 1', text)
        self.assertIn('volt_jobs{type="asr",state="failed"} 1', text)
        self.assertIn("volt_up 1", text)
        self.assertIn(f'volt_info{{version="{__version__}"}} 1', text)
        self.assertIn('volt_disk_free_bytes{dir="out"}', text)
        for line in text.splitlines():
            self.assertTrue(line.startswith("#") or len(line.rsplit(" ", 1)) == 2, line)


class Http(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, token=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if host:
            headers["Host"] = host
        try:
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        finally:
            conn.close()

    def test_liveness_is_open_and_carries_no_data(self):
        serve.TOKEN = TOKEN
        status, ctype, body = self.get("/health")
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertEqual(json.loads(body), {"status": "ok", "version": __version__})

    def test_liveness_answers_a_foreign_host_header_too(self):
        status, _, body = self.get("/health", host="lb.example")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"status", "version"})

    def test_detail_routes_need_the_token_when_one_is_set(self):
        serve.TOKEN = TOKEN
        for path in ("/api/health", "/metrics"):
            self.assertEqual(self.get(path)[0], 401, path)
            self.assertEqual(self.get(path, token="wrong" * 6)[0], 401, path)
            self.assertEqual(self.get(path, token=TOKEN)[0], 200, path)

    def test_detail_routes_without_a_token_are_loopback_only(self):
        self.assertEqual(self.get("/api/health")[0], 200)
        self.assertEqual(self.get("/api/health", host="evil.example")[0], 403)
        self.assertEqual(self.get("/metrics", host="evil.example")[0], 403)

    def test_api_health_body_and_metrics_body(self):
        self.job("proxy")
        status, ctype, body = self.get("/api/health")
        report = json.loads(body)
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertEqual(report["queue"]["queued"], 1)
        self.assertEqual(report["version"], __version__)
        status, ctype, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/plain"))
        self.assertIn(b"volt_up 1", body)


class PostgresHealth(PgCase):
    """The same report against Postgres, where timestamps come back as datetimes and the heartbeat upsert is real SQL."""

    def test_report_runner_beat_stuck_jobs_and_preflight(self):
        from volt import preflight
        store = self.store()
        with mock.patch.dict(os.environ, {"VOLT_MIN_FREE_MB": "1"}):
            r = health.collect(store, tempfile.gettempdir())
            self.assertEqual((r["status"], r["database"]["kind"]), ("ok", "postgres"))
            self.assertEqual(r["database"]["schemaState"], "current")
            asset = store.mint_asset(owner="desk")
            job = store.enqueue(asset.id, "proxy", "k1")
            r = health.collect(store, tempfile.gettempdir())
            self.assertEqual((r["queue"]["queued"], r["status"]), (1, "degraded"))  # no runner yet
            runner = Runner(store, {}, target=self.dsn)
            runner.beat(force=True)
            runner.beat(force=True)  # upsert twice: one row
            r = health.collect(store, tempfile.gettempdir())
            self.assertEqual((r["runners"]["count"], r["runners"]["alive"], r["status"]), (1, 1, "ok"))
            now = datetime.now(timezone.utc)
            store.conn.execute("UPDATE job SET status = 'running', claimed_by = 'r', lease_expires_at = ?, updated_at = ? "
                               "WHERE id = ?", (_stamp(now - timedelta(seconds=30)), _stamp(now - timedelta(seconds=90)), job.id))
            store.conn.commit()
            r = health.collect(store, tempfile.gettempdir())
            self.assertEqual(r["queue"]["stuckCount"], 1)
            self.assertIn("lease expired", r["queue"]["stuck"][0]["reason"])
            text = health.prometheus(r)
            self.assertIn("volt_jobs_stuck 1", text)
            results = {x.name: x for x in preflight.run_checks(env={"VOLT_MIN_FREE_MB": "1"}, db=self.dsn, port=0)}
        self.assertEqual(results["database"].status, preflight.OK, results["database"].detail)
        self.assertIn(":***@", results["database"].detail)  # the password is blanked


if __name__ == "__main__":
    unittest.main()
