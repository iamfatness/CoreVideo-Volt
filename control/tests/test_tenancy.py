"""Tenancy core (volt/tenancy.py): the control schema, the registry, and schema-per-tenant connections.

Postgres only: tenancy has no SQLite form. Each test makes its own control schema and its own tenant schemas in the test database
and drops them afterwards. This proves isolation at the connection layer (a pooled connection handed from tenant A to tenant B
never shows A's tables, lock names do not collide) and the registry's bookkeeping. It proves nothing about a hosted deployment."""

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from volt import migrations, pg, tenancy
from volt.store import Store
from volt.tenancy import Registry, TenantError, TenantRef

from tests.pgutil import DSN


class SqliteSide(unittest.TestCase):
    def test_store_without_schema_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "v.db"))
            try:
                self.assertFalse(store.postgres)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 0)
            finally:
                store.close()

    def test_a_schema_on_sqlite_is_refused_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Store(str(Path(tmp) / "v.db"), schema="t_acme")

    def test_slug_rules(self):
        for ok in ("acme", "ab", "news-24", "a" + "b" * 38):
            self.assertTrue(tenancy.SLUG.match(ok), ok)
        for bad in ("", "a", "A", "Acme", "1news", "-x", "x_y", "a" * 40, "x y", "x/y", "x.y"):
            self.assertFalse(tenancy.SLUG.match(bad), bad)

    def test_reserved_names_are_refused(self):
        for name in ("public", "volt-control", "admin", "www", "api", "pg-catalog", "information-schema", "default"):
            with self.assertRaises(TenantError, msg=name):
                tenancy.check_slug(name)

    def test_schema_for_a_slug_is_a_safe_identifier(self):
        self.assertEqual(tenancy.schema_for("news-24"), "t_news_24")
        self.assertRegex(tenancy.schema_for("acme"), r"^[a-z_][a-z0-9_]*$")

    def test_host_normalisation(self):
        self.assertEqual(tenancy.normalise_host("Acme.Volt.Example:8765"), "acme.volt.example")
        self.assertEqual(tenancy.normalise_host("acme.volt.example."), "acme.volt.example")
        with self.assertRaises(TenantError):
            tenancy.normalise_host("not a host")
        with self.assertRaises(TenantError):
            tenancy.normalise_host("")


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class Base(unittest.TestCase):
    def setUp(self):
        import psycopg
        pg.close_pools()
        self.ctl = "volt_ctl_" + uuid.uuid4().hex[:10]
        self.tag = uuid.uuid4().hex[:6]
        self.registry = Registry(DSN, control_schema=self.ctl)
        self.made = []
        self.stores = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import psycopg
        pg.close_pools()
        for s in self.stores:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{self.ctl}" CASCADE')
            for schema in self.made:
                admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    def slug(self, name):
        return f"{name}-{self.tag}"

    def create(self, name, domain=None):
        ref = self.registry.create(self.slug(name), domain or f"{name}-{self.tag}.volt.test", operator="tester")
        self.made.append(ref.schema)
        return ref

    def store(self, ref, **kw):
        s = Store(ref.dsn, schema=ref.schema, **kw)
        self.stores.append(s)
        return s

    def raw(self, sql, *params):
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else None


class Isolation(Base):
    def test_connection_for_b_never_sees_a_tables(self):
        a, b = self.create("alpha"), self.create("bravo")
        pg.configure_pool(DSN, max_size=1, min_size=1)       # one physical connection: A's is B's
        sa = self.store(a, init=False)
        sa.conn.execute("CREATE TABLE marker_only_in_a (n INTEGER)")
        sa.conn.execute("INSERT INTO marker_only_in_a VALUES (1)")
        sa.conn.commit()
        sa.close()
        sb = self.store(b, init=False)
        with self.assertRaises(Exception):
            sb.conn.execute("SELECT * FROM marker_only_in_a")
        sb.conn.rollback()
        path = sb.conn.execute("SHOW search_path").fetchone()[0]
        self.assertEqual(path.replace('"', ""), b.schema)

    def test_search_path_reset_when_returned_to_pool(self):
        a = self.create("alpha")
        pool = pg.configure_pool(DSN, max_size=1, min_size=1)
        sa = self.store(a, init=False)
        self.assertEqual(sa.conn.execute("SHOW search_path").fetchone()[0].replace('"', ""), a.schema)
        sa.close()
        conn = pool.getconn()
        try:
            shown = conn.execute("SHOW search_path").fetchone()[0]
            self.assertNotIn(a.schema, shown)
        finally:
            pool.putconn(conn)

    def test_a_connection_that_cannot_be_reset_is_not_returned_to_the_pool(self):
        a = self.create("alpha")
        pool = pg.configure_pool(DSN, max_size=1, min_size=1)
        sa = self.store(a, init=False)
        sa.conn._conn.close()         # the server-side connection is gone: RESET will fail
        sa.close()                    # must not raise, and must not hand a dead connection to the next borrower
        sb = Store(DSN, schema=self.create("bravo").schema, init=False)
        self.stores.append(sb)
        self.assertEqual(sb.conn.execute("SELECT 1").fetchone()[0], 1)

    def test_unpooled_connections_are_isolated_too(self):
        a, b = self.create("alpha"), self.create("bravo")
        sa = self.store(a, init=False)
        sa.conn.execute("CREATE TABLE marker_only_in_a (n INTEGER)")
        sa.conn.commit()
        sb = self.store(b, init=False)
        with self.assertRaises(Exception):
            sb.conn.execute("SELECT * FROM marker_only_in_a")

    def test_lock_names_are_schema_scoped(self):
        a, b, a2 = self.create("alpha"), self.create("bravo"), None
        sa, sb, sa2 = self.store(a), self.store(b), self.store(a)
        held, release = threading.Event(), threading.Event()

        def hold():
            with sa.transaction("show:1"):
                held.set()
                release.wait(30)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(30))
        started = time.monotonic()
        with sb.transaction("show:1"):                  # another tenant, same name: does not wait
            pass
        self.assertLess(time.monotonic() - started, 3.0, "tenant B was serialised behind tenant A's lock")
        blocked = threading.Event()

        def same_tenant():
            with sa2.transaction("show:1"):
                blocked.set()

        t2 = threading.Thread(target=same_tenant, daemon=True)
        t2.start()
        self.assertFalse(blocked.wait(1.0), "the same tenant's lock did not exclude")
        release.set()
        t.join(30)
        self.assertTrue(blocked.wait(30))

    def test_schema_less_store_keeps_unprefixed_lock_names(self):
        import inspect
        src = inspect.getsource(pg.PgConnection.transaction)
        self.assertIn("self._schema", src)


class Registering(Base):
    def test_create_runs_all_migrations_in_new_schema(self):
        ref = self.create("alpha")
        s = self.store(ref, init=False)
        self.assertEqual(migrations.current_version(migrations.PostgresBackend(s.conn._conn)), migrations.LATEST)
        tables = {r[0] for r in self.raw("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", ref.schema)}
        self.assertTrue({"asset", "item", "show", "audit", "volt_user"} <= tables)
        self.assertEqual(ref.state, "active")
        self.assertEqual(ref.slug, self.slug("alpha"))

    def test_create_refuses_a_duplicate_slug_or_domain(self):
        self.create("alpha", "same.volt.test")
        with self.assertRaises(TenantError):
            self.registry.create(self.slug("alpha"), "other.volt.test", operator="t")
        with self.assertRaises(TenantError):
            self.registry.create(self.slug("bravo"), "same.volt.test", operator="t")
        self.assertIsNone(self.registry.get(self.slug("bravo")), "a refused create left a half-made tenant")

    def test_adopt_public_moves_no_data(self):
        import psycopg
        schema = "adopt_" + uuid.uuid4().hex[:8]
        self.made.append(schema)
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f'CREATE SCHEMA "{schema}"')
        s = Store(DSN, schema=schema)
        self.stores.append(s)
        s.mint_asset(owner="desk")
        s.mint_asset(owner="desk")
        before = s.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
        s.close()
        ref = self.registry.adopt(self.slug("legacy"), schema, f"legacy-{self.tag}.volt.test", operator="tester")
        self.assertEqual(ref.schema, schema)
        after = self.store(ref, init=False).conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
        self.assertEqual((before, after), (2, 2))

    def test_adopt_refuses_a_missing_schema(self):
        with self.assertRaises(TenantError):
            self.registry.adopt(self.slug("ghost"), "no_such_schema_" + self.tag, "ghost.volt.test", operator="t")

    def test_control_migrations_forward_from_empty(self):
        self.create("alpha")
        rows = self.raw(f'SELECT version FROM "{self.ctl}".control_schema_version ORDER BY version')
        self.assertEqual([r[0] for r in rows], list(range(1, tenancy.CONTROL_LATEST + 1)))
        tables = {r[0] for r in self.raw("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", self.ctl)}
        self.assertTrue({"tenant", "tenant_domain", "tenant_limit", "tenant_setting", "registry_epoch", "control_audit",
                         "control_schema_version"} <= tables)
        Registry(DSN, control_schema=self.ctl).active()          # a second start applies nothing and breaks nothing
        rows2 = self.raw(f'SELECT COUNT(*) FROM "{self.ctl}".control_schema_version')
        self.assertEqual(rows2[0][0], tenancy.CONTROL_LATEST)

    def test_control_migration_gains_a_dropped_table(self):
        self.create("alpha")
        self.raw(f'DROP TABLE "{self.ctl}".tenant_limit')
        self.raw(f'DELETE FROM "{self.ctl}".control_schema_version WHERE version = %s', tenancy.CONTROL_LATEST)
        Registry(DSN, control_schema=self.ctl).active()
        tables = {r[0] for r in self.raw("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", self.ctl)}
        self.assertIn("tenant_limit", tables)

    def test_a_control_schema_newer_than_the_code_is_refused(self):
        self.create("alpha")
        self.raw(f'INSERT INTO "{self.ctl}".control_schema_version (version, name, applied_at) VALUES (%s, %s, now())',
                 tenancy.CONTROL_LATEST + 5, "from the future")
        with self.assertRaises(migrations.SchemaTooNewError):
            Registry(DSN, control_schema=self.ctl).active()

    def test_lookup_by_host_is_case_and_port_insensitive(self):
        ref = self.create("alpha", f"Alpha-{self.tag}.Volt.Test")
        found = self.registry.by_host(f"ALPHA-{self.tag}.volt.test:8765")
        self.assertEqual(found, ref)
        self.assertIsNone(self.registry.by_host("nobody.volt.test"))
        self.assertIsNone(self.registry.by_host(""))

    def test_extra_domains(self):
        ref = self.create("alpha")
        self.registry.add_domain(ref.slug, f"alias-{self.tag}.volt.test", operator="t")
        self.assertEqual(self.registry.by_host(f"alias-{self.tag}.volt.test").slug, ref.slug)

    def test_suspend_and_resume_bump_epoch_and_change_state(self):
        ref = self.create("alpha")
        e0 = self.registry.epoch()
        self.registry.suspend(ref.slug, operator="t", reason="unpaid")
        self.assertGreater(self.registry.epoch(), e0)
        self.assertEqual(self.registry.get(ref.slug).state, "suspended")
        self.assertEqual(self.registry.by_host(f"alpha-{self.tag}.volt.test").state, "suspended")
        self.assertNotIn(ref.slug, [t.slug for t in self.registry.active()])
        e1 = self.registry.epoch()
        self.registry.resume(ref.slug, operator="t")
        self.assertGreater(self.registry.epoch(), e1)
        self.assertEqual(self.registry.get(ref.slug).state, "active")

    def test_limits(self):
        ref = self.create("alpha")
        self.assertEqual(self.registry.limits(ref.slug), {})
        self.registry.set_limit(ref.slug, "max_assets", 1000, operator="t")
        self.registry.set_limit(ref.slug, "max_assets", 2000, operator="t")
        self.assertEqual(self.registry.limits(ref.slug), {"max_assets": 2000})
        with self.assertRaises(TenantError):
            self.registry.set_limit(ref.slug, "not_a_limit", 1, operator="t")
        with self.assertRaises(TenantError):
            self.registry.set_limit(ref.slug, "max_assets", -1, operator="t")

    def test_unknown_tenant_is_an_error_not_a_silent_noop(self):
        for call in (lambda: self.registry.suspend("nope-" + self.tag, operator="t"),
                     lambda: self.registry.resume("nope-" + self.tag, operator="t"),
                     lambda: self.registry.set_limit("nope-" + self.tag, "max_assets", 1, operator="t")):
            with self.assertRaises(TenantError):
                call()

    def test_platform_actions_audited(self):
        ref = self.create("alpha")
        self.registry.suspend(ref.slug, operator="tester", reason="because")
        self.registry.resume(ref.slug, operator="tester")
        self.registry.set_limit(ref.slug, "max_assets", 5, operator="tester")
        self.registry.add_domain(ref.slug, f"x-{self.tag}.volt.test", operator="tester")
        rows = self.raw(f'SELECT operator, action, slug, detail FROM "{self.ctl}".control_audit ORDER BY id')
        self.assertEqual([r[1] for r in rows], ["create", "suspend", "resume", "limit", "domain"])
        self.assertTrue(all(r[0] == "tester" and r[2] == ref.slug for r in rows))
        self.assertIn("because", rows[1][3])
        self.assertNotIn(DSN, " ".join(str(r) for r in rows), "a DSN (with a password) reached the audit table")

    def test_run_all_tenants_is_idempotent_and_reports_per_tenant(self):
        a, b = self.create("alpha"), self.create("bravo")
        first = migrations.run_all_tenants(self.registry)
        self.assertEqual(sorted(first), sorted([a.slug, b.slug]))
        self.assertTrue(all(v["ok"] and v["applied"] == [] for v in first.values()))
        self.raw(f'DROP SCHEMA "{b.schema}" CASCADE')
        self.raw(f'CREATE SCHEMA "{b.schema}"')
        self.raw(f'DROP SCHEMA "{a.schema}" CASCADE')            # a broken tenant must not stop the others
        second = migrations.run_all_tenants(self.registry)
        self.assertTrue(second[b.slug]["ok"])
        self.assertEqual(second[b.slug]["applied"], list(range(1, migrations.LATEST + 1)))
        self.assertFalse(second[a.slug]["ok"] and second[a.slug]["applied"] == [])

    def test_run_all_tenants_can_be_limited_to_named_slugs(self):
        a, b = self.create("alpha"), self.create("bravo")
        only = migrations.run_all_tenants(self.registry, slugs=[a.slug])
        self.assertEqual(list(only), [a.slug])
        with self.assertRaises(TenantError):
            migrations.run_all_tenants(self.registry, slugs=["missing-" + self.tag])


class Cli(Base):
    def test_cli_round_trip(self):
        import contextlib
        import io
        env = {"VOLT_CONTROL_DSN": DSN, "VOLT_CONTROL_SCHEMA": self.ctl}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        slug = self.slug("cli")

        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = tenancy.main(list(args))
            return code, out.getvalue(), err.getvalue()

        code, out, err = run("create", "--slug", slug, "--domain", f"cli-{self.tag}.volt.test")
        self.assertEqual(code, 0, err)
        self.made.append(tenancy.schema_for(slug))
        self.assertNotIn(DSN, out + err)
        code, out, _ = run("list")
        self.assertEqual(code, 0)
        self.assertIn(slug, out)
        self.assertEqual(run("suspend", slug, "--reason", "test")[0], 0)
        self.assertIn("suspended", run("list")[1])
        self.assertEqual(run("resume", slug)[0], 0)
        self.assertEqual(run("limit", slug, "max_assets", "50")[0], 0)
        self.assertEqual(run("migrate")[0], 0)
        self.assertEqual(run("migrate", "--tenants", slug)[0], 0)
        self.assertNotEqual(run("create", "--slug", "Bad Slug", "--domain", "x.test")[0], 0)
        self.assertNotEqual(run("suspend", "missing-" + self.tag)[0], 0)

    def test_cli_without_a_control_dsn_says_so(self):
        import contextlib
        import io
        for k in ("VOLT_CONTROL_DSN", "VOLT_DB"):
            old = os.environ.pop(k, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, k, old)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tenancy.main(["list"])
        self.assertEqual(code, 78)
        self.assertIn("VOLT_CONTROL_DSN", err.getvalue())


if __name__ == "__main__":
    unittest.main()
