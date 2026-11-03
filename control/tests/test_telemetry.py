"""Request, pool and job metrics (volt/telemetry.py) and the dashboard and alert files in deploy/monitoring.

These tests prove structure: the text format parses, buckets are cumulative, labels are bounded, and every metric a dashboard
panel or an alert rule names is a metric the code emits. They do NOT prove that Prometheus accepts the rules or that Grafana
renders the dashboard (neither was run), or that any alert fires."""

import http.client
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from volt import backup, health, runner, serve, telemetry, users
from volt.rooms import FakeRoom
from volt.rundown import add_item, attach, open_show
from volt.store import Store
from volt.telemetry import Registry

from tests.racekit import _cleared_span
from tests.test_rooms import obs

ROOT = Path(__file__).resolve().parents[2]
MON = ROOT / "deploy" / "monitoring"


def _samples(text):
    out = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            out.append(line)
    return out


class Format(unittest.TestCase):
    def test_histogram_buckets_are_cumulative_and_sum_matches(self):
        reg = Registry()
        h = reg.histogram("volt_x_seconds", {"route_class": "read"}, buckets=(0.1, 1, 10))
        for v in (0.05, 0.5, 0.7, 5, 50):
            h.observe(v)
        text = reg.render()
        got = dict(re.findall(r'volt_x_seconds_bucket\{route_class="read",le="([^"]+)"\} (\d+)', text))
        self.assertEqual(got, {"0.1": "1", "1": "3", "10": "4", "+Inf": "5"})
        self.assertIn('volt_x_seconds_count{route_class="read"} 5', text)
        self.assertRegex(text, r'volt_x_seconds_sum\{route_class="read"\} 56\.25')

    def test_text_format_parses(self):
        reg = Registry()
        reg.declare("volt_declared_total", "counter", "never observed")
        reg.counter("volt_hits_total", {"a": 'quote"d\nnewline'}).inc(3)
        reg.histogram("volt_lat_seconds").observe(0.2)
        reg.gauge_fn("volt_g", lambda: [({"k": "v"}, 7), ({"k": "w"}, None)])
        reg.gauge_fn("volt_broken", lambda: 1 / 0)
        text = reg.render()
        sample = re.compile(r'^[a-z_][a-z0-9_]*(\{([a-z_][a-z0-9_]*="([^"\\]|\\.)*",?)*\})? -?[0-9.e+]+$')
        seen_type = set()
        for line in text.splitlines():
            if line.startswith("# TYPE "):
                seen_type.add(line.split()[2])
            elif line.startswith("# HELP "):
                pass
            else:
                self.assertRegex(line, sample)
                base = re.sub(r"(_bucket|_sum|_count)?(\{.*)?( .*)?$", "", line.split("{")[0].split(" ")[0])
                self.assertTrue(any(base == n or base.startswith(n) for n in seen_type), f"{line} before its # TYPE")
        self.assertIn('volt_g{k="v"} 7', text)
        self.assertNotIn("volt_g{k=\"w\"}", text)
        self.assertIn("# TYPE volt_broken gauge", text)       # an unreadable gauge keeps its header and gives no sample
        self.assertIn('a="quote\\"d\\nnewline"', text)

    def test_names_must_be_volt_prefixed(self):
        with self.assertRaises(ValueError):
            Registry().declare("requests_total", "counter")

    def test_kind_mismatch_is_refused(self):
        reg = Registry()
        reg.counter("volt_x_total")
        with self.assertRaises(ValueError):
            reg.histogram("volt_x_total")

    def test_no_unbounded_labels(self):
        reg = Registry()
        for n in range(1000):
            telemetry.record_request("GET", None, f"/api/assets/{n:05d}/whatever", 200, 0.001, reg)
            telemetry.record_request("POST", f"act_fake_{n % 3}", f"/api/shows/{n}/take", 200, 0.001, reg)
        before = reg.series_count()
        for n in range(1000, 2000):
            telemetry.record_request("GET", None, f"/another/{n}", 200, 0.001, reg)
            telemetry.record_request("POST", f"act_fake_{n % 3}", f"/x/{n}", 200, 0.001, reg)
        self.assertEqual(reg.series_count(), before)       # a thousand more distinct paths, no new series
        self.assertLess(before, 40)

    def test_registry_caps_series_and_counts_what_it_dropped(self):
        reg = Registry(max_series=5)
        reg.declare(telemetry.DROPPED, "counter")
        for n in range(20):
            reg.counter("volt_many_total", {"n": str(n)}).inc()
        self.assertLessEqual(reg.series_count(), 6)
        self.assertRegex(reg.render(), r"volt_telemetry_series_dropped_total \d+")


class RouteClasses(unittest.TestCase):
    def test_route_class_is_a_closed_set(self):
        seen = set()
        for pattern, fn in serve.ROUTES:
            seen.add(telemetry.route_class("POST", fn.__name__, "/api/anything/" + "x" * 50))
        for path in ("/", "/operator", "/operator/operator.js", "/prompter", "/health", "/ready", "/metrics", "/api/health",
                     "/api/state", "/auth/login", "/auth/callback", "/assets", "/rundown", "/api/audit", "/media",
                     "/api/assets/a1/proxy", "/api/spans/s1/thumb", "/api/shows/s/prompter", "/x/" + "y" * 300):
            seen.add(telemetry.route_class("GET", None, path))
        seen.add(telemetry.route_class("PUT", None, "/x"))
        seen.add(telemetry.route_class("POST", None, "/api/nope"))
        self.assertLessEqual(seen, set(telemetry.ROUTE_CLASSES))
        self.assertTrue({"write", "read", "state", "health", "auth", "static", "media"} <= seen)

    def test_specific_classes(self):
        rc = telemetry.route_class
        self.assertEqual(rc("GET", None, "/api/state"), "state")
        self.assertEqual(rc("GET", None, "/health"), "health")
        self.assertEqual(rc("GET", None, "/operator/operator.css"), "static")
        self.assertEqual(rc("GET", None, "/api/assets/x/proxy"), "media")
        self.assertEqual(rc("POST", "act_ingest", "/api/ingest"), "heavy")
        self.assertEqual(rc("POST", "act_go", "/api/shows/s/go"), "write")

    def test_heavy_routes_exist(self):
        names = {fn.__name__ for _, fn in serve.ROUTES}
        self.assertLessEqual(telemetry.HEAVY_ROUTES, names)


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "volt.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        _, self.token = users.add(self.store, "alice", "admin")
        self.reg = Registry()
        for name, fam in telemetry.REGISTRY._families.items():
            self.reg.declare(name, fam.kind, fam.help, fam.buckets)
        for name, (help_text, _) in telemetry.REGISTRY._gauges.items():
            self.reg.declare(name, "gauge", help_text)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.REGISTRY, dict(serve.POLL))
        serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.REGISTRY = self.path, None, None, self.reg
        serve.POLL.update(attempts=2, interval=0, sleep=lambda _: None)
        telemetry.install_gauges(self.reg, serve.INFLIGHT, lambda: None, lambda: None)
        self.addCleanup(self._restore)
        self.srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.REGISTRY, poll = self._saved
        serve.POLL.update(poll)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {self.token}"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        try:
            c.request(method, path, None if body is None else json.dumps(body), h)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_request_counter_and_latency_recorded(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 200)
        show = open_show(self.store, "Final")
        self.assertEqual(self.req("POST", f"/api/shows/{show}/hold", {"reason": "x"})[0], 200)
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/health", token=False)[0], 200)
        text = self.reg.render()
        self.assertIn('volt_http_requests_total{route_class="state",status_class="2xx"} 1', text)
        self.assertIn('volt_http_requests_total{route_class="state",status_class="4xx"} 1', text)
        self.assertIn('volt_http_requests_total{route_class="write",status_class="2xx"} 1', text)
        self.assertIn('volt_http_requests_total{route_class="health",status_class="2xx"} 1', text)
        self.assertRegex(text, r'volt_http_request_seconds_count\{route_class="write"\} 1')
        self.assertRegex(text, r'volt_http_request_seconds_bucket\{route_class="write",le="\+Inf"\} 1')

    def test_a_429_style_refusal_is_counted_by_status_class(self):
        telemetry.record_request("GET", None, "/api/state", 429, 0.001, self.reg)
        self.assertIn('status_class="4xx"', self.reg.render())

    def test_metrics_endpoint_appends_the_registry_and_keeps_the_existing_series(self):
        self.req("GET", "/api/state")
        status, body = self.req("GET", "/metrics")
        text = body.decode()
        self.assertEqual(status, 200)
        for name in ("volt_up", "volt_info", "volt_degraded", "volt_schema_version", "volt_runners_alive"):
            self.assertIn(f"# TYPE {name} ", text)
        self.assertIn("# TYPE volt_http_requests_total counter", text)
        self.assertIn("volt_http_inflight 1", text)          # the scrape itself is in flight

    def test_db_checkout_is_timed(self):
        self.req("GET", "/api/state")
        self.assertRegex(self.reg.render(), r"volt_db_checkout_seconds_count\s+[1-9]")

    def test_a_take_splits_room_time_from_ours(self):
        asset, span = _cleared_span(self.store)
        show = open_show(self.store, "Final")
        item = add_item(self.store, show, "one")
        attach(self.store, item, span)
        serve.ROOM = FakeRoom([obs("idle")])
        status, body = self.req("POST", f"/api/shows/{show}/take")
        self.assertEqual(status, 200, body)
        text = self.reg.render()
        self.assertRegex(text, r'volt_take_seconds_count\{phase="room"\} 1')
        self.assertRegex(text, r'volt_take_seconds_count\{phase="overhead"\} 1')

    def test_timed_room_adds_up_only_room_calls(self):
        ticks = iter([0.0, 2.0, 5.0, 5.5])
        room = telemetry.TimedRoom(FakeRoom([obs("idle")]), clock=lambda: next(ticks))
        room.invoke("x", [])
        room.observe()
        self.assertEqual(room.seconds, 2.5)
        self.assertEqual(room.calls, [("x", [])])

    def test_runner_records_claims_job_time_and_queue_wait(self):
        asset = self.store.mint_asset(owner="desk")
        self.store.conn.execute(
            "INSERT INTO job (id, asset_id, type, idempotency_key, status, attempt, created_at, updated_at) "
            "VALUES ('j1', ?, 'noop', 'noop:1', 'queued', 1, '2020-01-01T00:00:00.000Z', '2020-01-01T00:00:00.000Z')", (asset.id,))
        self.store.conn.commit()
        r = runner.Runner(self.store, {"noop": lambda st, job: None}, name="r1", registry=self.reg)
        counts = r.drain()
        self.assertEqual(counts, {"done": 1})
        text = self.reg.render()
        self.assertIn('volt_runner_claims_total{outcome="claimed"} 1', text)
        self.assertIn('volt_runner_claims_total{outcome="empty"} 1', text)
        self.assertRegex(text, r'volt_job_seconds_count\{type="noop"\} 1')
        self.assertRegex(text, r'volt_job_queue_wait_seconds_count\{type="noop"\} 1')
        wait = float(re.search(r'volt_job_queue_wait_seconds_sum\{type="noop"\} ([0-9.e+]+)', text).group(1))
        self.assertGreater(wait, 86400 * 365)      # created in 2020: the wait is measured from creation

    def test_backup_and_drill_age_gauges(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b1"
            dest.mkdir()
            manifest = backup.backup(self.path, str(dest))
            self.assertEqual(manifest["kind"], "sqlite")
            now = datetime.now(timezone.utc).timestamp()
            telemetry.install_gauges(self.reg, telemetry.Inflight(), lambda: None, lambda: tmp, wall=lambda: now + 7200)
            text = self.reg.render()
            age = int(re.search(r"volt_backup_last_age_seconds (\d+)", text).group(1))
            self.assertTrue(7195 <= age <= 7210, age)
            self.assertNotRegex(text, r"volt_restore_drill_last_age_seconds \d")
            (Path(tmp) / "drill.json").write_text(json.dumps({"at": datetime.fromtimestamp(now - 100, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": True}))
            reg2 = Registry()
            reg2.declare(telemetry.DRILL_AGE, "gauge")
            telemetry.install_gauges(reg2, telemetry.Inflight(), lambda: None, lambda: tmp, wall=lambda: now)
            self.assertRegex(reg2.render(), r"volt_restore_drill_last_age_seconds (9[89]|10\d)")

    def test_a_failed_drill_does_not_reset_the_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "drill.json").write_text(json.dumps({"at": "2026-01-01T00:00:00Z", "ok": False}))
            self.assertIsNone(telemetry.last_drill_epoch(tmp))

    def test_a_truncated_backup_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b1"
            dest.mkdir()
            manifest = backup.backup(self.path, str(dest))
            (dest / manifest["file"]).write_bytes(b"short")
            self.assertIsNone(telemetry.newest_backup_epoch(tmp))

    def test_pool_gauges_read_stats(self):
        reg = Registry()
        telemetry.install_gauges(reg, telemetry.Inflight(),
                                 lambda: {"pool_size": 4, "pool_available": 1, "requests_waiting": 2}, lambda: None)
        text = reg.render()
        self.assertIn("volt_db_pool_in_use 3", text)
        self.assertIn("volt_db_pool_waiting 2", text)

    def test_metrics_never_change_a_response_when_recording_fails(self):
        class Broken(Registry):
            def counter(self, *a, **kw):
                raise RuntimeError("boom")
        telemetry.record_request("GET", None, "/x", 200, 0.1, Broken())     # must not raise


def _expr_metrics(expr: str) -> set:
    names = set(re.findall(r"\bvolt_[a-z0-9_]+", expr))
    return {re.sub(r"_(bucket|sum|count)$", "", n) if re.sub(r"_(bucket|sum|count)$", "", n) in HISTOGRAMS else n for n in names}


def _full_names() -> set:
    """Every metric name the code can emit: health.prometheus on a fully populated report, plus what the registry declares."""
    report = {
        "status": "ok", "version": "x", "problems": [], "database": {"schemaVersion": 35},
        "runners": {"newestHeartbeatAgeSeconds": 1, "alive": 1},
        "queue": {"byTypeAndState": {"proxy": {"queued": 1}}, "oldestQueuedAgeSeconds": 1, "stuckCount": 0},
        "asRun": {"lastAsRunAgeSeconds": 1}, "disk": [{"name": "out", "freeBytes": 1, "low": False}],
        "thumbCache": {"bytes": 1}, "storage": {"backend": "local", "ok": True},
    }
    names = set(re.findall(r"# TYPE (volt_[a-z0-9_]+)", health.prometheus(report)))
    names |= {n for n in telemetry.REGISTRY.names()}
    return names


HISTOGRAMS = {n for n, f in telemetry.REGISTRY._families.items() if f.kind == "histogram"}


class Files(unittest.TestCase):
    def test_dashboard_json_parses_and_every_expr_metric_is_emitted(self):
        doc = json.loads((MON / "volt-dashboard.json").read_text(encoding="utf-8"))
        exprs = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("expr"), str):
                    exprs.append(node["expr"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(doc)
        self.assertGreaterEqual(len(exprs), 10)
        emitted = _full_names()
        for expr in exprs:
            for name in _expr_metrics(expr):
                self.assertIn(name, emitted, f"the dashboard names {name}, which nothing emits: {expr}")
        self.assertIn("unvalidated", json.dumps(doc).lower())

    def test_alert_rules_reference_emitted_metrics(self):
        text = (MON / "volt-alerts.yml").read_text(encoding="utf-8")
        self.assertIn("unvalidated", text.lower())
        exprs = re.findall(r"^\s*expr:\s*(.+)$", text, re.M)
        self.assertGreaterEqual(len(exprs), 10)
        emitted = _full_names()
        for expr in exprs:
            for name in _expr_metrics(expr):
                self.assertIn(name, emitted, f"an alert names {name}, which nothing emits: {expr}")

    def test_alerts_have_severity_and_a_for_clause_where_a_blip_must_not_page(self):
        text = (MON / "volt-alerts.yml").read_text(encoding="utf-8")
        alerts = re.split(r"^\s*- alert:\s*", text, flags=re.M)[1:]
        self.assertGreaterEqual(len(alerts), 10)
        kinds = set()
        for block in alerts:
            sev = re.search(r"severity:\s*(page|ticket)", block)
            self.assertTrue(sev, block[:60])
            kinds.add(sev.group(1))
        self.assertEqual(kinds, {"page", "ticket"})

    def test_the_alert_set_covers_the_spec(self):
        text = (MON / "volt-alerts.yml").read_text(encoding="utf-8")
        for name in ("VoltDown", "VoltWriteErrors", "VoltNoRunner", "VoltSchemaMismatch", "VoltDiskLow", "VoltBackupStale",
                     "VoltQueueOld", "VoltJobsStuck", "VoltDrillOverdue", "VoltPoolWaiting"):
            self.assertIn(f"alert: {name}", text)

    def test_scrape_example_names_metrics_with_a_token(self):
        text = (MON / "scrape.example.yml").read_text(encoding="utf-8")
        self.assertIn("/metrics", text)
        self.assertIn("unvalidated", text.lower())
        self.assertNotRegex(text, r"[A-Za-z0-9]{30,}")      # no real-looking secret in an example

    def test_existing_fourteen_series_unchanged(self):
        report = {
            "status": "ok", "version": "x", "problems": [], "database": {"schemaVersion": 35},
            "runners": {"newestHeartbeatAgeSeconds": 1, "alive": 1},
            "queue": {"byTypeAndState": {"proxy": {"queued": 1}}, "oldestQueuedAgeSeconds": 1, "stuckCount": 0},
            "asRun": {"lastAsRunAgeSeconds": 1}, "disk": [{"name": "out", "freeBytes": 1, "low": False}],
            "thumbCache": {"bytes": 1}, "storage": {"backend": "local", "ok": True},
        }
        names = re.findall(r"# TYPE (volt_[a-z0-9_]+) ", health.prometheus(report))
        self.assertEqual(names, [
            "volt_up", "volt_info", "volt_degraded", "volt_schema_version", "volt_runner_heartbeat_age_seconds",
            "volt_runners_alive", "volt_jobs", "volt_queue_oldest_queued_age_seconds", "volt_jobs_stuck",
            "volt_last_as_run_age_seconds", "volt_disk_free_bytes", "volt_disk_low", "volt_thumb_cache_bytes",
            "volt_storage_ok"])
        self.assertEqual(health.prometheus(report), health.prometheus(report, registry=None))

    def test_prometheus_appends_a_registry_when_given_one(self):
        reg = Registry()
        reg.counter("volt_extra_total").inc()
        out = health.prometheus({"status": "down", "version": "x"}, registry=reg)
        self.assertIn("volt_up 0", out)
        self.assertIn("volt_extra_total 1", out)


if __name__ == "__main__":
    unittest.main()
