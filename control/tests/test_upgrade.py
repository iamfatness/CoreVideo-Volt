"""Rolling upgrades: the expand/contract skew guard, `volt migrate`, `volt upgrade plan`, `/ready` and drain, and the deployment files.

What this proves: an older build runs against a database that only gained additive migrations and refuses one that gained a breaking
one; the plan refuses a rolling upgrade across a breaking migration; a draining node says so on /ready, finishes its in-flight request
and exits. What it does not prove: a rolling upgrade on a real balancer or cluster (the compose and Kubernetes files are text checks
only, and unvalidated until rehearsal R2 in docs/superpowers/specs/2026-11-01-scale-operations-design.md has been run)."""

import contextlib
import http.client
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from volt import cli, health, migrations, serve
from volt.migrations import Migration, SchemaTooNewError
from volt.rundown import add_item, attach, open_show
from volt.store import Store

from tests.pgutil import DSN, PgCase
from tests.racekit import _cleared_span
from tests.test_multi_serve import FakeCoreHttp, Node

ROOT = Path(__file__).resolve().parents[2]
CONTROL = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"


def _newer(breaking: bool, n: int = 1) -> Migration:
    return Migration(migrations.LATEST + n, "synthetic newer change", breaking=breaking,
                     sqlite=("CREATE TABLE IF NOT EXISTS synthetic_newer (id TEXT PRIMARY KEY)",),
                     postgres=("CREATE TABLE IF NOT EXISTS synthetic_newer (id TEXT PRIMARY KEY)",))


class Skew:
    """One set of cases for both backends. Subclasses give `self.db` (something migrations.run can use) and `self.reopen()`."""

    def with_newer_code(self, *extra):
        """The database as a newer release leaves it: its migrations applied and recorded."""
        saved = migrations.MIGRATIONS
        migrations.MIGRATIONS = saved + tuple(extra)
        try:
            self.run_migrations()
        finally:
            migrations.MIGRATIONS = saved

    def test_old_code_runs_against_additive_newer_schema(self):
        self.with_newer_code(_newer(breaking=False))
        db = self.backend()
        self.assertGreater(migrations.current_version(db), migrations.known_version())
        self.assertEqual(migrations.required_code_version(db), 0)
        migrations.check_not_newer(db)                 # the older build is allowed to run and to restart
        self.run_migrations()                          # and a restart applies nothing and breaks nothing

    def test_old_code_refuses_breaking_newer_schema(self):
        extra = _newer(breaking=True)
        self.with_newer_code(extra)
        db = self.backend()
        self.assertEqual(migrations.required_code_version(db), extra.version)
        with self.assertRaises(SchemaTooNewError):
            migrations.check_not_newer(db)
        with self.assertRaises(SchemaTooNewError):
            self.run_migrations()

    def test_min_code_carried_forward_when_not_breaking(self):
        breaking, additive = _newer(True, 1), _newer(False, 2)
        additive = Migration(additive.version, "second", sqlite=additive.sqlite, postgres=additive.postgres)
        self.with_newer_code(breaking, additive)
        self.assertEqual(migrations.required_code_version(self.backend()), breaking.version,
                         "an additive migration after a breaking one must not lower the required code version")

    def test_a_build_that_knows_the_breaking_migration_may_run(self):
        extra = _newer(breaking=True)
        self.with_newer_code(extra)
        saved = migrations.MIGRATIONS
        migrations.MIGRATIONS = saved + (extra,)
        try:
            migrations.check_not_newer(self.backend())
        finally:
            migrations.MIGRATIONS = saved


class SqliteSkew(Skew, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "v.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def backend(self):
        return migrations.SqliteBackend(self.store.conn)

    def run_migrations(self):
        migrations.run(self.backend())
        self.store.conn.commit()


class PostgresSkew(Skew, PgCase):
    def setUp(self):
        super().setUp()
        self.s = self.store()

    def backend(self):
        return migrations.PostgresBackend(self.s.conn._conn)

    def run_migrations(self):
        with self.s.conn._conn.transaction():
            migrations.run(self.backend())


class VersionTable(unittest.TestCase):
    def test_existing_version_table_without_min_code_gains_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            store.conn.execute("ALTER TABLE schema_version RENAME TO old_version")
            store.conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            store.conn.execute("INSERT INTO schema_version SELECT version, name, applied_at FROM old_version")
            store.conn.execute("DROP TABLE old_version")
            store.conn.commit()
            store.close()
            store = Store(path)
            try:
                cols = {r[1] for r in store.conn.execute("PRAGMA table_info(schema_version)")}
                self.assertIn("min_code", cols)
                migrations.check_not_newer(migrations.SqliteBackend(store.conn))
            finally:
                store.close()

    def test_every_shipped_migration_is_additive(self):
        """The bridge release starts the expand/contract discipline; nothing shipped before it is breaking."""
        self.assertFalse([m.version for m in migrations.MIGRATIONS if m.breaking])


class Plan(unittest.TestCase):
    def test_plan_refuses_rolling_when_any_pending_is_breaking(self):
        applied = set(range(1, migrations.LATEST + 1))
        pending = [_newer(False, 1), _newer(True, 2)]
        plan = migrations.plan(applied, tuple(migrations.MIGRATIONS) + tuple(pending))
        self.assertFalse(plan.rolling_ok)
        self.assertEqual(plan.pending, [(migrations.LATEST + 1, False), (migrations.LATEST + 2, True)])
        self.assertTrue(any("breaking" in r for r in plan.reasons))

    def test_plan_allows_rolling_for_additive_only(self):
        applied = set(range(1, migrations.LATEST + 1))
        plan = migrations.plan(applied, tuple(migrations.MIGRATIONS) + (_newer(False),))
        self.assertTrue(plan.rolling_ok)
        self.assertEqual(plan.reasons, [])

    def test_nothing_pending_is_fine(self):
        plan = migrations.plan(set(range(1, migrations.LATEST + 1)), migrations.MIGRATIONS)
        self.assertTrue(plan.rolling_ok)
        self.assertEqual(plan.pending, [])

    def test_a_node_older_than_the_required_code_version_is_a_reason(self):
        applied = set(range(1, migrations.LATEST + 1)) | {migrations.LATEST + 1}
        plan = migrations.plan(applied, migrations.MIGRATIONS, required=migrations.LATEST + 1,
                               nodes=[{"name": "old-node", "knownVersion": migrations.LATEST}])
        self.assertFalse(plan.rolling_ok)
        self.assertTrue(any("old-node" in r for r in plan.reasons))


class MigrateCli(unittest.TestCase):
    def run_cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_volt_migrate_is_idempotent_on_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            code, out, _ = self.run_cli("migrate", "--db", path)
            self.assertEqual(code, 0)
            code, out2, _ = self.run_cli("migrate", "--db", path)
            self.assertEqual(code, 0)
            self.assertIn("nothing", out2)

    def test_upgrade_plan_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            Store(path).close()
            self.assertEqual(self.run_cli("upgrade", "plan", "--db", path)[0], 0)
            extra = _newer(breaking=True)
            with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (extra,)):
                code, out, _ = self.run_cli("upgrade", "plan", "--db", path)
            self.assertEqual(code, 2, out)
            self.assertIn("breaking", out)
            self.assertEqual(self.run_cli("upgrade", "plan", "--db", path, "--verify")[0], 0)
            code, _, err = self.run_cli("upgrade", "plan", env={"VOLT_DB": ""})
            self.assertEqual(code, 78)

    def test_verify_fails_when_the_database_is_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            Store(path).close()
            extra = _newer(breaking=False)
            with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (extra,)):
                code, out, _ = self.run_cli("upgrade", "plan", "--db", path, "--verify")
            self.assertNotEqual(code, 0)
            self.assertIn("behind", out)


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class MigrateTenants(unittest.TestCase):
    def test_volt_migrate_reports_per_tenant(self):
        import uuid
        from volt import pg, tenancy
        ctl = "volt_ctl_" + uuid.uuid4().hex[:10]
        reg = tenancy.Registry(DSN, control_schema=ctl)
        tag = uuid.uuid4().hex[:6]
        a = reg.create(f"alpha-{tag}", f"alpha-{tag}.volt.test", operator="t")
        b = reg.create(f"bravo-{tag}", f"bravo-{tag}.volt.test", operator="t")

        def cleanup():
            import psycopg
            pg.close_pools()
            with psycopg.connect(DSN, autocommit=True) as c:
                for s in (ctl, a.schema, b.schema):
                    c.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        self.addCleanup(cleanup)
        code, out, _ = MigrateCli().run_cli("migrate", "--tenants", "all", env={"VOLT_CONTROL_DSN": DSN, "VOLT_CONTROL_SCHEMA": ctl, "VOLT_TENANCY": "on"})
        self.assertEqual(code, 0, out)
        self.assertIn(a.slug, out)
        self.assertIn(b.slug, out)
        code, out, _ = MigrateCli().run_cli("migrate", env={"VOLT_CONTROL_DSN": DSN, "VOLT_CONTROL_SCHEMA": ctl, "VOLT_TENANCY": "on"})
        self.assertEqual(code, 0)
        self.assertEqual(out.count("nothing"), 2)


class Fingerprint(unittest.TestCase):
    def test_media_key_fingerprint_differs_when_key_differs_and_does_not_reveal_key(self):
        a, b = health.media_key_fingerprint("a" * 32), health.media_key_fingerprint("b" * 32)
        self.assertNotEqual(a, b)
        self.assertEqual(a, health.media_key_fingerprint("a" * 32))
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        self.assertNotIn("a" * 8, a)
        self.assertIsNone(health.media_key_fingerprint(None))
        self.assertIsNone(health.media_key_fingerprint(""))

    def test_api_health_carries_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            from volt import users
            _, token = users.add(store, "alice", "admin")
            saved = (serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.ROOM)
            serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.ROOM = path, None, "k" * 32, None
            srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
                c.request("GET", "/api/health", headers={"Authorization": f"Bearer {token}"})
                body = c.getresponse().read().decode()
                c.close()
            finally:
                srv.shutdown()
                srv.server_close()
                serve.DB_PATH, serve.TOKEN, serve.MEDIA_KEY, serve.ROOM = saved
                store.close()
        self.assertEqual(json.loads(body)["mediaKeyFingerprint"], health.media_key_fingerprint("k" * 32))
        self.assertNotIn("k" * 32, body)


class Ready(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "v.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM)
        serve.DB_PATH, serve.TOKEN, serve.ROOM = self.path, None, None
        self.addCleanup(lambda: setattr_all(serve, ("DB_PATH", "TOKEN", "ROOM"), self._saved))
        self.srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}"), dict(r.getheaders())
        finally:
            c.close()

    def test_ready_is_200_with_no_auth(self):
        status, body, _ = self.get("/ready")
        self.assertEqual((status, body["status"]), (200, "ready"))

    def test_ready_503_while_draining_and_health_still_ok(self):
        serve.DRAIN.begin()
        self.addCleanup(serve.DRAIN.reset)
        status, body, headers = self.get("/ready")
        self.assertEqual((status, body["status"]), (503, "draining"))
        self.assertEqual(headers.get("Connection"), "close")
        self.assertEqual(self.get("/health")[0], 200)

    def test_ready_503_when_the_database_cannot_be_read(self):
        serve.DB_PATH = str(Path(self.tmp.name) / "no-such-dir" / "v.db")
        status, body, _ = self.get("/ready")
        self.assertEqual((status, body["status"]), (503, "unavailable"))
        self.assertEqual(self.get("/health")[0], 200)


def setattr_all(obj, names, values):
    for n, v in zip(names, values):
        setattr(obj, n, v)


class Drain(unittest.TestCase):
    def _signal_stop(self, proc):
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)

    def test_drain_finishes_in_flight_request_then_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "v.db")
            store = Store(db)
            asset, span = _cleared_span(store)
            show = open_show(store, "Final")
            item = add_item(store, show, "one")
            attach(store, item, span)
            from volt.cues import add_cue
            add_cue(store, item, "corevideo.pro", "overlay.take", "on")
            store.close()
            core = FakeCoreHttp(delay=2.5)
            self.addCleanup(core.close)
            env = {"VOLT_ROOM_URL": core.url, "VOLT_MIN_FREE_MB": "1", "VOLT_DRAIN_SECONDS": "15"}
            node = Node(db, env, new_group=True)
            self.addCleanup(node.stop)
            result = {}

            def take():
                try:
                    result["take"] = node.post(f"/api/shows/{show}/take")
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc
            t = threading.Thread(target=take)
            t.start()
            self.assertTrue(core.entered.wait(30))
            self._signal_stop(node.proc)
            deadline = time.monotonic() + 10
            ready_status = None
            while time.monotonic() < deadline and ready_status != 503:
                try:
                    c = http.client.HTTPConnection("127.0.0.1", node.port, timeout=5)
                    c.request("GET", "/ready")
                    ready_status = c.getresponse().status
                    c.close()
                except OSError:
                    break
                time.sleep(0.05)
            self.assertEqual(ready_status, 503, "/ready never reported draining")
            t.join(30)
            self.assertNotIn("error", result, result)
            self.assertEqual(result["take"][0], 200, result)                     # the in-flight take finished
            node.proc.wait(30)
            self.assertIsNotNone(node.proc.returncode)
            with self.assertRaises(OSError):                                     # and the listener is gone
                socket_conn = http.client.HTTPConnection("127.0.0.1", node.port, timeout=2)
                socket_conn.request("GET", "/health")
                socket_conn.getresponse()


class DeployFiles(unittest.TestCase):
    def text(self, *parts):
        p = ROOT.joinpath(*parts)
        self.assertTrue(p.is_file(), f"{p} is missing")
        return p.read_text(encoding="utf-8")

    def test_deploy_files_exist_and_name_ready_probe_and_pdb(self):
        compose = self.text("deploy", "compose", "compose.yaml")
        self.assertIn("/ready", compose)
        self.assertIn("unvalidated", compose.lower())
        for service in ("serve-1:", "serve-2:", "runner", "clock:", "postgres:", "caddy:", "migrate:"):
            self.assertIn(service, compose)
        caddy = self.text("deploy", "compose", "Caddyfile")
        self.assertIn("health_uri /ready", caddy)
        serve_yaml = self.text("deploy", "kubernetes", "serve.yaml")
        self.assertIn("readinessProbe", serve_yaml)
        self.assertIn("/ready", serve_yaml)
        self.assertIn("preStop", serve_yaml)
        self.assertIn("terminationGracePeriodSeconds", serve_yaml)
        self.assertIn("PodDisruptionBudget", self.text("deploy", "kubernetes", "pdb.yaml"))
        self.assertIn("kind: Job", self.text("deploy", "kubernetes", "migrate-job.yaml"))
        self.assertIn('command: ["volt", "migrate"]', self.text("deploy", "kubernetes", "migrate-job.yaml"))
        for name in ("runner", "clock", "ingress"):
            self.assertIn("unvalidated", self.text("deploy", "kubernetes", f"{name}.yaml").lower())
        for name in ("serve", "pdb", "migrate-job"):
            self.assertIn("unvalidated", self.text("deploy", "kubernetes", f"{name}.yaml").lower())

    def test_no_deploy_file_carries_a_secret(self):
        for p in list((ROOT / "deploy" / "compose").glob("*")) + list((ROOT / "deploy" / "kubernetes").glob("*")):
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)(password|token|secret|key)\s*[:=]\s*[\"']?[A-Za-z0-9+/]{20,}", p.name)

    def test_the_scale_page_names_the_rehearsals_and_says_unvalidated(self):
        page = self.text("docs", "scale.md")
        self.assertIn("unvalidated", page.lower())
        for r in ("R1", "R2", "R3", "R4", "R5", "R6"):
            self.assertIn(r, page)
        self.assertIn("bridge release", page.lower())

    def test_changelog_names_the_bridge_release(self):
        self.assertIn("bridge release", self.text("CHANGELOG.md").lower())

    def test_stop_grace_covers_the_drain(self):
        from volt import supervisor
        self.assertGreaterEqual(supervisor.Supervisor.__dataclass_fields__["grace"].default, serve.DRAIN_DEFAULT_SECONDS + 5)


if __name__ == "__main__":
    unittest.main()
