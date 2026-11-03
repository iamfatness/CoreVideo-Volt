"""Tenancy on the request path: the tenant is found from the Host header before authentication, and nothing one tenant owns reaches another.

Two tenants on Postgres schemas, in-process servers, a fake room. Proves isolation through the HTTP layer for the routes listed here
(and fails when a new GET path appears that is not listed); it does not prove a hosted deployment, browser sign-in in tenant mode (not
built), or per-tenant file directories outside `serve` (the runner and ingest still read the process environment)."""

import ast
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from volt import health, pg, serve, tenancy, tickets, users
from volt.rooms import FakeRoom
from volt.rundown import add_item, attach, open_show
from volt.store import Store
from volt.tenancy import Registry

from tests.pgutil import DSN
from tests.racekit import _cleared_span
from tests.test_rooms import obs

MASTER = "m" * 32


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class TenantServer(unittest.TestCase):
    def setUp(self):
        pg.close_pools()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tag = uuid.uuid4().hex[:6]
        self.ctl = "volt_ctl_" + uuid.uuid4().hex[:10]
        self.registry = Registry(DSN, control_schema=self.ctl)
        self.refs = {}
        self.stores = []
        self.tokens = {}
        self.hosts = {}
        for name in ("alpha", "bravo"):
            host = f"{name}-{self.tag}.volt.test"
            ref = self.registry.create(f"{name}-{self.tag}", host, operator="tester")
            self.refs[name], self.hosts[name] = ref, host
            store = Store(ref.dsn, schema=ref.schema, init=False)
            self.stores.append(store)
            _, self.tokens[name] = users.add(store, "admin1", "admin")
            store.mint_asset(owner=f"MARK-{name.upper()}", show_name=f"MARK-{name.upper()}-show")
            open_show(store, f"MARK-{name.upper()}-rundown")
        self.addCleanup(self._drop)
        self.out_root = Path(self.tmp.name) / "out"
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.TENANTS, serve.OUT_DIR, serve.MEDIA_KEY, serve.REGISTRY,
                       dict(serve.POLL), serve.PUBLISH)
        serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.OUT_DIR, serve.MEDIA_KEY = DSN, None, None, self.out_root, MASTER
        serve.TENANTS = tenancy.TenantResolver(self.registry, out_root=self.out_root, master_key=MASTER)
        serve.REGISTRY = serve.telemetry.Registry()
        serve.POLL.update(attempts=2, interval=0, sleep=lambda _: None)
        self.addCleanup(self._restore)
        self.srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.TENANTS, serve.OUT_DIR, serve.MEDIA_KEY, serve.REGISTRY, poll,
         serve.PUBLISH) = self._saved
        serve.POLL.update(poll)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _drop(self):
        import psycopg
        pg.close_pools()
        for s in self.stores:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{self.ctl}" CASCADE')
            for ref in self.refs.values():
                admin.execute(f'DROP SCHEMA IF EXISTS "{ref.schema}" CASCADE')

    def call(self, method, path, host, token=None, body=None):
        h = {"Host": host}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if body is not None:
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=20)
        try:
            c.request(method, path, None if body is None else json.dumps(body), h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def a(self, method, path, body=None, who="alpha", on=None):
        return self.call(method, path, self.hosts[on or who], self.tokens[who], body)


class Resolution(TenantServer):
    def test_a_token_from_a_on_the_b_host_is_401(self):
        status, body, _ = self.call("GET", "/api/state", self.hosts["bravo"], self.tokens["alpha"])
        self.assertEqual(status, 401, body)
        self.assertEqual(self.a("GET", "/api/state", who="alpha")[0], 200)
        self.assertEqual(self.a("GET", "/api/state", who="bravo")[0], 200)

    def test_a_token_from_a_cannot_post_on_the_b_host(self):
        show = self.stores[1].conn.execute("SELECT id FROM show LIMIT 1").fetchone()["id"]
        status, body, _ = self.call("POST", f"/api/shows/{show}/hold", self.hosts["bravo"], self.tokens["alpha"], {"reason": "x"})
        self.assertEqual(status, 401, body)

    def test_unknown_host_is_plain_404_without_tenant_hint(self):
        for host in ("nobody.volt.test", "", "127.0.0.1", "localhost"):
            status, body, headers = self.call("GET", "/api/state", host, self.tokens["alpha"])
            self.assertEqual((status, body), (404, b"not found"), host)
            self.assertEqual(headers.get("Content-Type"), "text/plain")
        status, body, _ = self.call("POST", "/api/shows", "nobody.volt.test", self.tokens["alpha"], {"title": "x"})
        self.assertEqual(status, 404)
        self.assertNotIn(self.tag.encode(), body)

    def test_unknown_host_cannot_reach_static_or_health(self):
        self.assertEqual(self.call("GET", "/", "nobody.volt.test")[0], 404)
        self.assertEqual(self.call("GET", "/operator/operator.js", "nobody.volt.test")[0], 404)

    def test_host_is_case_and_port_insensitive(self):
        status, _, _ = self.call("GET", "/api/state", self.hosts["alpha"].upper() + ":8765", self.tokens["alpha"])
        self.assertEqual(status, 200)

    def test_tenant_is_never_taken_from_a_header_or_query(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            c.request("GET", f"/api/state?tenant={self.refs['bravo'].slug}", headers={
                "Host": self.hosts["alpha"], "X-Volt-Tenant": self.refs["bravo"].slug, "Authorization": f"Bearer {self.tokens['bravo']}"})
            r = c.getresponse()
            r.read()
        finally:
            c.close()
        self.assertEqual(r.status, 401)

    def test_suspended_tenant_is_403_but_health_answers(self):
        self.registry.suspend(self.refs["alpha"].slug, operator="t", reason="x")
        serve.TENANTS.clear()
        status, body, _ = self.a("GET", "/api/state")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "tenant_suspended")
        self.assertEqual(self.a("POST", "/api/shows", {"title": "x"})[0], 403)
        self.assertEqual(self.call("GET", "/health", self.hosts["alpha"])[0], 200)
        self.assertEqual(self.a("GET", "/api/state", who="bravo")[0], 200)

    def test_suspend_takes_effect_within_the_epoch_interval_without_a_clear(self):
        clock = [1000.0]
        serve.TENANTS = tenancy.TenantResolver(self.registry, out_root=self.out_root, master_key=MASTER,
                                               now=lambda: clock[0], ttl=60.0, epoch_every=5.0)
        self.assertEqual(self.a("GET", "/api/state")[0], 200)
        self.registry.suspend(self.refs["alpha"].slug, operator="t")
        self.assertEqual(self.a("GET", "/api/state")[0], 200)             # inside the 5 s window: the cached answer
        clock[0] += 6
        self.assertEqual(self.a("GET", "/api/state")[0], 403)             # the epoch moved: re-read at once, not after 60 s

    def test_resolver_serves_a_cached_context_between_epoch_checks(self):
        clock = [1000.0]
        calls = []
        real = self.registry.by_host
        self.registry.by_host = lambda h: (calls.append(h), real(h))[1]
        res = tenancy.TenantResolver(self.registry, out_root=self.out_root, master_key=MASTER, now=lambda: clock[0])
        for _ in range(20):
            self.assertIsNotNone(res.resolve(self.hosts["alpha"]))
        self.assertEqual(len(calls), 1)

    def test_resolver_bounds_its_negative_cache(self):
        res = tenancy.TenantResolver(self.registry, out_root=self.out_root, master_key=MASTER)
        for n in range(1500):
            res.resolve(f"scan-{n}.volt.test")
        self.assertLessEqual(res.size(), tenancy.TenantResolver.MAX_ENTRIES)


class Secrets(TenantServer):
    def test_media_ticket_from_a_does_not_verify_on_b_same_asset_id(self):
        for name in ("alpha", "bravo"):
            st = self.stores[0 if name == "alpha" else 1]
            st.conn.execute("INSERT INTO asset (id, owner, retention_class, legal_hold, cleared, created_at) "
                            "VALUES ('same-id', 'desk', 'show', FALSE, FALSE, '2026-01-01T00:00:00Z')")
            f = Path(self.tmp.name) / f"{name}.mp4"
            f.write_bytes(b"0123456789" * 10)
            st.add_essence("same-id", role="proxy", location=str(f))
            st.conn.commit()
        status, body, _ = self.a("GET", "/api/media-ticket?asset=same-id&kind=proxy", who="alpha")
        self.assertEqual(status, 200, body)
        url = json.loads(body)["url"]
        self.assertEqual(self.call("GET", url, self.hosts["alpha"])[0], 200)             # the ticket works on its own tenant
        status, body, _ = self.call("GET", url, self.hosts["bravo"])
        self.assertEqual(status, 401, "a ticket signed for tenant A verified on tenant B")

    def test_media_keys_differ_per_tenant_and_do_not_reveal_the_master(self):
        ka = tenancy.derive_media_key(MASTER, self.refs["alpha"].slug)
        kb = tenancy.derive_media_key(MASTER, self.refs["bravo"].slug)
        self.assertNotEqual(ka, kb)
        self.assertNotIn(MASTER, ka + kb)
        self.assertEqual(ka, tenancy.derive_media_key(MASTER, self.refs["alpha"].slug))
        self.assertNotEqual(ka, tenancy.derive_media_key("n" * 32, self.refs["alpha"].slug))

    def test_thumbnail_and_publish_paths_are_under_tenant_slug(self):
        ctx = serve.TENANTS.resolve(self.hosts["alpha"])
        self.assertEqual(ctx.settings.out_dir, self.out_root / self.refs["alpha"].slug)
        self.assertEqual(ctx.settings.storage_prefix, f"tenants/{self.refs['alpha'].slug}/")
        seen = []
        serve.PUBLISH = lambda store, span, out: seen.append(Path(out)) or []
        asset, span = _cleared_span(self.stores[0])
        status, body, _ = self.a("POST", f"/api/spans/{span}/publish", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(seen, [self.out_root / self.refs["alpha"].slug])
        self.assertEqual(serve.OUT_DIR, self.out_root, "the process-wide default must not have been rewritten for the request")

    def test_the_room_is_per_tenant_and_the_global_room_url_is_never_a_fallback(self):
        with mock.patch.dict(os.environ, {"VOLT_ROOM_URL": "http://global.invalid:1"}):
            status, body, _ = self.a("POST", "/api/record/start", {})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"]["code"], "no_room")
        self.registry.set_setting(self.refs["alpha"].slug, "room_url", "http://alpha-core.invalid:1", operator="t")
        serve.TENANTS.clear()
        ctx = serve.TENANTS.resolve(self.hosts["alpha"])
        self.assertEqual(ctx.settings.room_url, "http://alpha-core.invalid:1")
        self.assertIsNone(serve.TENANTS.resolve(self.hosts["bravo"]).settings.room_url)

    def test_a_room_secret_is_read_from_the_named_environment_variable_only(self):
        self.registry.set_setting(self.refs["alpha"].slug, "room_url", "http://alpha-core.invalid:1", operator="t")
        self.registry.set_setting(self.refs["alpha"].slug, "room_token_env", "TEST_ALPHA_ROOM_TOKEN", operator="t")
        with mock.patch.dict(os.environ, {"TEST_ALPHA_ROOM_TOKEN": "s3cret-token"}):
            serve.TENANTS.clear()
            ctx = serve.TENANTS.resolve(self.hosts["alpha"])
        self.assertEqual(ctx.settings.room_token, "s3cret-token")
        rows = self.stores[0].conn.execute("SELECT 1").fetchone()
        control = Registry(DSN, control_schema=self.ctl)
        with control._cx() as c:
            values = [r["value"] for r in c.execute("SELECT value FROM tenant_setting").fetchall()]
        self.assertNotIn("s3cret-token", values)
        with self.assertRaises(tenancy.TenantError):
            self.registry.set_setting(self.refs["alpha"].slug, "room_token", "plaintext", operator="t")


class Isolation(TenantServer):
    # Every GET path the server answers through `_route` (and its helpers) that returns tenant data. A path added to `_route` that is
    # not in one of these two sets fails test_every_get_path_is_accounted_for until someone adds it here, with a call below.
    COVERED = {"/api/health", "/metrics", "/api/state", "/assets", "/rundown", "/seek", "/api/audit", "/api/whoami", "/api/groups",
               "/api/users", "/api/media-ticket", "/media"}
    EXEMPT = {}      # path -> reason

    CALLS = ["/api/health", "/metrics", "/api/state", "/assets", "/api/audit?target=x", "/api/audit?limit=50", "/api/whoami",
             "/api/groups", "/api/users", "/api/shows", "/api/library/assets", "/api/devices", "/api/notifications", "/api/wire",
             "/api/search?q=MARK", "/api/schema", "/api/rooms", "/api/safe-mode"]

    def test_every_get_path_is_accounted_for(self):
        src = (Path(serve.__file__)).read_text(encoding="utf-8")
        body = src[src.index("    def _route(self, STORE):"):src.index("    def _send_headers")]
        literal = set(re.findall(r'parsed\.path == "(/[^"]*)"', body)) | set(
            p for tup in re.findall(r'parsed\.path in \(([^)]*)\)', body) for p in re.findall(r'"(/[^"]*)"', tup))
        missing = literal - self.COVERED - set(self.EXEMPT)
        self.assertEqual(missing, set(), "a GET path was added to Handler._route: add it to COVERED with a call in CALLS")

    def test_every_get_route_returns_only_own_rows(self):
        stores_by = {"alpha": self.stores[0], "bravo": self.stores[1]}
        stores_by["alpha"].append_audit("MARK-ALPHA-actor", "x", "show", "x", None)
        stores_by["bravo"].append_audit("MARK-BRAVO-actor", "x", "show", "x", None)
        for me, other in (("alpha", "bravo"), ("bravo", "alpha")):
            marker = f"MARK-{other.upper()}"
            own = f"MARK-{me.upper()}"
            seen_own = False
            for path in self.CALLS:
                status, body, _ = self.a("GET", path, who=me)
                text = body.decode("utf-8", "replace")
                self.assertNotIn(marker, text, f"{path} on {me} returned {other}'s data")
                self.assertNotIn(self.refs[other].slug, text, f"{path} on {me} named tenant {other}")
                self.assertNotIn(self.refs[other].schema, text)
                self.assertNotIn(self.hosts[other], text)
                seen_own = seen_own or own in text
            self.assertTrue(seen_own, "the calls never returned this tenant's own marker, so the check above proves nothing")

    def test_no_store_construction_outside_the_opener(self):
        allowed = {"open_request_store", "main"}
        for name in ("serve.py", "operator_state.py"):
            path = Path(serve.__file__).parent / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Store" and fn.name not in allowed:
                        self.fail(f"{name}:{node.lineno} builds a Store inside {fn.name}(); tenant requests must go through open_request_store")
            for node in tree.body:     # module level
                for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) else []:
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "Store":
                        self.fail(f"{name}:{sub.lineno} builds a Store at module level")

    def test_error_bodies_never_name_another_tenant(self):
        for path in ("/api/nope", "/api/assets/x/spans", "/api/shows/zzz/access"):
            status, body, _ = self.a("GET", path, who="alpha")
            text = body.decode("utf-8", "replace")
            self.assertNotIn(self.refs["bravo"].slug, text)
            self.assertNotIn(self.hosts["bravo"], text)
        status, body, _ = self.call("GET", "/api/state", self.hosts["bravo"], "wrong-token-wrong-token")
        self.assertEqual(status, 401)
        self.assertNotIn(self.refs["alpha"].slug.encode(), body)

    def test_health_and_metrics_are_tenant_scoped(self):
        self.a("GET", "/api/state")
        self.a("GET", "/api/state", who="bravo")
        status, body, _ = self.a("GET", "/metrics", who="alpha")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("# TYPE volt_up gauge", text)
        self.assertNotIn("volt_http_requests_total", text, "process-wide request series on a tenant host reveal other tenants' load")
        self.assertNotIn(self.refs["bravo"].slug, text)
        status, body, _ = self.a("GET", "/api/health", who="alpha")
        report = json.loads(body)
        self.assertNotIn(DSN.split("@")[0], body.decode(), "a DSN credential reached the health report")
        for entry in report.get("disk", []):
            self.assertNotIn(self.refs["bravo"].slug, entry["path"])

    def test_the_tenant_cannot_use_the_legacy_shared_token(self):
        serve.TOKEN = "legacy-token-legacy-token"      # even if one slipped in, it must not be an admin on a tenant host
        status, _, _ = self.call("GET", "/api/state", self.hosts["alpha"], "legacy-token-legacy-token")
        self.assertEqual(status, 401)

    def test_a_tenant_with_no_users_is_not_open_mode(self):
        ref = self.registry.create(f"empty-{self.tag}", f"empty-{self.tag}.volt.test", operator="t")
        self.refs["empty"] = ref
        status, body, _ = self.call("GET", "/api/state", f"empty-{self.tag}.volt.test")
        self.assertEqual(status, 401, body)
        status, _, _ = self.call("GET", "/", f"empty-{self.tag}.volt.test")
        self.assertEqual(status, 200)       # the static page is not data

    def test_writes_land_in_the_callers_schema_only(self):
        status, body, _ = self.a("POST", "/api/shows", {"title": "ONLY-IN-ALPHA"}, who="alpha")
        self.assertEqual(status, 200, body)
        a = self.stores[0].conn.execute("SELECT COUNT(*) FROM show WHERE title = 'ONLY-IN-ALPHA'").fetchone()[0]
        b = self.stores[1].conn.execute("SELECT COUNT(*) FROM show WHERE title = 'ONLY-IN-ALPHA'").fetchone()[0]
        self.assertEqual((a, b), (1, 0))

    def test_one_tenants_audit_rows_stay_in_its_schema(self):
        self.a("POST", "/api/shows", {"title": "x"}, who="alpha")
        rows_b = self.stores[1].conn.execute("SELECT COUNT(*) FROM audit WHERE actor = 'admin1'").fetchone()[0]
        rows_a = self.stores[0].conn.execute("SELECT COUNT(*) FROM audit WHERE actor = 'admin1'").fetchone()[0]
        self.assertEqual((rows_a >= 1, rows_b), (True, 0))

    def test_concurrent_requests_for_two_tenants_never_cross(self):
        errors = []

        def hammer(who):
            for _ in range(15):
                status, body, _ = self.a("GET", "/api/state", who=who)
                other = "BRAVO" if who == "alpha" else "ALPHA"
                if status != 200 or f"MARK-{other}".encode() in body:
                    errors.append((who, status))
        threads = [threading.Thread(target=hammer, args=(w,)) for w in ("alpha", "bravo", "alpha", "bravo")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(errors, [])


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class StartRules(unittest.TestCase):
    def _run(self, env, argv=None):
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("VOLT_TOKEN", "VOLT_OIDC_CONFIG"):
                if name not in env:
                    os.environ.pop(name, None)
            saved = serve.TOKEN
            serve.TOKEN = env.get("VOLT_TOKEN")
            try:
                with self.assertRaises(SystemExit) as raised:
                    serve.main(argv or ["--port", "0", "--db", DSN])
            finally:
                serve.TOKEN = saved
        return raised.exception.code

    def test_legacy_token_refused_when_tenancy_on(self):
        self.assertEqual(self._run({"VOLT_TENANCY": "on", "VOLT_TOKEN": "t" * 24, "VOLT_CONTROL_DSN": DSN}), 78)

    def test_oidc_config_refused_when_tenancy_on(self):
        self.assertEqual(self._run({"VOLT_TENANCY": "on", "VOLT_OIDC_CONFIG": "nowhere.json", "VOLT_CONTROL_DSN": DSN}), 78)

    def test_tenancy_needs_a_postgres_control_dsn(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = self._run({"VOLT_TENANCY": "on"}, ["--port", "0", "--db", str(Path(tmp) / "v.db")])
        self.assertEqual(code, 78)


class TenancyOff(unittest.TestCase):
    def test_host_is_ignored_and_behaviour_is_unchanged_when_tenancy_is_off(self):
        self.assertIsNone(serve.TENANTS)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            _, token = users.add(store, "alice", "operator")
            saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM)
            serve.DB_PATH, serve.TOKEN, serve.ROOM = path, None, None
            srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
                c.request("GET", "/api/state", headers={"Host": "anything.example", "Authorization": f"Bearer {token}"})
                r = c.getresponse()
                r.read()
                c.close()
                self.assertEqual(r.status, 200)
            finally:
                srv.shutdown()
                srv.server_close()
                serve.DB_PATH, serve.TOKEN, serve.ROOM = saved
                store.close()


if __name__ == "__main__":
    unittest.main()
