"""Viewer role, groups and the effective role (identity plan, task 1). Bearer tokens behave as before; a group can only raise a role."""
import unittest

from tests.test_operator_api import Api
from tests.test_take import _show
from volt import access, migrations, serve, users
from volt.store import Store


class Effective(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        users.add(self.store, "root", "admin")
        users.add(self.store, "alice", "operator")
        users.add(self.store, "vic", "viewer")

    def test_rank_order(self):
        self.assertLess(access.RANK["viewer"], access.RANK["operator"])
        self.assertLess(access.RANK["operator"], access.RANK["admin"])

    def test_the_role_is_the_highest_of_direct_and_group(self):
        access.create_group(self.store, "leads", role="admin")
        access.set_members(self.store, "leads", add=["alice"])
        self.assertEqual(access.effective_role(self.store, "alice", "operator"), "admin")
        self.assertEqual(access.groups_of(self.store, "alice"), ("leads",))
        access.set_members(self.store, "leads", remove=["alice"])
        self.assertEqual(access.effective_role(self.store, "alice", "operator"), "operator")   # at once, no restart

    def test_a_group_never_lowers_a_role(self):
        access.create_group(self.store, "watchers", role="viewer")
        access.set_members(self.store, "watchers", add=["alice"])
        self.assertEqual(access.effective_role(self.store, "alice", "operator"), "operator")

    def test_a_group_without_a_role_grants_nothing(self):
        access.create_group(self.store, "crew")
        access.set_members(self.store, "crew", add=["vic"])
        self.assertEqual(access.effective_role(self.store, "vic", "viewer"), "viewer")

    def test_validation(self):
        for bad in ("", "a b", "x" * 101, "../x"):
            with self.assertRaises(access.AccessError):
                access.create_group(self.store, bad)
        with self.assertRaises(access.AccessError):
            access.create_group(self.store, "g", role="root")
        access.create_group(self.store, "g")
        with self.assertRaises(access.AccessError) as ctx:
            access.create_group(self.store, "g")
        self.assertEqual(ctx.exception.code, "group_exists")
        with self.assertRaises(access.AccessError) as ctx:
            access.set_members(self.store, "g", add=["nobody"])
        self.assertEqual(ctx.exception.code, "no_such_user")
        with self.assertRaises(access.AccessError) as ctx:
            access.set_members(self.store, "missing", add=["alice"])
        self.assertEqual(ctx.exception.code, "no_such_group")

    def test_deleting_a_group_removes_its_authority_and_members(self):
        access.create_group(self.store, "leads", role="admin")
        access.set_members(self.store, "leads", add=["alice"])
        access.delete_group(self.store, "leads")
        self.assertEqual(access.effective_role(self.store, "alice", "operator"), "operator")
        self.assertEqual(access.groups_of(self.store, "alice"), ())

    def test_the_last_admin_cannot_be_demoted(self):
        with self.assertRaises(users.UserError) as ctx:
            access.set_user_role(self.store, "root", "operator")
        self.assertEqual(ctx.exception.code, "last_admin")
        access.set_user_role(self.store, "root", "operator", legacy_admin=True)   # a legacy token still administers
        self.assertEqual(self.store.conn.execute("SELECT role FROM volt_user WHERE name = 'root'").fetchone()["role"], "operator")
        access.set_user_role(self.store, "alice", "admin")
        with self.assertRaises(users.UserError):                   # alice is now the only admin
            access.set_user_role(self.store, "alice", "viewer")

    def test_a_disabled_user_has_no_groups_effect_and_cannot_authenticate(self):
        _, token = users.add(self.store, "carol", "operator")
        access.create_group(self.store, "leads", role="admin")
        access.set_members(self.store, "leads", add=["carol"])
        self.assertEqual(users.authenticate(self.store, token).role, "admin")
        users.disable(self.store, "carol", legacy_admin=True)
        self.assertIsNone(users.authenticate(self.store, token))


class Registry(unittest.TestCase):
    def test_every_route_role_is_ranked_and_unclassified_stays_admin(self):
        self.assertTrue(set(serve.ROUTE_ROLES.values()) <= set(access.RANK))
        for name in ("act_group_create", "act_group_members", "act_group_delete", "act_user_role"):
            self.assertEqual(serve.ROUTE_ROLES[name], "admin", name)

    def test_the_roles_tuple_has_viewer(self):
        self.assertEqual(users.ROLES, ("viewer", "operator", "admin"))

    def test_migration_adds_the_columns_and_tables(self):
        self.assertGreaterEqual(migrations.LATEST, 22)
        store = Store()
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(volt_user)")}
        self.assertTrue({"email", "display_name", "source"} <= cols)
        for table in ("volt_group", "group_member"):
            self.assertIsNotNone(store.conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (table,)).fetchone())


class AccessApi(Api):
    def setUp(self):
        super().setUp()
        _, self.admin = users.add(self.store, "root", "admin")
        _, self.op = users.add(self.store, "alice", "operator")
        _, self.viewer = users.add(self.store, "vic", "viewer")
        self.show, _ = _show(self.store, [[]])

    def test_a_viewer_reads_but_cannot_write_an_operator_route(self):
        status, _, _ = self.call("GET", "/api/state", token=self.viewer)
        self.assertEqual(status, 200)
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.viewer)
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))
        status, _, _ = self.call("POST", f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.op)
        self.assertEqual(status, 200)

    def test_whoami_reports_the_effective_role_and_groups(self):
        self.call("POST", "/api/groups", {"name": "leads", "role": "admin"}, token=self.admin)
        self.call("POST", "/api/groups/leads/members", {"add": ["alice"]}, token=self.admin)
        _, out, _ = self.call("GET", "/api/whoami", token=self.op)
        self.assertEqual((out["role"], out["groups"]), ("admin", ["leads"]))
        self.call("POST", "/api/groups/leads/members", {"remove": ["alice"]}, token=self.admin)
        _, out, _ = self.call("GET", "/api/whoami", token=self.op)
        self.assertEqual((out["role"], out["groups"]), ("operator", []))

    def test_group_routes_are_admin_only_and_audited(self):
        status, out, _ = self.call("POST", "/api/groups", {"name": "leads"}, token=self.op)
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))
        status, out, _ = self.call("POST", "/api/groups", {"name": "leads", "role": "operator"}, token=self.admin)
        self.assertEqual((status, out["group"]["name"]), (200, "leads"))
        status, out, _ = self.call("GET", "/api/groups", token=self.admin)
        self.assertEqual([g["name"] for g in out["groups"]], ["leads"])
        status, out, _ = self.call("GET", "/api/groups", token=self.op)
        self.assertEqual(status, 403)
        actions = [r["action"] for r in self.store.list_audit("leads")]
        self.assertIn("group_create", actions)

    def test_set_a_users_role_and_the_last_admin_rule(self):
        status, out, _ = self.call("POST", "/api/users/alice/role", {"role": "viewer"}, token=self.admin)
        self.assertEqual((status, out["user"]["role"]), (200, "viewer"))
        status, out, _ = self.call("POST", "/api/users/root/role", {"role": "operator"}, token=self.admin)
        self.assertEqual((status, out["error"]["code"]), (409, "last_admin"))
        status, out, _ = self.call("POST", "/api/users/alice/role", {"role": "boss"}, token=self.admin)
        self.assertEqual((status, out["error"]["code"]), (422, "bad_role"))

    def test_bad_group_bodies(self):
        for body in ({}, {"name": "a b"}, {"name": "g", "role": "boss"}, {"name": "g", "extra": 1}):
            status, out, _ = self.call("POST", "/api/groups", body, token=self.admin)
            self.assertEqual(status, 422, body)
        status, out, _ = self.call("POST", "/api/groups/nope/members", {"add": ["alice"]}, token=self.admin)
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()


class Cli(unittest.TestCase):
    def test_role_and_group_commands(self):
        import contextlib
        import io
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "v.db")
            store = Store(db)
            users.add(store, "root", "admin")
            users.add(store, "alice", "operator")
            store.close()

            def run(*args):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = users.main(["--db", db, *args])
                return code, out.getvalue(), err.getvalue()
            self.assertEqual(run("role", "alice", "--role", "viewer")[0], 0)
            self.assertEqual(run("group-add", "leads", "--role", "admin")[0], 0)
            code, out, _ = run("group-member", "leads", "--add", "alice")
            self.assertEqual((code, "alice" in out), (0, True))
            code, _, err = run("group-member", "leads", "--add", "nobody")
            self.assertEqual((code, "no_such_user" in err), (1, True))
            code, _, err = run("role", "root", "--role", "operator")
            self.assertEqual((code, "last_admin" in err), (1, True))
