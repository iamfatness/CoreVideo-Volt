"""Per-user tokens: two roles (operator, admin), hashed at rest, revocable at once, actor from the token."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_operator_api import TOKEN, Api
from tests.test_playout import _show
from volt import migrations, serve, users
from volt.store import Store


def _token_line(out: str) -> str:
    return next(line.split()[-1] for line in out.splitlines() if line.startswith("token"))


class UserStore(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def test_add_returns_the_token_once_and_stores_only_its_hash(self):
        user, token = users.add(self.store, "alice", "operator")
        self.assertEqual((user["name"], user["role"]), ("alice", "operator"))
        self.assertIsNone(user["disabled_at"])
        self.assertNotIn("token", user)
        self.assertGreaterEqual(len(token), 32)
        rows = [tuple(r) for r in self.store.conn.execute("SELECT * FROM volt_user")]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(token, " ".join(str(c) for c in rows[0]))
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), rows[0])
        for listed in users.list_users(self.store):
            self.assertNotIn("token_hash", listed)

    def test_authenticate(self):
        _, token = users.add(self.store, "alice", "admin")
        got = users.authenticate(self.store, token)
        self.assertEqual((got.name, got.role), ("alice", "admin"))
        self.assertIsNone(users.authenticate(self.store, token + "x"))
        self.assertIsNone(users.authenticate(self.store, ""))
        self.assertIsNone(users.authenticate(self.store, "alice"))

    def test_disable_takes_effect_at_once_and_enable_restores(self):
        users.add(self.store, "root", "admin")
        _, token = users.add(self.store, "bob", "operator")
        users.disable(self.store, "bob")
        self.assertIsNone(users.authenticate(self.store, token))
        bob = next(u for u in users.list_users(self.store) if u["name"] == "bob")
        self.assertTrue(bob["disabled_at"])
        users.enable(self.store, "bob")
        self.assertIsNotNone(users.authenticate(self.store, token))

    def test_rotate_invalidates_the_old_token(self):
        _, old = users.add(self.store, "bob", "operator")
        new = users.rotate(self.store, "bob")
        self.assertNotEqual(old, new)
        self.assertIsNone(users.authenticate(self.store, old))
        self.assertEqual(users.authenticate(self.store, new).name, "bob")

    def test_rotate_refuses_a_disabled_user(self):
        users.add(self.store, "root", "admin")
        users.add(self.store, "bob", "operator")
        users.disable(self.store, "bob")
        with self.assertRaises(users.UserError) as cm:
            users.rotate(self.store, "bob")
        self.assertEqual(cm.exception.code, "user_disabled")

    def test_validation(self):
        for name in ("", "bad name", "a@b", "x" * 101, "desk"):
            with self.subTest(name=name), self.assertRaises(users.UserError) as cm:
                users.add(self.store, name, "operator")
            self.assertEqual(cm.exception.code, "bad_name")
        with self.assertRaises(users.UserError) as cm:
            users.add(self.store, "alice", "root")
        self.assertEqual(cm.exception.code, "bad_role")
        users.add(self.store, "alice", "operator")
        with self.assertRaises(users.UserError) as cm:
            users.add(self.store, "alice", "admin")
        self.assertEqual(cm.exception.code, "user_exists")
        with self.assertRaises(users.UserError) as cm:
            users.disable(self.store, "nobody")
        self.assertEqual(cm.exception.code, "no_such_user")

    def test_last_enabled_admin_cannot_be_disabled_without_a_legacy_token(self):
        users.add(self.store, "root", "admin")
        with self.assertRaises(users.UserError) as cm:
            users.disable(self.store, "root")
        self.assertEqual(cm.exception.code, "last_admin")
        users.disable(self.store, "root", legacy_admin=True)

    def test_has_enabled_admin(self):
        self.assertFalse(users.has_enabled_admin(self.store))
        users.add(self.store, "op", "operator")
        self.assertFalse(users.has_enabled_admin(self.store))
        users.add(self.store, "root", "admin")
        self.assertTrue(users.has_enabled_admin(self.store))


class Registry(unittest.TestCase):
    def test_migration_17_is_the_user_table(self):
        self.assertEqual(migrations.MIGRATIONS[16].name, "volt_user table")
        self.assertGreaterEqual(migrations.LATEST, 17)

    def test_every_post_route_has_an_explicit_role(self):
        names = {fn.__name__ for _p, fn in serve.ROUTES}
        self.assertEqual(names, set(serve.ROUTE_ROLES), "classify every new route in serve.ROUTE_ROLES")
        self.assertTrue(set(serve.ROUTE_ROLES.values()) <= {"operator", "admin"})

    def test_destructive_routes_are_admin(self):
        for name in ("act_delete", "act_show_delete", "act_detect", "act_unpublish", "act_user_add"):
            self.assertEqual(serve.ROUTE_ROLES[name], "admin", name)
        for name in ("act_go", "act_take", "act_hold", "act_skip", "act_record_start", "act_cleared", "act_publish"):
            self.assertEqual(serve.ROUTE_ROLES[name], "operator", name)


class CheckBind(unittest.TestCase):
    def test_users_only_needs_an_enabled_admin(self):
        with self.assertRaises(serve.ConfigError):
            serve.check_bind("0.0.0.0", None)
        with self.assertRaises(serve.ConfigError):
            serve.check_bind("0.0.0.0", None, has_admin_user=False)
        serve.check_bind("0.0.0.0", None, has_admin_user=True)

    def test_a_weak_legacy_token_is_still_refused(self):
        with self.assertRaises(serve.ConfigError):
            serve.check_bind("0.0.0.0", "short", has_admin_user=True)


class UsersApi(Api):
    def setUp(self):
        super().setUp()
        _, self.admin = users.add(self.store, "root", "admin")
        _, self.op = users.add(self.store, "alice", "operator")

    def test_users_only_requires_a_token_even_without_volt_token(self):
        show, _ = _show(self.store)
        self.refused(401, "unauthorized", f"/api/shows/{show}/go")
        self.refused(401, "unauthorized", f"/api/shows/{show}/go", token="nope")
        s, _ = self.post(f"/api/shows/{show}/go", token=self.op)
        self.assertEqual(s, 200)

    def test_audit_actor_is_the_user_not_the_header(self):
        show, _ = _show(self.store)
        s, _ = self.post(f"/api/shows/{show}/go", token=self.op, headers={"X-Volt-Actor": "mallory"})
        self.assertEqual(s, 200)
        self.assertEqual(self.store.list_audit(show)[0]["actor"], "alice")

    def test_even_an_invalid_header_is_ignored_for_a_user_token(self):
        show, _ = _show(self.store)
        s, _ = self.post(f"/api/shows/{show}/go", token=self.op, headers={"X-Volt-Actor": "bad actor"})
        self.assertEqual(s, 200)

    def test_legacy_token_stays_admin_and_honours_the_header(self):
        serve.TOKEN = TOKEN
        show, _ = _show(self.store)
        s, _ = self.post(f"/api/shows/{show}/go", token=TOKEN, headers={"X-Volt-Actor": "header.user"})
        self.assertEqual(s, 200)
        self.assertEqual(self.store.list_audit(show)[0]["actor"], "header.user")
        s, p = self.post("/api/users", {"name": "carol", "role": "operator"}, token=TOKEN)
        self.assertEqual(s, 200, p)
        s, _ = self.post(f"/api/shows/{show}/go", token=self.op)  # user tokens work alongside the legacy one
        self.assertEqual(s, 200)

    def test_operator_is_refused_admin_routes_with_403(self):
        p = self.refused(403, "forbidden_role", "/api/assets/a1/delete", token=self.op)
        self.assertIn("admin", p["error"]["reason"])
        self.refused(403, "forbidden_role", "/api/shows/x/delete", token=self.op)
        self.refused(403, "forbidden_role", "/api/users", {"name": "eve", "role": "admin"}, token=self.op)
        s, _, _ = self.call("GET", "/api/users", token=self.op)
        self.assertEqual(s, 403)
        self.assertEqual({u["name"] for u in users.list_users(self.store)}, {"root", "alice"})

    def test_admin_passes_the_role_check(self):
        self.refused(404, "not_found", "/api/shows/nope/delete", token=self.admin)

    def test_unknown_route_stays_404_for_an_operator(self):
        self.refused(404, "not_found", "/api/nothing/here", token=self.op)

    def test_disable_is_immediate(self):
        show, _ = _show(self.store)
        s, _ = self.post(f"/api/shows/{show}/hold", {"reason": "x"}, token=self.op)
        self.assertEqual(s, 200)
        s, p = self.post("/api/users/alice/disable", token=self.admin)
        self.assertEqual(s, 200, p)
        self.refused(401, "unauthorized", f"/api/shows/{show}/release", token=self.op)

    def test_rotate_over_http_returns_new_token_once(self):
        s, p = self.post("/api/users/alice/rotate", token=self.admin)
        self.assertEqual(s, 200, p)
        new = p["token"]
        self.refused(401, "unauthorized", "/api/shows/x/go", token=self.op)
        self.refused(404, "not_found", "/api/shows/x/go", token=new)
        s, listing, _ = self.call("GET", "/api/users", token=self.admin)
        self.assertNotIn(new, json.dumps(listing))

    def test_create_user_shows_token_once_and_never_in_list_or_audit(self):
        s, p = self.post("/api/users", {"name": "dave", "role": "operator"}, token=self.admin)
        self.assertEqual(s, 200, p)
        token = p["token"]
        self.assertEqual((p["user"]["name"], p["user"]["role"]), ("dave", "operator"))
        s, listing, _ = self.call("GET", "/api/users", token=self.admin)
        self.assertEqual(s, 200)
        self.assertNotIn(token, json.dumps(listing))
        self.assertNotIn("token_hash", json.dumps(listing))
        self.assertEqual({u["name"] for u in listing["users"]}, {"root", "alice", "dave"})
        rows = self.store.list_audit("dave")
        self.assertEqual((rows[0]["actor"], rows[0]["action"], rows[0]["target_kind"]), ("root", "user_add", "user"))
        self.assertNotIn(token, json.dumps(rows))
        self.refused(404, "not_found", "/api/shows/x/go", token=token)

    def test_bad_user_input(self):
        self.refused(422, "bad_name", "/api/users", {"name": "no good", "role": "operator"}, token=self.admin)
        self.refused(422, "bad_role", "/api/users", {"name": "ok", "role": "god"}, token=self.admin)
        self.refused(409, "user_exists", "/api/users", {"name": "alice", "role": "operator"}, token=self.admin)
        self.refused(404, "no_such_user", "/api/users/zed/disable", token=self.admin)
        self.refused(409, "last_admin", "/api/users/root/disable", token=self.admin)

    def test_whoami(self):
        s, p, _ = self.call("GET", "/api/whoami", token=self.op)
        self.assertEqual((s, p["name"], p["role"]), (200, "alice", "operator"))
        serve.TOKEN = TOKEN
        s, p, _ = self.call("GET", "/api/whoami", token=TOKEN)
        self.assertEqual((p["name"], p["role"]), ("desk", "admin"))

    def test_reads_work_for_operator(self):
        s, _, _ = self.call("GET", "/api/state", token=self.op)
        self.assertEqual(s, 200)
        s, _, _ = self.call("GET", "/api/state")
        self.assertEqual(s, 401)

    def test_media_ticket_issue_works_for_a_user_with_a_media_key(self):
        saved = serve.MEDIA_KEY
        serve.MEDIA_KEY = "k" * 32
        try:
            s, p, _ = self.call("GET", "/api/media-ticket?asset=a1&kind=proxy", token=self.op)
            self.assertEqual(s, 200, p)
            s, _, _ = self.call("GET", p["url"])  # the ticket alone, no bearer
            self.assertNotEqual(s, 401)
        finally:
            serve.MEDIA_KEY = saved

    def test_no_users_and_no_token_stays_open_on_loopback(self):
        self.store.conn.execute("DELETE FROM volt_user")
        self.store.conn.commit()
        show, _ = _show(self.store)
        s, _ = self.post(f"/api/shows/{show}/go")
        self.assertEqual(s, 200)


class UserCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "volt.db")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = users.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def auth(self, token):
        store = Store(self.db)
        try:
            return users.authenticate(store, token)
        finally:
            store.close()

    def test_add_list_rotate_disable(self):
        code, out, _ = self.run_cli("add", "root", "--role", "admin")
        self.assertEqual(code, 0)
        token = _token_line(out)
        self.assertEqual(self.auth(token).name, "root")

        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("root", out)
        self.assertIn("admin", out)
        self.assertNotIn(token, out)

        code, out, _ = self.run_cli("rotate", "root")
        self.assertEqual(code, 0)
        new = _token_line(out)
        self.assertNotEqual(new, token)
        self.assertIsNone(self.auth(token))

        code, _, err = self.run_cli("disable", "root")
        self.assertEqual(code, 1)  # last admin
        self.assertIn("last_admin", err)
        self.assertNotIn(new, err)

    def test_errors_exit_nonzero(self):
        code, _, err = self.run_cli("add", "bad name", "--role", "operator")
        self.assertEqual(code, 1)
        self.assertIn("bad_name", err)
        code, _, err = self.run_cli("disable", "ghost")
        self.assertEqual(code, 1)

    def test_list_json_has_no_secrets(self):
        _, out, _ = self.run_cli("add", "root", "--role", "admin")
        token = _token_line(out)
        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual(json.loads(out)[0]["name"], "root")
        self.assertNotIn(token, out)
        self.assertNotIn("token_hash", out)


if __name__ == "__main__":
    unittest.main()
