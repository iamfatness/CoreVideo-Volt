"""Server-side sessions, the cookie, and the CSRF checks for cookie requests (identity plan, task 3).

Bearer tokens behave as before; a cookie request is a different kind of caller (the browser attaches it by itself), so it needs an
Origin that matches the configured public URL and the per-session X-Volt-Csrf value. Time is injected; nothing here sleeps.
"""
import hashlib
import io
import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_operator_api import Api
from tests.test_take import _show
from tests.pgutil import PgCase
from volt import access, migrations, serve, sessions, users
from volt.playout import hold_state
from volt.store import Store

T0 = 1_800_000_000.0
PUBLIC = "https://volt.example.test"


class Unit(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        users.add(self.store, "alice", "operator")

    def test_create_returns_a_cookie_and_csrf_and_stores_only_the_hash(self):
        value, csrf = sessions.create(self.store, "alice", "token", T0)
        self.assertGreaterEqual(len(value), 40)
        self.assertNotEqual(value, csrf)
        row = self.store.conn.execute("SELECT * FROM session").fetchone()
        self.assertEqual(row["id_hash"], hashlib.sha256(value.encode()).hexdigest())
        self.assertNotIn(value, [str(row[k]) for k in row.keys()])
        self.assertEqual((row["user_name"], row["provider"], row["csrf"]), ("alice", "token", csrf))

    def test_each_login_mints_a_new_id(self):
        seen = {sessions.create(self.store, "alice", "token", T0)[0] for _ in range(20)}
        self.assertEqual(len(seen), 20)

    def test_resolve_unknown_or_malformed_values_is_none(self):
        for bad in ("", "x", "a" * 5000, None):
            self.assertIsNone(sessions.resolve(self.store, bad, T0))

    def test_idle_and_absolute_expiry_on_frozen_clock(self):
        idle, absolute = sessions.idle_seconds({}), sessions.max_seconds({})
        self.assertEqual((idle, absolute), (8 * 3600, 14 * 3600))
        value, _ = sessions.create(self.store, "alice", "token", T0)
        self.assertIsNotNone(sessions.resolve(self.store, value, T0 + idle - 1))
        # Idle: nothing seen for the whole idle window (the check above touched last_seen at T0+idle-1, so start over).
        value, _ = sessions.create(self.store, "alice", "token", T0)
        self.assertIsNone(sessions.resolve(self.store, value, T0 + idle + 1))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session WHERE id_hash = ?",
                                                 (sessions.hash_cookie(value),)).fetchone()[0], 0)   # an expired row is removed
        # Activity keeps it alive past the idle window...
        value, _ = sessions.create(self.store, "alice", "token", T0)
        for hour in range(1, 14):
            self.assertIsNotNone(sessions.resolve(self.store, value, T0 + hour * 3600), hour)
        # ...but never past the absolute limit.
        self.assertIsNone(sessions.resolve(self.store, value, T0 + absolute + 1))

    def test_limits_come_from_the_environment(self):
        self.assertEqual(sessions.idle_seconds({"VOLT_SESSION_IDLE": "60"}), 60)
        self.assertEqual(sessions.max_seconds({"VOLT_SESSION_MAX": "120"}), 120)
        for junk in ("", "abc", "0", "-5", "1.5"):
            self.assertEqual(sessions.idle_seconds({"VOLT_SESSION_IDLE": junk}), 8 * 3600, junk)
        env = {"VOLT_SESSION_IDLE": "60", "VOLT_SESSION_MAX": "120"}
        value, _ = sessions.create(self.store, "alice", "token", T0, env=env)
        self.assertIsNone(sessions.resolve(self.store, value, T0 + 61, env=env))

    def test_last_seen_write_is_throttled(self):
        value, _ = sessions.create(self.store, "alice", "token", T0)
        writes = []
        self.store.conn.set_trace_callback(lambda sql: writes.append(sql) if sql.lstrip().upper().startswith("UPDATE SESSION") else None)
        for second in range(1, 61):
            self.assertIsNotNone(sessions.resolve(self.store, value, T0 + second))
        self.store.conn.set_trace_callback(None)
        self.assertEqual(len(writes), 1)
        info = sessions.resolve(self.store, value, T0 + 61)
        self.assertEqual(info.last_seen_at, T0 + 60)

    def test_end_and_end_for_user(self):
        a, _ = sessions.create(self.store, "alice", "token", T0)
        b, _ = sessions.create(self.store, "alice", "token", T0)
        users.add(self.store, "bob", "operator")
        c, _ = sessions.create(self.store, "bob", "token", T0)
        sessions.end(self.store, sessions.hash_cookie(a))
        self.assertIsNone(sessions.resolve(self.store, a, T0))
        self.assertEqual(sessions.end_for_user(self.store, "alice"), 1)
        self.assertIsNone(sessions.resolve(self.store, b, T0))
        self.assertIsNotNone(sessions.resolve(self.store, c, T0))

    def test_disabling_a_user_ends_their_sessions(self):
        users.add(self.store, "root", "admin")
        value, _ = sessions.create(self.store, "alice", "token", T0)
        users.disable(self.store, "alice")
        self.assertIsNone(sessions.resolve(self.store, value, T0))


class CookieText(unittest.TestCase):
    def test_cookie_attributes(self):
        https = sessions.cookie_header("abc", secure=True, host_prefix=True)
        self.assertTrue(https.startswith("__Host-volt_session=abc;"))
        for part in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(part, https.split("; "))
        self.assertNotIn("Domain", https)
        plain = sessions.cookie_header("abc", secure=False, host_prefix=False)
        self.assertTrue(plain.startswith("volt_session=abc;"))
        self.assertNotIn("Secure", plain)
        self.assertIn("HttpOnly", plain)
        self.assertNotIn("Domain", plain)
        # __Host- requires Secure, so a prefix without it is not produced.
        self.assertIn("Secure", sessions.cookie_header("abc", secure=False, host_prefix=True))

    def test_clear_header_matches_the_name_and_expires_at_once(self):
        for prefix in (True, False):
            text = sessions.clear_header(secure=prefix, host_prefix=prefix)
            self.assertTrue(text.startswith(sessions.cookie_name(prefix) + "=;"))
            self.assertIn("Max-Age=0", text)

    def test_reading_the_cookie_ignores_other_names_and_the_wrong_prefix(self):
        self.assertEqual(sessions.cookie_value("a=1; volt_session=xyz; b=2", host_prefix=False), "xyz")
        self.assertEqual(sessions.cookie_value("__Host-volt_session=xyz", host_prefix=True), "xyz")
        self.assertEqual(sessions.cookie_value("volt_session=xyz", host_prefix=True), "")   # https only trusts the __Host- name
        self.assertEqual(sessions.cookie_value("__Host-volt_session=xyz", host_prefix=False), "")
        self.assertEqual(sessions.cookie_value(None, host_prefix=False), "")
        self.assertEqual(sessions.cookie_value("volt_session=", host_prefix=False), "")

    def test_origins_are_normalised_for_comparison(self):
        self.assertEqual(sessions.origin_of("https://Volt.Example.test/"), "https://volt.example.test")
        self.assertEqual(sessions.origin_of("https://volt.example.test:443/x?y"), "https://volt.example.test")
        self.assertEqual(sessions.origin_of("http://localhost:8080"), "http://localhost:8080")
        for bad in ("", "null", "volt.example.test", "ftp://x", "javascript:alert(1)", None):
            self.assertIsNone(sessions.origin_of(bad), bad)


class Web(Api):
    """The server end to end, with the clock and the public URL under the test's control."""

    def setUp(self):
        super().setUp()
        self.now = T0
        saved = serve.CLOCK
        serve.CLOCK = lambda: self.now
        self.addCleanup(setattr, serve, "CLOCK", saved)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("VOLT_PUBLIC_URL", "VOLT_SESSION_IDLE", "VOLT_SESSION_MAX"):
            os.environ.pop(key, None)
        _, self.admin = users.add(self.store, "root", "admin")
        _, self.op = users.add(self.store, "alice", "operator")
        _, self.viewer = users.add(self.store, "vic", "viewer")
        self.show, _ = _show(self.store, [[]])

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.srv.server_port}"

    def start(self, token, headers=None):
        status, out, resp = self.call("POST", "/api/session/start", {}, token=token, headers=headers)
        return status, out, resp

    def login(self, token, headers=None):
        """A fresh session for the token's user: (cookie request headers, csrf value, raw cookie value, name)."""
        status, out, resp = self.start(token, headers)
        self.assertEqual(status, 200, out)
        set_cookie = resp.getheader("Set-Cookie")
        name, _, rest = set_cookie.partition("=")
        value = rest.split(";")[0]
        return {"Cookie": f"{name}={value}"}, out["csrf"], value, name

    def ck(self, method, path, auth, body=None, origin="default", csrf="default", extra=None, **kw):
        headers = dict(auth[0])
        if origin == "default":
            origin = self.origin
        if origin is not None:
            headers["Origin"] = origin
        if csrf == "default":
            csrf = auth[1]
        if csrf is not None:
            headers["X-Volt-Csrf"] = csrf
        headers.update(extra or {})
        return self.call(method, path, body, headers=headers, **kw)

    def hold(self, auth, **kw):
        return self.ck("POST", f"/api/shows/{self.show}/hold", auth, {"reason": "x"}, **kw)


class Login(Web):
    def test_cookie_attributes_over_http_localhost(self):
        _, out, resp = self.start(self.op)
        cookie = resp.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("volt_session="), cookie)
        for part in ("HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(part, cookie.split("; "))
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("Domain", cookie)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(set(out), {"ok", "csrf"})

    def test_cookie_attributes_under_an_https_public_url(self):
        os.environ["VOLT_PUBLIC_URL"] = PUBLIC
        _, out, resp = self.start(self.op)
        cookie = resp.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("__Host-volt_session="), cookie)
        for part in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(part, cookie.split("; "))
        self.assertNotIn("Domain", cookie)

    def test_new_session_id_each_login_and_client_id_ignored(self):
        first = self.login(self.op)
        second = self.login(self.op)
        self.assertNotEqual(first[2], second[2])
        planted = "attacker-chosen-value-0123456789abcdefghijklmnop"
        status, out, resp = self.start(self.op, headers={"Cookie": f"volt_session={planted}"})
        self.assertEqual(status, 200)
        issued = resp.getheader("Set-Cookie").split("=", 1)[1].split(";")[0]
        self.assertNotEqual(issued, planted)
        self.assertIsNone(sessions.resolve(self.store, planted, T0))
        status, out, _ = self.call("POST", "/api/session/start", {"sessionId": planted, "cookie": planted}, token=self.op)
        self.assertEqual((status, out["error"]["code"]), (422, "bad_input"))

    def test_cookie_value_stored_only_hashed_and_never_logged_or_audited(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old)
        auth = self.login(self.op)
        self.ck("GET", "/api/whoami", auth)
        self.hold(auth)
        value, csrf = auth[2], auth[1]
        dump = " ".join(" ".join(str(c) for c in tuple(r)) for t in ("session", "audit", "volt_user")
                        for r in self.store.conn.execute(f"SELECT * FROM {t}").fetchall())
        self.assertNotIn(value, dump)
        self.assertIn(hashlib.sha256(value.encode()).hexdigest(), dump)
        self.assertNotIn(value, stream.getvalue())
        self.assertNotIn(csrf, stream.getvalue())
        self.assertNotIn(self.op, stream.getvalue() + dump)   # nor the bearer token

    def test_start_needs_a_bearer_and_a_real_user(self):
        self.assertEqual(self.call("POST", "/api/session/start", {})[0], 401)
        auth = self.login(self.op)
        status, out, _ = self.ck("POST", "/api/session/start", auth, {})      # a cookie cannot mint another session
        self.assertEqual((status, out["error"]["code"]), (401, "bearer_required"))
        serve.TOKEN = "legacy-token-for-this-test"
        status, out, _ = self.call("POST", "/api/session/start", {}, token="legacy-token-for-this-test")
        self.assertEqual((status, out["error"]["code"]), (409, "not_a_user"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)

    def test_start_is_refused_without_a_public_url_off_loopback(self):
        status, out, _ = self.start(self.op, headers={"Host": "volt.example.test"})
        self.assertEqual((status, out["error"]["code"]), (409, "public_url_required"))
        os.environ["VOLT_PUBLIC_URL"] = PUBLIC
        status, _, _ = self.start(self.op, headers={"Host": "volt.example.test"})
        self.assertEqual(status, 200)

    def test_start_is_audited_as_login_without_secrets(self):
        auth = self.login(self.op)
        rows = self.store.list_audit("alice")
        self.assertTrue(any(r["action"] == "login" and r["target_kind"] == "session" and r["actor"] == "alice" for r in rows), rows)
        self.assertNotIn(auth[2], " ".join(str(dict(r)) for r in rows))


class CookieWrites(Web):
    def test_cookie_post_without_origin_is_refused(self):
        auth = self.login(self.op)
        status, out, _ = self.hold(auth, origin=None)
        self.assertEqual((status, out["error"]["code"]), (403, "bad_origin"))
        self.assertIsNone(hold_state(self.store, self.show))

    def test_cookie_post_with_foreign_origin_is_refused(self):
        auth = self.login(self.op)
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1", "http://localhost:" + str(self.srv.server_port)):
            status, out, _ = self.hold(auth, origin=origin)
            self.assertEqual((status, out["error"]["code"]), (403, "bad_origin"), origin)
        self.assertIsNone(hold_state(self.store, self.show))

    def test_origin_is_compared_with_the_public_url_not_the_host_header(self):
        os.environ["VOLT_PUBLIC_URL"] = PUBLIC + "/"
        auth = self.login(self.op)
        status, out, _ = self.hold(auth)     # Origin is the loopback one the test client would send
        self.assertEqual((status, out["error"]["code"]), (403, "bad_origin"))
        status, out, _ = self.hold(auth, origin=PUBLIC, extra={"Host": "evil.example"})
        self.assertEqual(status, 200, out)

    def test_cookie_post_without_csrf_header_is_refused(self):
        auth = self.login(self.op)
        for csrf in (None, "", "wrong", auth[1][:-1], auth[1] + "x"):
            status, out, _ = self.hold(auth, csrf=csrf)
            self.assertEqual((status, out["error"]["code"]), (403, "bad_csrf"), csrf)
        self.assertIsNone(hold_state(self.store, self.show))
        # another session's csrf value does not work either
        other = self.login(self.op)
        status, out, _ = self.hold(auth, csrf=other[1])
        self.assertEqual((status, out["error"]["code"]), (403, "bad_csrf"))

    def test_cookie_post_with_origin_and_csrf_succeeds_and_is_audited_as_the_user(self):
        auth = self.login(self.op)
        status, out, _ = self.hold(auth)
        self.assertEqual((status, out["ok"]), (200, True))
        self.assertIsNotNone(hold_state(self.store, self.show))
        self.assertTrue(any(r["actor"] == "alice" and r["action"] == "hold" for r in self.store.list_audit(self.show)))

    def test_bearer_post_needs_no_origin_or_csrf(self):
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.op)
        self.assertEqual(status, 200, out)

    def test_a_bearer_wins_over_a_cookie(self):
        auth = self.login(self.op)
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.viewer, headers=auth[0])
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))     # judged as vic, and needs no CSRF
        status, _, _ = self.call("GET", "/api/whoami", token="not-a-token", headers=auth[0])
        self.assertEqual(status, 401)    # a wrong bearer does not fall back to the cookie
        _, who, _ = self.call("GET", "/api/whoami", token=self.viewer, headers=auth[0])
        self.assertEqual((who["name"], who["via"]), ("vic", "token"))

    def test_json_content_type_still_required(self):
        auth = self.login(self.op)
        status, out, _ = self.hold(auth, ctype="text/plain", raw=b'{"reason":"x"}')
        self.assertEqual((status, out["error"]["code"]), (415, "unsupported_media_type"))
        self.assertIsNone(hold_state(self.store, self.show))

    def test_a_viewer_session_is_still_a_viewer(self):
        auth = self.login(self.viewer)
        status, out, _ = self.hold(auth)
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))
        self.assertEqual(self.ck("GET", "/api/state", auth)[0], 200)

    def test_cookie_reads_need_no_origin_or_csrf(self):
        auth = self.login(self.op)
        status, _, _ = self.call("GET", "/api/state", headers=auth[0])
        self.assertEqual(status, 200)

    def test_a_cookie_is_ignored_under_the_wrong_name_for_the_scheme(self):
        os.environ["VOLT_PUBLIC_URL"] = PUBLIC
        auth = self.login(self.op)
        self.assertEqual(self.call("GET", "/api/state", headers=auth[0])[0], 200)
        plain = {"Cookie": "volt_session=" + auth[2]}      # the plain name is not honoured under https
        self.assertEqual(self.call("GET", "/api/state", headers=plain)[0], 401)

    def test_no_cookie_and_no_token_is_still_401(self):
        self.assertEqual(self.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.call("GET", "/api/state", headers={"Cookie": "volt_session=nope"})[0], 401)


class Lifetime(Web):
    def test_idle_and_absolute_expiry_over_http(self):
        auth = self.login(self.op)
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 200)
        self.now += 8 * 3600 + 1
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)
        auth = self.login(self.op)
        for _ in range(13):                                  # active every hour: alive...
            self.now += 3600
            self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 200)
        self.now += 3600 + 1                                 # ...until the 14 hour limit
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)

    def test_disable_user_kills_session_next_request(self):
        auth = self.login(self.op)
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 200)
        status, _, _ = self.call("POST", "/api/users/alice/disable", {}, token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)
        self.assertEqual(self.hold(auth)[0], 401)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session WHERE user_name = 'alice'").fetchone()[0], 0)
        self.call("POST", "/api/users/alice/enable", {}, token=self.admin)
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)      # enabling again does not revive it

    def test_a_session_whose_user_row_vanished_is_refused_and_removed(self):
        auth = self.login(self.op)
        self.store.conn.execute("DELETE FROM volt_user WHERE name = 'alice'")
        self.store.conn.commit()
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 0)

    def test_role_change_applies_without_new_login(self):
        auth = self.login(self.op)
        self.assertEqual(self.hold(auth)[0], 200)
        self.call("POST", "/api/users/alice/role", {"role": "viewer"}, token=self.admin)
        status, out, _ = self.hold(auth)
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))
        self.call("POST", "/api/users/alice/role", {"role": "operator"}, token=self.admin)
        self.call("POST", "/api/groups", {"name": "leads", "role": "admin"}, token=self.admin)
        self.call("POST", "/api/groups/leads/members", {"add": ["alice"]}, token=self.admin)
        _, who, _ = self.ck("GET", "/api/whoami", auth)
        self.assertEqual((who["effectiveRole"], who["role"], who["groups"]), ("admin", "admin", ["leads"]))
        self.call("POST", "/api/groups/leads/members", {"remove": ["alice"]}, token=self.admin)
        _, who, _ = self.ck("GET", "/api/whoami", auth)
        self.assertEqual(who["effectiveRole"], "operator")

    def test_admin_can_end_a_users_sessions(self):
        auth = self.login(self.op)
        self.login(self.op)
        status, out, _ = self.call("POST", "/api/users/alice/end-sessions", {}, token=self.op)
        self.assertEqual(status, 403)
        status, out, _ = self.call("POST", "/api/users/alice/end-sessions", {}, token=self.admin)
        self.assertEqual((status, out["ended"]), (200, 2))
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)
        self.assertTrue(any(r["action"] == "session_end" for r in self.store.list_audit("alice")))
        self.refused(404, "no_such_user", "/api/users/zed/end-sessions", token=self.admin)

    def test_session_end_clears_cookie(self):
        auth = self.login(self.op)
        status, out, resp = self.ck("POST", "/api/session/end", auth, {})
        self.assertEqual((status, out["ok"]), (200, True))
        cleared = resp.getheader("Set-Cookie")
        self.assertTrue(cleared.startswith(auth[3] + "=;"), cleared)
        self.assertIn("Max-Age=0", cleared)
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 401)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 0)
        self.assertTrue(any(r["action"] == "logout" for r in self.store.list_audit("alice")))

    def test_session_end_needs_the_csrf_checks_like_any_cookie_post(self):
        auth = self.login(self.op)
        status, out, _ = self.ck("POST", "/api/session/end", auth, {}, csrf=None)
        self.assertEqual((status, out["error"]["code"]), (403, "bad_csrf"))
        self.assertEqual(self.ck("GET", "/api/whoami", auth)[0], 200)

    def test_a_viewer_can_end_their_own_session_and_a_bearer_end_is_harmless(self):
        auth = self.login(self.viewer)
        self.assertEqual(self.ck("POST", "/api/session/end", auth, {})[0], 200)
        status, out, _ = self.call("POST", "/api/session/end", {}, token=self.op)
        self.assertEqual((status, out["ok"], out["ended"]), (200, True, False))   # a bearer has no session to end


class Whoami(Web):
    def test_whoami_returns_csrf_only_for_session(self):
        auth = self.login(self.op)
        _, who, _ = self.ck("GET", "/api/whoami", auth)
        self.assertEqual(who["csrf"], auth[1])
        self.assertEqual((who["via"], who["name"], who["effectiveRole"], who["displayName"]), ("session", "alice", "operator", "alice"))
        _, who, _ = self.call("GET", "/api/whoami", token=self.op)
        self.assertNotIn("csrf", who)
        self.assertEqual((who["via"], who["effectiveRole"], who["groups"]), ("token", "operator", []))
        serve.TOKEN = "legacy-token-for-this-test"
        _, who, _ = self.call("GET", "/api/whoami", token="legacy-token-for-this-test")
        self.assertEqual((who["via"], who["legacy"], who["name"]), ("legacy", True, "desk"))
        self.assertNotIn("csrf", who)

    def test_display_name_is_used_when_set(self):
        self.store.conn.execute("UPDATE volt_user SET display_name = 'Alice A' WHERE name = 'alice'")
        self.store.conn.commit()
        _, who, _ = self.call("GET", "/api/whoami", token=self.op)
        self.assertEqual(who["displayName"], "Alice A")
        self.assertEqual(who["name"], "alice")


class OnAir(Web):
    def test_bearer_control_works_with_the_session_table_gone(self):
        self.store.conn.execute("DROP TABLE session")
        self.store.conn.commit()
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.op)
        self.assertEqual(status, 200, out)
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/release", {}, token=self.op)
        self.assertEqual(status, 200, out)
        self.assertEqual(self.call("GET", "/api/state", token=self.op)[0], 200)
        # and a cookie request fails closed rather than guessing
        self.assertIn(self.call("GET", "/api/state", headers={"Cookie": "volt_session=whatever"})[0], (401, 503))


class Registry(unittest.TestCase):
    def test_routes_are_classified_and_audited(self):
        names = {fn.__name__ for _, fn in serve.ROUTES}
        for fn in ("act_session_start", "act_session_end", "act_user_end_sessions"):
            self.assertIn(fn, names)
        self.assertEqual(serve.ROUTE_ROLES["act_session_start"], "viewer")
        self.assertEqual(serve.ROUTE_ROLES["act_session_end"], "viewer")
        self.assertEqual(serve.ROUTE_ROLES["act_user_end_sessions"], "admin")
        self.assertEqual(serve._AUDIT_KIND["session"], "session")


class Migration(unittest.TestCase):
    def test_a_database_at_the_previous_version_gains_the_session_table(self):
        self.assertGreaterEqual(migrations.LATEST, 26)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v23.db")
            Store(path).close()
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE session")
            conn.execute("DELETE FROM schema_version WHERE version >= 26")
            conn.commit()
            conn.close()
            store = Store(path)
            try:
                cols = {r[1] for r in store.conn.execute("PRAGMA table_info(session)")}
                self.assertEqual(cols, {"id_hash", "user_name", "provider", "csrf", "created_at", "last_seen_at", "expires_at"})
                users.add(store, "alice", "operator")
                sessions.create(store, "alice", "token", T0)
                self.assertEqual([r[0] for r in store.conn.execute("SELECT version FROM schema_version ORDER BY version")],
                                 list(range(1, migrations.LATEST + 1)))
            finally:
                store.close()


class MigrationPostgres(PgCase):
    def test_a_postgres_database_at_the_previous_version_gains_the_session_table(self):
        store = self.store()
        store.conn.execute("DROP TABLE session")
        store.conn.execute("DELETE FROM schema_version WHERE version >= 26")
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        users.add(again, "alice", "operator")
        value, csrf = sessions.create(again, "alice", "token", T0)
        info = sessions.resolve(again, value, T0 + 5)
        self.assertEqual((info.user_name, info.csrf), ("alice", csrf))
        self.assertIsNone(sessions.resolve(again, value, T0 + 15 * 3600))


if __name__ == "__main__":
    unittest.main()
