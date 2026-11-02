"""The OIDC sign-in flow and the mapping from a verified token to a Volt user (identity plan, task 5).

UNVALIDATED against a real provider: every provider here is tests/oidc_fake.py behind the HTTP seam (`oidc.HTTP`), so nothing touches the
network and the claims are only what Google and Microsoft are recalled to send. Time is a frozen clock passed in.
"""
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.oidc_fake import CLIENT_ID, FakeProvider, b64u
from tests.pgutil import PgCase
from tests.test_operator_api import Api
from volt import access, migrations, oidc, serve, sessions, users
from volt.store import Store

T0 = 1_800_000_000
PUBLIC = "https://volt.example.test"
GOOGLE_ISS = "https://idp.test/google"
MS_ISS = "https://idp.test/ms/tenant-a/v2.0"
SECRET = "s3cret-VALUE-9f2c1"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + (self.formatter.formatException(record.exc_info) if record.exc_info and self.formatter else ""))


@contextlib.contextmanager
def volt_logs():
    """Capture everything the `volt` loggers emit, whatever earlier tests did to logging (volt.logs.setup swaps handlers and turns
    propagation off; dictConfig can disable loggers). The loggers are put back exactly as found."""
    names = ["volt", "volt.oidc", "volt.sessions", "volt.serve"]
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg.handlers[:], lg.level, lg.propagate, lg.disabled) for lg in loggers]
    prior_disable = logging.root.manager.disable
    handler = _Capture()
    try:
        logging.disable(logging.NOTSET)
        for lg in loggers:
            lg.handlers, lg.disabled = [], False
            lg.setLevel(logging.DEBUG)
            lg.propagate = lg.name != "volt"
        loggers[0].handlers = [handler]
        loggers[0].propagate = False
        yield handler.lines
    finally:
        for lg, (hs, lvl, prop, dis) in zip(loggers, saved):
            lg.handlers, lg.propagate, lg.disabled = hs, prop, dis
            lg.setLevel(lvl)
        logging.disable(prior_disable)


class Idp(FakeProvider):
    """A fake provider whose next token can carry any claims a test sets."""

    def __init__(self, **kw):
        self.sub, self.claims, self.drop = "sub-1", {}, ()
        super().__init__(**kw)

    def id_token(self, nonce, **kw):
        return super().id_token(nonce, sub=self.sub, drop=self.drop, **self.claims, **kw)


class Router:
    """The HTTP seam: sends each call to the fake that owns the URL, and can pretend the provider is down."""

    def __init__(self, *fakes):
        self.fakes, self.down, self.jwks_down, self.token_error = fakes, False, False, None
        self.posts, self.gets = [], []

    def _fake(self, url):
        for f in self.fakes:
            if url.startswith(f.issuer + "/"):
                return f
        raise KeyError(url)

    def get_json(self, url):
        self.gets.append(url)
        if self.down or (self.jwks_down and url.endswith("/jwks")):
            raise OSError("provider unreachable")
        return self._fake(url).get_json(url)

    def post_form(self, url, data):
        self.posts.append((url, dict(data)))
        if self.down:
            raise OSError("provider unreachable")
        if self.token_error is not None:
            return self.token_error
        return self._fake(url).post_form(url, data)


def write_config(directory, google=None, microsoft=None):
    g = {"id": "google", "label": "Google", "issuer": GOOGLE_ISS, "client_id": CLIENT_ID,
         "client_secret_env": "VOLT_TEST_GOOGLE_SECRET", "domains": ["example.test"]}
    m = {"id": "microsoft", "label": "Microsoft", "issuer": MS_ISS, "client_id": CLIENT_ID,
         "client_secret_env": "VOLT_TEST_MS_SECRET", "tenants": ["tenant-a"]}
    g.update(google or {})
    m.update(microsoft or {})
    path = Path(directory) / "oidc.json"
    path.write_text(json.dumps({"providers": [g, m]}), encoding="utf-8")
    return str(path)


ENV = {"VOLT_PUBLIC_URL": PUBLIC, "VOLT_TEST_GOOGLE_SECRET": SECRET, "VOLT_TEST_MS_SECRET": SECRET + "-ms"}


def make_idps(clock):
    google = Idp(issuer=GOOGLE_ISS, now=clock)
    google.claims = {"hd": "example.test", "email": "dana@example.test"}
    ms = Idp(issuer=MS_ISS, now=clock)
    ms.claims = {"tid": "tenant-a"}
    ms.drop = ("email_verified",)        # Microsoft sends no email_verified (recalled; unvalidated)
    return google, ms


class Flow(unittest.TestCase):
    """Library-level tests: begin and complete against a store, a frozen clock and the fake providers."""

    def setUp(self):
        prior = logging.root.manager.disable          # another test file may have switched logging off for the whole process
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, prior)
        oidc.reset_caches()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = T0
        self.google, self.ms = make_idps(lambda: self.t)
        self.http = Router(self.google, self.ms)
        self.cfg = oidc.load_config(write_config(self.tmp.name), ENV)
        self.store = Store()
        self.addCleanup(self.store.close)

    # -- helpers ------------------------------------------------------
    def start(self, provider="google", next_="/"):
        url, cookie = oidc.begin(self.store, self.cfg, provider, next_, self.t, self.http)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        return url, cookie, query

    def fake_for(self, provider):
        return self.google if provider == "google" else self.ms

    def sign_in(self, provider="google", next_="/", **idp):
        fake = self.fake_for(provider)
        for k, v in idp.items():
            setattr(fake, k, v)
        _, cookie, q = self.start(provider, next_)
        code = fake.authorize(q)
        return oidc.complete(self.store, self.cfg, {"code": code, "state": q["state"]}, cookie, self.t, self.http)

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(oidc.LoginError) as got:
            fn(*args, **kw)
        self.assertEqual(got.exception.code, code)
        return got.exception

    def audit(self, action):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT actor, action, target_kind, target_id, detail FROM audit WHERE action = ? ORDER BY at, id", (action,)).fetchall()]

    def count(self, table):
        return self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def user(self, name="dana", role="operator", email="dana@example.test", **kw):
        users.add(self.store, name, role)
        access.set_user_email(self.store, name, email)
        return name

    # -- configuration -------------------------------------------------
    def test_no_config_path_means_oidc_is_off(self):
        self.assertIsNone(oidc.load_config(None, ENV))
        self.assertIsNone(oidc.load_config("", ENV))

    def test_config_reads_the_secret_from_the_environment_and_never_shows_it(self):
        self.assertEqual([p.id for p in self.cfg.providers], ["google", "microsoft"])
        self.assertEqual(self.cfg.public_url, PUBLIC)
        self.assertNotIn(SECRET, repr(self.cfg))
        self.assertNotIn(SECRET, repr(self.cfg.providers[0]))
        self.assertNotIn(SECRET, str(self.cfg.providers[0].__dict__.get("client_secret_env")))
        self.assertEqual(self.cfg.provider("google").scopes, ("openid", "email", "profile"))
        self.assertEqual(self.cfg.provider("nope"), None)

    def test_bad_config_is_loud(self):
        for label, env, google, ms in (
                ("no public url", {**ENV, "VOLT_PUBLIC_URL": ""}, None, None),
                ("plain http off loopback", {**ENV, "VOLT_PUBLIC_URL": "http://volt.example.test"}, None, None),
                ("secret not in the environment", {k: v for k, v in ENV.items() if k != "VOLT_TEST_GOOGLE_SECRET"}, None, None),
                ("microsoft common issuer", ENV, None, {"issuer": "https://login.microsoftonline.com/common/v2.0"}),
                ("microsoft organizations issuer", ENV, None, {"issuer": "https://login.microsoftonline.com/organizations/v2.0"}),
                ("microsoft without a tenant list", ENV, None, {"tenants": []}),
                ("google auto provision without domains", ENV, {"auto_provision": True, "domains": []}, None),
                ("unknown provider id", ENV, {"id": "okta"}, None),
                ("issuer not https", ENV, {"issuer": "http://idp.test/google"}, None),
                ("secret inline in the file", ENV, {"client_secret": "x"}, None)):
            with self.subTest(label):
                with self.assertRaises(oidc.ConfigError):
                    oidc.load_config(write_config(self.tmp.name, google, ms), env)
        with self.assertRaises(oidc.ConfigError):
            oidc.load_config(str(Path(self.tmp.name) / "absent.json"), ENV)

    def test_plain_http_is_allowed_on_loopback(self):
        cfg = oidc.load_config(write_config(self.tmp.name), {**ENV, "VOLT_PUBLIC_URL": "http://localhost:8765"})
        self.assertEqual(cfg.public_url, "http://localhost:8765")

    # -- begin ---------------------------------------------------------
    def test_login_redirect_has_pkce_state_nonce_and_config_redirect_uri(self):
        url, cookie, q = self.start("google", "/operator")
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", GOOGLE_ISS + "/authorize")
        self.assertEqual((q["response_type"], q["client_id"], q["code_challenge_method"]), ("code", CLIENT_ID, "S256"))
        self.assertEqual(q["redirect_uri"], PUBLIC + "/auth/callback")
        self.assertEqual(q["scope"], "openid email profile")
        self.assertNotIn("offline_access", q["scope"])
        row = self.store.conn.execute("SELECT * FROM auth_flow WHERE state = ?", (q["state"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(q["code_challenge"], b64u(hashlib.sha256(row["code_verifier"].encode()).digest()))
        self.assertEqual(q["nonce"], row["nonce"])
        self.assertEqual(len({q["state"], q["nonce"], row["code_verifier"]}), 3)
        for v in (q["state"], q["nonce"], row["code_verifier"]):
            self.assertGreaterEqual(len(v), 43)
        self.assertEqual(row["next"], "/operator")
        self.assertEqual(row["provider"], "google")
        self.assertNotIn("operator", url.replace("redirect_uri", ""))      # next never rides in the URL or state
        self.assertEqual(row["binder_hash"], hashlib.sha256(cookie.encode()).hexdigest())
        self.assertNotEqual(row["binder_hash"], cookie)                       # only the hash is stored

    def test_every_flow_gets_fresh_randomness(self):
        seen = [self.start()[2] for _ in range(5)]
        self.assertEqual(len({q["state"] for q in seen}), 5)
        self.assertEqual(len({q["nonce"] for q in seen}), 5)

    def test_unknown_provider_is_refused_and_creates_no_flow(self):
        self.refused("bad_provider", oidc.begin, self.store, self.cfg, "okta", "/", self.t, self.http)
        self.refused("bad_provider", oidc.begin, self.store, self.cfg, None, "/", self.t, self.http)
        self.assertEqual(self.count("auth_flow"), 0)

    def test_discovery_is_cached_and_endpoints_must_be_https(self):
        self.start()
        self.start()
        self.assertEqual(len([g for g in self.http.gets if g.endswith("openid-configuration")]), 1)
        self.t += 24 * 3600 + 1
        self.start()
        self.assertEqual(len([g for g in self.http.gets if g.endswith("openid-configuration")]), 2)
        oidc.reset_caches()
        bad = self.google.discovery()
        bad["token_endpoint"] = "http://idp.test/token"
        with mock.patch.object(self.google, "discovery", return_value=bad):
            self.refused("oidc_unavailable", oidc.begin, self.store, self.cfg, "google", "/", self.t, self.http)

    def test_discovery_issuer_must_match_exactly(self):
        bad = self.google.discovery()
        bad["issuer"] = GOOGLE_ISS + "/"
        with mock.patch.object(self.google, "discovery", return_value=bad):
            self.refused("oidc_unavailable", oidc.begin, self.store, self.cfg, "google", "/", self.t, self.http)

    def test_provider_down_at_begin_is_unavailable_and_not_hammered(self):
        self.http.down = True
        self.refused("oidc_unavailable", oidc.begin, self.store, self.cfg, "google", "/", self.t, self.http)
        before = len(self.http.gets)
        for _ in range(20):
            self.refused("oidc_unavailable", oidc.begin, self.store, self.cfg, "google", "/", self.t + 1, self.http)
        self.assertEqual(len(self.http.gets), before)            # a short back-off: unauthenticated requests cannot drive outbound calls
        self.http.down = False
        self.t += 60
        self.start()

    def test_flow_table_is_capped_and_pruned(self):
        with mock.patch.object(oidc, "MAX_FLOWS", 5):
            for _ in range(5):
                self.start()
            self.refused("oidc_unavailable", oidc.begin, self.store, self.cfg, "google", "/", self.t, self.http)
            self.assertEqual(self.count("auth_flow"), 5)
            self.t += oidc.FLOW_TTL + 1                              # all five are now expired: pruned, and a new one fits
            self.start()
            self.assertEqual(self.count("auth_flow"), 1)

    def test_open_redirect_inputs_become_root(self):
        for bad in ("//evil.com", "/\\evil", "https://evil", "javascript:x", "/a\r\nSet-Cookie: x=1", "/" + "a" * 600, "",
                    "evil.com", "/a\tb", "/\x00", "/é", "///evil", "/\\/evil.com", None, 7, ["/x"], "http://evil/"):
            with self.subTest(bad=bad):
                self.assertEqual(oidc.safe_next(bad), "/")
                _, _, q = self.start("google", bad)
                row = self.store.conn.execute("SELECT next FROM auth_flow WHERE state = ?", (q["state"],)).fetchone()
                self.assertEqual(row["next"], "/")
        for good in ("/", "/operator", "/operator?show=abc&x=1", "/operator#library", "/a/b/c", "/" + "a" * 511):
            self.assertEqual(oidc.safe_next(good), good, good)
        self.assertEqual(oidc.safe_next("/" + "a" * 512), "/")

    # -- complete: the happy path and the refusals ---------------------
    def test_happy_path_creates_session_and_audits_login(self):
        self.user()
        res = self.sign_in("google", "/operator?show=1")
        self.assertEqual((res.user_name, res.next, res.provider), ("dana", "/operator?show=1", "google"))
        info = sessions.resolve(self.store, res.session_cookie, self.t)
        self.assertEqual((info.user_name, info.provider, info.csrf), ("dana", "oidc:google", res.csrf))
        self.assertEqual(self.count("auth_flow"), 0)                           # single use
        rows = self.audit("login")
        self.assertEqual([(r["actor"], r["target_kind"], r["target_id"]) for r in rows], [("dana", "session", "dana")])
        self.assertEqual(self.audit("login_failed"), [])
        self.assertNotIn(res.session_cookie, json.dumps(rows))

    def test_the_code_exchange_carries_the_verifier_and_config_redirect_uri(self):
        self.user()
        self.sign_in()
        url, data = self.http.posts[-1]
        self.assertEqual(url, GOOGLE_ISS + "/token")
        self.assertEqual((data["grant_type"], data["client_id"], data["client_secret"]), ("authorization_code", CLIENT_ID, SECRET))
        self.assertEqual(data["redirect_uri"], PUBLIC + "/auth/callback")
        self.assertTrue(data["code_verifier"])

    def test_callback_replay_refused(self):
        self.user()
        _, cookie, q = self.start()
        code = self.google.authorize(q)
        query = {"code": code, "state": q["state"]}
        oidc.complete(self.store, self.cfg, query, cookie, self.t, self.http)
        self.assertEqual(self.count("session"), 1)
        self.refused("flow_invalid", oidc.complete, self.store, self.cfg, query, cookie, self.t, self.http)
        self.assertEqual(self.count("session"), 1)
        self.assertEqual(len(self.audit("login_failed")), 1)
        self.assertEqual(len([p for p in self.http.posts]), 1)                  # the replay never reached the token endpoint

    def test_callback_from_other_browser_refused(self):
        self.user()
        for label, cookie in (("missing", None), ("empty", ""), ("wrong", "x" * 43), ("another flow's", "other")):
            with self.subTest(label):
                _, real, q = self.start()
                code = self.google.authorize(q)
                self.refused("flow_invalid", oidc.complete, self.store, self.cfg, {"code": code, "state": q["state"]},
                             cookie if cookie != "other" else self.start()[1], self.t, self.http)
                # the attempt used the flow up: even the right browser cannot finish it now
                self.refused("flow_invalid", oidc.complete, self.store, self.cfg, {"code": code, "state": q["state"]}, real, self.t, self.http)
        self.assertEqual(self.count("session"), 0)
        self.assertEqual(self.http.posts, [])

    def test_expired_flow_refused(self):
        self.user()
        _, cookie, q = self.start()
        code = self.google.authorize(q)
        self.refused("flow_invalid", oidc.complete, self.store, self.cfg, {"code": code, "state": q["state"]}, cookie,
                     self.t + oidc.FLOW_TTL + 1, self.http)
        self.assertEqual(self.count("session"), 0)
        self.assertEqual(len(self.audit("login_failed")), 1)

    def test_malformed_callbacks_are_refused(self):
        self.user()
        _, cookie, q = self.start()
        for query in ({}, {"code": "c"}, {"state": q["state"]}, {"code": "c", "state": ""}, {"code": "c", "state": "nope"},
                      {"code": "c", "state": ["a", "b"]}, {"code": ["c"], "state": q["state"]}, {"code": "c" * 5000, "state": q["state"]},
                      {"code": "c", "state": "s" * 5000}, {"code": 7, "state": q["state"]}, "state=x", None):
            with self.subTest(query=str(query)[:40]):
                with self.assertRaises(oidc.LoginError):
                    oidc.complete(self.store, self.cfg, query, cookie, self.t, self.http)
        self.assertEqual(self.count("session"), 0)
        self.assertEqual(self.http.posts, [])

    def test_provider_error_param_not_reflected(self):
        _, cookie, q = self.start()
        evil = "<script>alert(1)</script> access_denied"
        err = self.refused("provider_error", oidc.complete, self.store, self.cfg,
                           {"error": evil, "error_description": evil, "state": q["state"]}, cookie, self.t, self.http)
        self.assertNotIn("script", str(err) + err.code + err.public_message)
        for row in self.audit("login_failed"):
            self.assertNotIn("script", json.dumps(row))
        self.assertEqual(self.count("auth_flow"), 0)                             # the flow is consumed even on a provider error

    def test_every_fake_misbehaviour_refuses_and_audits_login_failed(self):
        cases = (
            ("bad_nonce", {"bad_nonce": True}, "oidc_token_invalid"),
            ("bad_aud", {"bad_aud": True}, "oidc_token_invalid"),
            ("bad_iss", {"bad_iss": True}, "oidc_token_invalid"),
            ("expired", {"expired": True}, "oidc_token_invalid"),
            ("old_iat", {"old_iat": True}, "oidc_token_invalid"),
            ("alg none", {"alg": "none"}, "oidc_alg_unsupported"),
            ("alg HS256 with the public key", {"alg": "HS256"}, "oidc_alg_unsupported"),
            ("alg ES256", {"alg": "ES256"}, "oidc_alg_unsupported"),
            ("unknown kid", {"kid_override": "not-in-the-jwks"}, "oidc_token_invalid"),
            ("truncated signature", {"truncate_sig": True}, "oidc_token_invalid"),
            ("e=3 forgery", {"forge_e3": True}, "oidc_token_invalid"),
            ("1024-bit key", {"small_key": True}, "oidc_token_invalid"),
        )
        for label, switches, code in cases:
            with self.subTest(label):
                oidc.reset_caches()
                self.store.close()
                self.store = Store()
                self.user()
                for k in ("bad_nonce", "bad_aud", "bad_iss", "expired", "old_iat", "truncate_sig", "forge_e3", "small_key"):
                    setattr(self.google, k, False)
                self.google.alg, self.google.kid_override = None, None
                self.refused(code, self.sign_in, "google", "/", **switches)
                self.assertEqual(self.count("session"), 0)
                self.assertEqual(self.count("identity"), 0)
                rows = self.audit("login_failed")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["actor"], "oidc:google")
                self.assertTrue(rows[0]["detail"].startswith(code), rows[0])
                self.assertEqual(self.audit("login"), [])

    def test_a_token_missing_required_claims_is_refused(self):
        self.user()
        for drop in (("sub",), ("exp",), ("iat",), ("nonce",), ("aud",), ("iss",)):
            with self.subTest(drop=drop):
                self.google.drop = drop
                self.refused("oidc_token_invalid", self.sign_in, "google")
        self.assertEqual(self.count("session"), 0)

    def test_token_endpoint_failures_refuse_without_leaking(self):
        self.user()
        _, cookie, q = self.start()
        code = self.google.authorize(q)
        for label, reply, expect in (
                ("provider error with the secret in its text", {"error": "invalid_client", "error_description": "bad secret " + SECRET}, "oidc_token_invalid"),
                ("no id_token", {"access_token": "a"}, "oidc_token_invalid"),
                ("id_token not text", {"id_token": 7}, "oidc_token_invalid"),
                ("not an object", ["x"], "oidc_token_invalid")):
            with self.subTest(label):
                self.http.token_error = reply
                _, cookie, q = self.start()
                self.refused(expect, oidc.complete, self.store, self.cfg, {"code": "c", "state": q["state"]}, cookie, self.t, self.http)
        self.http.token_error = None
        self.assertNotIn(SECRET, json.dumps(self.audit("login_failed")))
        self.assertEqual(self.count("session"), 0)

    def test_provider_unreachable_at_the_token_endpoint_is_unavailable(self):
        self.user()
        _, cookie, q = self.start()
        self.http.down = True
        self.refused("oidc_unavailable", oidc.complete, self.store, self.cfg, {"code": "c", "state": q["state"]}, cookie, self.t, self.http)
        self.assertEqual(self.count("session"), 0)
        self.assertEqual(len(self.audit("login_failed")), 1)

    def test_rotated_keys_are_picked_up(self):
        self.user()
        self.sign_in()
        self.t += 10
        self.google.rotate_keys = True
        res = self.sign_in()
        self.assertEqual(res.user_name, "dana")

    def test_provider_down_cached_keys_still_verify_unknown_kid_refused(self):
        self.user()
        self.sign_in()
        self.t += 400                                           # past the 5 minute floor: the cached keys are stale
        self.http.jwks_down = True
        self.assertEqual(self.sign_in().user_name, "dana")      # stale-while-error: a known kid still verifies
        self.refused("oidc_unavailable", self.sign_in, "google", "/", kid_override="rotated-away")
        self.assertEqual(len(self.audit("login_failed")), 1)
        self.t += 25 * 3600                                     # past the 24 hour stale window
        self.google.kid_override = None
        self.refused("oidc_unavailable", self.sign_in, "google")

    def test_login_failed_rows_are_rate_limited_with_a_count(self):
        _, cookie, q = self.start()
        for _ in range(30):
            with self.assertRaises(oidc.LoginError):
                oidc.complete(self.store, self.cfg, {"code": "c", "state": "nope"}, cookie, self.t, self.http)
        rows = self.audit("login_failed")
        self.assertLessEqual(len(rows), 3)                       # one per second (the loop may straddle a second boundary)
        total = sum(int(r["detail"].rsplit(" x", 1)[1]) if " x" in r["detail"] else 1 for r in rows)
        self.assertEqual(total, 30)

    # -- mapping claims to users --------------------------------------
    def test_unknown_identity_links_by_verified_email_only(self):
        self.user("dana", email="dana@example.test")
        self.google.sub = "g-1"
        self.google.claims.update(email="dana@example.test", email_verified=False)
        self.refused("not_provisioned", self.sign_in, "google")
        self.assertEqual(self.count("identity"), 0)
        self.google.claims.update(email_verified=True)
        self.assertEqual(self.sign_in("google").user_name, "dana")
        row = self.store.conn.execute("SELECT * FROM identity").fetchone()
        self.assertEqual((row["issuer"], row["sub"], row["user_name"], row["email"]), (GOOGLE_ISS, "g-1", "dana", "dana@example.test"))
        # outside the allowed domain, or with no hd at all (a consumer account), the same email is not trusted
        self.store.conn.execute("DELETE FROM identity")
        self.store.conn.commit()
        self.google.claims["hd"] = "other.test"
        self.refused("not_provisioned", self.sign_in, "google")
        del self.google.claims["hd"]
        self.refused("not_provisioned", self.sign_in, "google")
        self.assertEqual(self.count("identity"), 0)

    def test_microsoft_links_only_inside_an_allowed_tenant(self):
        self.user("sam", email="sam@example.test")
        self.ms.sub = "m-1"
        self.ms.claims.update(email="sam@example.test", tid="tenant-evil")
        self.refused("not_provisioned", self.sign_in, "microsoft")
        self.ms.claims["tid"] = "tenant-a"
        self.assertEqual(self.sign_in("microsoft").user_name, "sam")
        self.assertEqual(self.store.conn.execute("SELECT issuer FROM identity").fetchone()["issuer"], MS_ISS)
        # a present email_verified=false is still honoured
        self.store.conn.execute("DELETE FROM identity")
        self.store.conn.commit()
        self.ms.claims["email_verified"] = False
        self.ms.drop = ()
        self.refused("not_provisioned", self.sign_in, "microsoft")

    def test_microsoft_token_from_another_tenant_is_refused_even_for_a_known_identity(self):
        self.user("sam", email="sam@example.test")
        self.ms.sub, self.ms.claims["email"] = "m-1", "sam@example.test"
        self.sign_in("microsoft")
        self.ms.claims["tid"] = "tenant-evil"
        self.refused("not_provisioned", self.sign_in, "microsoft")

    def test_nOAuth_style_changed_email_does_not_relink(self):
        alice = self.user("alice", email="alice@example.test")
        bob = self.user("bob", email="bob@example.test")
        self.google.sub, self.google.claims["email"] = "g-alice", "alice@example.test"
        self.assertEqual(self.sign_in().user_name, alice)
        # the same (iss, sub) now claims bob's address: still alice, and bob's account is untouched
        self.google.claims["email"] = "bob@example.test"
        self.assertEqual(self.sign_in().user_name, alice)
        self.assertEqual(self.count("identity"), 1)
        self.assertEqual(self.store.conn.execute("SELECT email FROM identity").fetchone()["email"], "bob@example.test")
        self.assertEqual(self.store.conn.execute("SELECT email FROM volt_user WHERE name = 'bob'").fetchone()["email"], "bob@example.test")
        self.assertEqual(self.store.conn.execute("SELECT email FROM volt_user WHERE name = 'alice'").fetchone()["email"], "alice@example.test")
        # and a different sub with an address that already belongs to a linked user is not linked to it
        self.google.sub = "g-other"
        self.google.claims["email"] = "alice@example.test"
        self.refused("not_provisioned", self.sign_in)
        self.assertEqual(self.count("identity"), 1)

    def test_email_matching_ignores_case_and_surrounding_space(self):
        self.user("dana", email="Dana@Example.test")
        self.google.claims["email"] = "dana@EXAMPLE.test"
        self.assertEqual(self.sign_in().user_name, "dana")

    def test_two_users_with_one_address_link_nobody(self):
        users.add(self.store, "a", "operator")
        users.add(self.store, "b", "operator")
        self.store.conn.execute("UPDATE volt_user SET email = 'x@example.test'")
        self.store.conn.commit()
        self.google.claims["email"] = "x@example.test"
        self.refused("not_provisioned", self.sign_in)

    def test_no_user_refused_unless_auto_provision(self):
        self.google.claims["email"] = "newbie@example.test"
        self.refused("not_provisioned", self.sign_in)
        self.assertEqual(self.count("volt_user"), 0)
        self.cfg = oidc.load_config(write_config(self.tmp.name, {"auto_provision": True}), ENV)
        res = self.sign_in()
        self.assertEqual(res.user_name, "newbie")
        row = self.store.conn.execute("SELECT * FROM volt_user WHERE name = 'newbie'").fetchone()
        self.assertEqual((row["role"], row["source"], row["email"]), ("viewer", "oidc", "newbie@example.test"))
        self.assertEqual(access.effective_role(self.store, "newbie", row["role"]), "viewer")

    def test_auto_provision_still_needs_a_trusted_email(self):
        self.cfg = oidc.load_config(write_config(self.tmp.name, {"auto_provision": True}), ENV)
        self.google.claims.update(email="newbie@example.test", email_verified=False)
        self.refused("not_provisioned", self.sign_in)
        self.google.claims.update(email_verified=True, hd="other.test")
        self.refused("not_provisioned", self.sign_in)
        self.google.claims["hd"] = "example.test"
        self.google.drop = ("email",)
        self.refused("not_provisioned", self.sign_in)
        self.assertEqual(self.count("volt_user"), 0)

    def test_auto_provision_name_is_sanitised_and_not_desk(self):
        self.cfg = oidc.load_config(write_config(self.tmp.name, {"auto_provision": True}), ENV)
        got = []
        for sub, email in (("s1", "Dana.Smith+news@example.test"), ("s2", "Desk@example.test"), ("s3", "desk+x@example.test"),
                           ("s4", "dén@example.test"), ("s5", "dana.smith_news@example.test"), ("s6", ("x" * 150) + "@example.test")):
            self.google.sub, self.google.claims["email"] = sub, email
            got.append(self.sign_in().user_name)
        self.assertEqual(len(set(got)), 6)                       # duplicates get a numeric suffix
        for name in got:
            self.assertTrue(users.NAME.fullmatch(name), name)
            self.assertNotEqual(name, "desk")
        self.assertEqual(got[0], "Dana.Smith_news")
        self.assertEqual(got[4], "dana.smith_news-2")            # names are unique without regard to case
        self.assertTrue(all(r["role"] == "viewer" for r in self.store.conn.execute("SELECT role FROM volt_user").fetchall()))

    def test_provisioned_user_cannot_use_a_token(self):
        self.cfg = oidc.load_config(write_config(self.tmp.name, {"auto_provision": True}), ENV)
        self.google.claims["email"] = "newbie@example.test"
        self.sign_in()
        th = self.store.conn.execute("SELECT token_hash FROM volt_user WHERE name = 'newbie'").fetchone()["token_hash"]
        self.assertGreaterEqual(len(th), 64)
        for guess in ("", "newbie", th, "vtk_" + th):
            self.assertIsNone(users.authenticate(self.store, guess))

    def test_display_name_is_cleaned(self):
        self.user("dana")
        self.google.claims["name"] = "Dana\x00 \x1b[31mSmith\r\n" + "x" * 300
        self.google.claims["email"] = "dana@example.test"
        self.sign_in()
        shown = self.store.conn.execute("SELECT display_name FROM volt_user WHERE name = 'dana'").fetchone()["display_name"]
        self.assertTrue(shown.startswith("Dana "))
        self.assertLessEqual(len(shown), 100)
        self.assertFalse(re.search(r"[\x00-\x1f\x7f]", shown))

    def test_disabled_user_refused(self):
        users.add(self.store, "root", "admin")
        self.user("dana")
        self.google.claims["email"] = "dana@example.test"
        self.sign_in()                                           # linked while enabled
        users.disable(self.store, "dana")
        self.refused("user_disabled", self.sign_in)              # known identity
        self.assertEqual(self.count("session"), 0)
        self.store.conn.execute("DELETE FROM identity")
        self.store.conn.commit()
        self.refused("user_disabled", self.sign_in)              # and by email link
        self.assertEqual(self.count("identity"), 0)
        self.assertIn(len(self.audit("login_failed")), (1, 2))   # (a second failure in the same second is counted, not a new row)

    def test_a_stale_identity_for_a_deleted_user_is_refused(self):
        self.user("dana")
        self.google.claims["email"] = "dana@example.test"
        self.sign_in()
        self.store.conn.execute("DELETE FROM volt_user WHERE name = 'dana'")
        self.store.conn.commit()
        self.refused("not_provisioned", self.sign_in)

    def test_the_subject_must_be_sane(self):
        self.user()
        for sub in ("", " ", "s" * 300):
            self.google.sub = sub
            self.refused("oidc_token_invalid", self.sign_in)

    # -- groups --------------------------------------------------------
    def make_groups(self):
        for g in ("editors", "producers", "manual-only", "admins"):
            access.create_group(self.store, g, "admin" if g == "admins" else None)
        access.set_claim_map(self.store, "editors", "microsoft", "oid-editors")
        access.set_claim_map(self.store, "producers", "microsoft", "oid-producers")

    def ms_login(self, groups, **claims):
        self.ms.sub, self.ms.claims["email"] = "m-1", "sam@example.test"
        if groups is None:
            self.ms.claims.pop("groups", None)
        else:
            self.ms.claims["groups"] = groups
        self.ms.claims.update(claims)
        return self.sign_in("microsoft")

    def members(self, group):
        return sorted((r["user_name"], r["source"]) for r in self.store.conn.execute(
            "SELECT user_name, source FROM group_member WHERE group_name = ?", (group,)).fetchall())

    def test_oidc_groups_replace_only_oidc_sourced_membership(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        access.set_members(self.store, "manual-only", add=["sam"])
        access.set_members(self.store, "editors", add=["sam"])   # sam is a manual editor already
        self.ms_login(["oid-editors", "oid-producers", "oid-unmapped"])
        self.assertEqual(self.members("editors"), [("sam", "manual")])       # a manual row is neither overwritten nor taken over
        self.assertEqual(self.members("producers"), [("sam", "oidc")])
        self.assertEqual(self.members("manual-only"), [("sam", "manual")])
        self.ms_login(["oid-editors"])                                       # producers claim gone: removed
        self.assertEqual(self.members("producers"), [])
        self.assertEqual(self.members("editors"), [("sam", "manual")])       # the manual membership survives the claim going away
        self.assertEqual(self.members("manual-only"), [("sam", "manual")])
        self.ms_login(None)                                                  # no groups claim at all means no groups
        self.assertEqual(self.members("producers"), [])

    def test_unmapped_claim_values_grant_nothing_and_admin_needs_an_admin_mapping(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        self.ms_login(["admins", "admin", "oid-admins", "Administrators"])
        self.assertEqual(access.groups_of(self.store, "sam"), ())
        self.assertEqual(access.effective_role(self.store, "sam", "operator"), "operator")
        access.set_claim_map(self.store, "admins", "microsoft", "oid-admins")
        self.ms_login(["oid-admins"])
        self.assertEqual(access.effective_role(self.store, "sam", "operator"), "admin")   # only because an admin mapped it so
        self.ms_login([])                                                    # removed at the provider: gone at the next login
        self.assertEqual(access.effective_role(self.store, "sam", "operator"), "operator")

    def test_a_claim_map_is_per_provider(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        self.google.sub, self.google.claims.update(email="sam@example.test", groups=["oid-editors"])
        self.sign_in("google")
        self.assertEqual(access.groups_of(self.store, "sam"), ())

    def test_a_mapped_group_that_was_deleted_is_skipped(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        self.store.conn.execute("DELETE FROM volt_group WHERE name = 'editors'")
        self.store.conn.commit()
        self.ms_login(["oid-editors", "oid-producers"])
        self.assertEqual(access.groups_of(self.store, "sam"), ("producers",))

    def test_malformed_group_claims_change_nothing(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        self.ms_login(["oid-producers"])
        for bad in ("oid-editors", {"a": 1}, 7, [1, 2], [["x"]], ["oid-editors"] * 600):
            self.ms_login(bad)
            self.assertEqual(access.groups_of(self.store, "sam"), ("producers",), str(bad)[:30])

    def test_microsoft_group_overage_leaves_groups_and_warns(self):
        self.user("sam", email="sam@example.test")
        self.make_groups()
        self.ms_login(["oid-producers"])
        with volt_logs() as logs:
            res = self.ms_login(None, _claim_names={"groups": "src1"}, _claim_sources={"src1": {"endpoint": "https://graph.example/x"}})
        self.assertEqual(access.groups_of(self.store, "sam"), ("producers",))      # left as they were
        self.assertTrue(res.groups_overage)
        self.assertIn("too large", " ".join(logs))
        self.assertNotIn("graph.example", " ".join(logs))                   # the endpoint is never followed or logged
        res = self.ms_login(["oid-producers"])
        self.assertFalse(res.groups_overage)

    # -- secrets ---------------------------------------------------------
    def test_client_secret_never_in_log_or_audit_or_any_table(self):
        self.user()
        self.google.claims["email"] = "dana@example.test"
        with volt_logs() as logs:
            logging.getLogger("volt.oidc").debug("start")
            self.sign_in()
            self.http.token_error = {"error": "invalid_client", "error_description": "wrong secret " + SECRET}
            _, cookie, q = self.start()
            with self.assertRaises(oidc.LoginError):
                oidc.complete(self.store, self.cfg, {"code": "c", "state": q["state"]}, cookie, self.t, self.http)
            self.http.token_error = None
            self.http.down = True
            _, cookie, q = self.start()
            with self.assertRaises(oidc.LoginError):
                oidc.complete(self.store, self.cfg, {"code": "c", "state": q["state"]}, cookie, self.t, self.http)
        self.assertIn("start", logs[0])                                                   # the capture works (not a vacuous pass)
        self.assertTrue(any("token endpoint refused" in l for l in logs), logs)             # and saw the flow's own log lines
        self.assertTrue(any("unreachable" in l for l in logs), logs)
        self.assertNotIn(SECRET, "\n".join(logs))
        self.assertIn(SECRET, json.dumps(self.http.posts))                            # it was sent to the provider...
        dump = []
        for (name,) in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            dump.extend(str(tuple(r)) for r in self.store.conn.execute(f"SELECT * FROM {name}").fetchall())
        self.assertNotIn(SECRET, "\n".join(dump))                                     # ...and is in no table
        for secretish in ("code-1", cookie):
            self.assertNotIn(secretish, json.dumps(self.audit("login_failed")))

    def test_audit_rows_hold_no_state_code_or_cookie(self):
        self.user()
        res = self.sign_in()
        _, cookie, q = self.start()
        with self.assertRaises(oidc.LoginError):
            oidc.complete(self.store, self.cfg, {"code": "THE-CODE", "state": q["state"]}, "wrong", self.t, self.http)
        blob = json.dumps([self.audit("login"), self.audit("login_failed")])
        for secret in (q["state"], q["nonce"], cookie, "THE-CODE", res.session_cookie, res.csrf):
            self.assertNotIn(secret, blob)


class Default(unittest.TestCase):
    def test_the_default_client_has_ten_second_timeouts_and_follows_no_redirects(self):
        self.assertEqual(oidc.TIMEOUT, 10)
        self.assertIsInstance(oidc.HTTP, oidc.UrllibHttp)
        self.assertIsNone(oidc.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/"))

    def test_the_default_client_only_speaks_https(self):
        for url in ("http://idp.test/x", "file:///etc/passwd", "ftp://idp.test/x", "//idp.test/x", ""):
            with self.assertRaises(oidc.HttpError):
                oidc.UrllibHttp().get_json(url)
            with self.assertRaises(oidc.HttpError):
                oidc.UrllibHttp().post_form(url, {"a": "b"})


class Http(Api):
    """The routes: GET /auth/providers, /auth/login, /auth/callback, and the two admin POSTs."""

    def setUp(self):
        super().setUp()
        oidc.reset_caches()
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        self.google, self.ms = make_idps(None)
        self.google.now = self.ms.now = __import__("time").time
        self.router = Router(self.google, self.ms)
        self.cfg = oidc.load_config(write_config(self.tmp2.name), ENV)
        saved = (serve.OIDC_CONFIG, oidc.HTTP)
        serve.OIDC_CONFIG, oidc.HTTP = self.cfg, self.router
        self.addCleanup(lambda: setattr(serve, "OIDC_CONFIG", saved[0]) or setattr(oidc, "HTTP", saved[1]))
        env = mock.patch.dict(os.environ, {"VOLT_PUBLIC_URL": PUBLIC})
        env.start()
        self.addCleanup(env.stop)
        _, self.admin_token = users.add(self.store, "root", "admin")
        _, self.op_token = users.add(self.store, "op", "operator")
        users.add(self.store, "dana", "operator")
        access.set_user_email(self.store, "dana", "dana@example.test")

    def set_cookies(self, resp):
        return resp.msg.get_all("Set-Cookie") or []

    def begin(self, provider="google", next_="/operator", **kw):
        from urllib.parse import quote
        s, _, resp = self.call("GET", f"/auth/login?provider={provider}&next={quote(next_, safe='')}", **kw)
        return s, resp

    def flow(self, provider="google", next_="/operator"):
        s, resp = self.begin(provider, next_)
        self.assertEqual(s, 302)
        q = {k: v[0] for k, v in parse_qs(urlsplit(resp.getheader("Location")).query).items()}
        cookie = self.set_cookies(resp)[0].split(";")[0]
        return resp, q, cookie

    def test_oidc_off_gives_404(self):
        serve.OIDC_CONFIG = None
        for path in ("/auth/providers", "/auth/login?provider=google", "/auth/callback?code=a&state=b", "/auth/anything"):
            s, p, resp = self.call("GET", path)
            self.assertEqual(s, 404, path)
            self.assertEqual(resp.getheader("Set-Cookie"), None)
        s, _, _ = self.call("GET", "/api/whoami", token=self.admin_token)
        self.assertEqual(s, 200)

    def test_providers_are_listed_without_secrets_and_without_auth(self):
        s, p, _ = self.call("GET", "/auth/providers")
        self.assertEqual(s, 200)
        self.assertEqual(p, {"ok": True, "providers": [{"id": "google", "label": "Google"}, {"id": "microsoft", "label": "Microsoft"}]})
        self.assertNotIn(SECRET, json.dumps(p))

    def test_login_redirects_to_the_provider_and_sets_a_flow_cookie(self):
        resp, q, cookie = self.flow()
        loc = resp.getheader("Location")
        self.assertTrue(loc.startswith(GOOGLE_ISS + "/authorize?"))
        self.assertEqual(q["redirect_uri"], PUBLIC + "/auth/callback")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        attrs = [a.strip() for a in self.set_cookies(resp)[0].split(";")]
        self.assertTrue(attrs[0].startswith("volt_flow="))
        for want in ("HttpOnly", "SameSite=Lax", "Path=/auth", "Max-Age=600", "Secure"):
            self.assertIn(want, attrs)
        self.assertFalse([a for a in attrs if a.startswith("Domain")])
        self.assertNotIn(attrs[0].split("=", 1)[1], loc)

    def test_a_hostile_host_header_cannot_change_the_redirect_uri(self):
        s, resp = self.begin(headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example", "Origin": "https://evil.example"})
        self.assertEqual(s, 302)
        q = parse_qs(urlsplit(resp.getheader("Location")).query)
        self.assertEqual(q["redirect_uri"], [PUBLIC + "/auth/callback"])
        self.assertNotIn("evil", resp.getheader("Location"))

    def test_bad_provider_is_422_and_missing_parameters_too(self):
        for path, code in (("/auth/login?provider=okta", "bad_provider"), ("/auth/login", "bad_provider"),
                           ("/auth/login?provider=google&provider=microsoft", "bad_provider"), ("/auth/login?provider=google&x=1", "bad_input")):
            s, p, resp = self.call("GET", path)
            self.assertEqual(s, 422, path)
            self.assertEqual(p["error"]["code"], code)
            self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_login_with_the_provider_down_is_503_and_sets_no_cookie(self):
        self.router.down = True
        s, p, resp = self.call("GET", "/auth/login?provider=google")
        self.assertEqual(s, 503)
        self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_callback_happy_path_sets_a_session_and_clears_the_flow_cookie(self):
        _, q, cookie = self.flow(next_="/operator?show=1")
        code = self.google.authorize(q)
        s, _, resp = self.call("GET", f"/auth/callback?code={code}&state={q['state']}", headers={"Cookie": cookie})
        self.assertEqual(s, 302)
        self.assertEqual(resp.getheader("Location"), "/operator?show=1")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        cookies = self.set_cookies(resp)
        session = [c for c in cookies if c.startswith("__Host-volt_session=")]
        self.assertEqual(len(session), 1)
        for want in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(want, [a.strip() for a in session[0].split(";")])
        flow = [c for c in cookies if c.startswith("volt_flow=")]
        self.assertEqual(len(flow), 1)
        self.assertIn("Max-Age=0", flow[0])
        self.assertNotIn(code, resp.getheader("Location"))
        value = session[0].split(";")[0]
        s, who, _ = self.call("GET", "/api/whoami", headers={"Cookie": value})
        self.assertEqual((s, who["name"], who["via"]), (200, "dana", "session"))
        self.assertEqual(self.store.conn.execute("SELECT provider FROM session").fetchone()[0], "oidc:google")
        self.assertEqual(self.store.conn.execute("SELECT action, actor FROM audit WHERE action = 'login'").fetchone()[:], ("login", "dana"))

    def test_a_cookie_session_from_sign_in_cannot_write_without_origin_and_csrf(self):
        _, q, cookie = self.flow()
        code = self.google.authorize(q)
        _, _, resp = self.call("GET", f"/auth/callback?code={code}&state={q['state']}", headers={"Cookie": cookie})
        value = [c for c in self.set_cookies(resp) if c.startswith("__Host-volt_session=")][0].split(";")[0]
        s, p = self.post("/api/groups", {"name": "x"}, headers={"Cookie": value})
        self.assertEqual((s, p["error"]["code"]), (403, "bad_origin"))

    def test_callback_refusals_are_an_error_page_with_no_session(self):
        _, q, cookie = self.flow()
        code = self.google.authorize(q)
        evil = "%3Cscript%3Ealert(1)%3C/script%3E"
        for label, path, headers, status in (
                ("no cookie", f"/auth/callback?code={code}&state={q['state']}", {}, 400),
                ("provider error", f"/auth/callback?error={evil}&error_description={evil}&state={q['state']}", {"Cookie": cookie}, 400),
                ("no parameters", "/auth/callback", {"Cookie": cookie}, 400),
                ("garbage state", "/auth/callback?code=a&state=zzz", {"Cookie": cookie}, 400)):
            with self.subTest(label):
                s, _, resp = self.call("GET", path, headers=headers)
                self.assertEqual(s, status)
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")
                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
                self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
                self.assertFalse([c for c in self.set_cookies(resp) if "session" in c.split("=")[0]])
                flow = [c for c in self.set_cookies(resp) if c.startswith("volt_flow=")]
                self.assertTrue(flow and "Max-Age=0" in flow[0])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 0)
        self.assertGreaterEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'login_failed'").fetchone()[0], 1)

    def test_error_pages_are_fixed_text_and_reflect_nothing(self):
        import http.client
        _, q, cookie = self.flow()
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        conn.request("GET", "/auth/callback?error=%3Cscript%3Ealert(1)%3C/script%3E&state=" + q["state"] + "&x=%3Cb%3E", headers={"Cookie": cookie})
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertNotIn("alert(1)", body)
        self.assertNotIn("<b>", body)
        self.assertNotIn("<script", body.lower())
        self.assertIn("could not be completed", body)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))

    def test_replay_over_http_is_refused(self):
        _, q, cookie = self.flow()
        code = self.google.authorize(q)
        path = f"/auth/callback?code={code}&state={q['state']}"
        self.assertEqual(self.call("GET", path, headers={"Cookie": cookie})[0], 302)
        self.assertEqual(self.call("GET", path, headers={"Cookie": cookie})[0], 400)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)

    def test_a_not_provisioned_user_is_403_and_a_disabled_one_too(self):
        self.google.sub, self.google.claims["email"] = "stranger", "stranger@example.test"
        _, q, cookie = self.flow()
        code = self.google.authorize(q)
        s, _, _ = self.call("GET", f"/auth/callback?code={code}&state={q['state']}", headers={"Cookie": cookie})
        self.assertEqual(s, 403)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 0)

    def test_auth_routes_are_get_only(self):
        for path in ("/auth/login?provider=google", "/auth/callback", "/auth/providers"):
            s, p = self.post(path, {})
            self.assertIn(s, (404, 401, 403), path)

    # -- admin routes ----------------------------------------------------
    def test_registry_and_audit_kinds(self):
        for fn in ("act_group_claims", "act_user_identity"):
            self.assertEqual(serve.ROUTE_ROLES[fn], "admin")
            self.assertIn(fn, {f.__name__ for _, f in serve.ROUTES})

    def test_claim_map_route_is_admin_validated_and_audited(self):
        self.post("/api/groups", {"name": "editors"}, token=self.admin_token)
        path = "/api/groups/editors/claims"
        self.refused(403, "forbidden_role", path, {"provider": "microsoft", "value": "oid-1"}, token=self.op_token)
        s, p = self.post(path, {"provider": "microsoft", "value": "oid-1"}, token=self.admin_token)
        self.assertEqual(s, 200, p)
        s, groups, _ = self.call("GET", "/api/groups", token=self.admin_token)
        self.assertEqual(groups["groups"][0]["claims"], [{"provider": "microsoft", "value": "oid-1"}])
        row = self.store.conn.execute("SELECT actor, action, target_kind, target_id, detail FROM audit WHERE action = 'group_claims'").fetchone()
        self.assertEqual(tuple(row)[:4], ("root", "group_claims", "group", "editors"))
        s, p = self.post(path, {"provider": "microsoft", "value": "oid-1", "remove": True}, token=self.admin_token)
        self.assertEqual(s, 200)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM oidc_group_map").fetchone()[0], 0)
        self.refused(404, "not_found", "/api/groups/nope/claims", {"provider": "microsoft", "value": "x"}, token=self.admin_token)
        for bad in ({}, {"provider": "microsoft"}, {"value": "x"}, {"provider": "", "value": "x"}, {"provider": "microsoft", "value": ""},
                    {"provider": "microsoft", "value": 7}, {"provider": "microsoft", "value": "x" * 300}, {"provider": "Micro soft", "value": "x"},
                    {"provider": "microsoft", "value": "x\ny"}, {"provider": "microsoft", "value": "x", "extra": 1},
                    {"provider": "microsoft", "value": "x", "remove": "yes"}):
            self.refused(422, "bad_input", path, bad, token=self.admin_token)

    def test_deleting_a_group_drops_its_claim_map(self):
        self.post("/api/groups", {"name": "editors"}, token=self.admin_token)
        self.post("/api/groups/editors/claims", {"provider": "microsoft", "value": "oid-1"}, token=self.admin_token)
        self.post("/api/groups/editors/delete", {}, token=self.admin_token)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM oidc_group_map").fetchone()[0], 0)

    def test_user_identity_route_prelinks_an_email(self):
        path = "/api/users/op/identity"
        self.refused(403, "forbidden_role", path, {"email": "op@example.test"}, token=self.op_token)
        s, p = self.post(path, {"email": " Op@Example.test "}, token=self.admin_token)
        self.assertEqual(s, 200, p)
        self.assertEqual(self.store.conn.execute("SELECT email FROM volt_user WHERE name = 'op'").fetchone()[0], "Op@Example.test")
        row = self.store.conn.execute("SELECT actor, action, target_kind, target_id FROM audit WHERE action = 'user_identity'").fetchone()
        self.assertEqual(tuple(row), ("root", "user_identity", "user", "op"))
        self.refused(409, "email_taken", "/api/users/root/identity", {"email": "OP@example.test"}, token=self.admin_token)
        self.refused(404, "no_such_user", "/api/users/ghost/identity", {"email": "g@example.test"}, token=self.admin_token)
        for bad in ({}, {"email": ""}, {"email": "no-at-sign"}, {"email": "a@b@c"}, {"email": "a b@c.test"}, {"email": 7},
                    {"email": "x@y.test" + "z" * 300}, {"email": "a\n@b.test"}, {"email": "x@y.test", "extra": 1}):
            self.refused(422, "bad_input", path, bad, token=self.admin_token)
        s, p = self.post(path, {"email": None}, token=self.admin_token)         # null clears the pre-link
        self.assertEqual(s, 200)
        self.assertIsNone(self.store.conn.execute("SELECT email FROM volt_user WHERE name = 'op'").fetchone()[0])


class Tables(unittest.TestCase):
    NAME = "oidc sign-in tables"

    def migration(self):
        return next(m for m in migrations.MIGRATIONS if m.name == self.NAME)

    def test_the_registry_names_it_and_it_is_idempotent(self):
        m = self.migration()
        self.assertGreater(m.version, 26)
        for stmts in (m.sqlite, m.postgres):
            self.assertTrue(all("IF NOT EXISTS" in s for s in stmts))

    def test_a_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = self.migration()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "before.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            sessions.create(store, "alice", "token", T0)
            store.close()
            conn = sqlite3.connect(path)
            for t in ("auth_flow", "identity", "oidc_group_map"):
                conn.execute(f"DROP TABLE {t}")
            conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            conn.commit()
            conn.close()
            store = Store(path)
            try:
                cols = {t: {r[1] for r in store.conn.execute(f"PRAGMA table_info({t})")} for t in ("auth_flow", "identity", "oidc_group_map")}
                self.assertEqual(cols["auth_flow"], {"state", "nonce", "code_verifier", "provider", "next", "binder_hash", "created_at", "expires_at"})
                self.assertEqual(cols["identity"], {"issuer", "sub", "user_name", "email", "display_name", "last_login_at"})
                self.assertEqual(cols["oidc_group_map"], {"provider", "claim_value", "group_name"})
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
                self.assertEqual([r[0] for r in store.conn.execute("SELECT version FROM schema_version ORDER BY version")],
                                 list(range(1, migrations.LATEST + 1)))
                store.conn.execute("INSERT INTO identity VALUES ('i', 's', 'alice', NULL, NULL, 'now')")
                with self.assertRaises(store.IntegrityError):
                    store.conn.execute("INSERT INTO identity VALUES ('i', 's', 'alice', NULL, NULL, 'now')")
            finally:
                store.close()

    def test_the_oldest_historical_shape_gains_them_on_sqlite(self):
        from tests.historical_shapes import SHAPES
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(SHAPES[0][2])
            conn.close()
            store = Store(path)
            try:
                names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({"auth_flow", "identity", "oidc_group_map"} <= names)
            finally:
                store.close()


class TablesPostgres(PgCase):
    def test_a_postgres_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = Tables().migration()
        store = self.store()
        users.add(store, "alice", "operator")
        for t in ("auth_flow", "identity", "oidc_group_map"):
            store.conn.execute(f"DROP TABLE {t}")
        store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        rows = again.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_name IN ('auth_flow', 'identity', 'oidc_group_map')").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
        # and the flow itself works on Postgres: begin, sign in, replay refused
        oidc.reset_caches()
        with tempfile.TemporaryDirectory() as tmp:
            t = [T0]
            google, ms = make_idps(lambda: t[0])
            http = Router(google, ms)
            cfg = oidc.load_config(write_config(tmp), ENV)
            access.set_user_email(again, "alice", "alice@example.test")
            google.claims["email"] = "alice@example.test"
            url, cookie = oidc.begin(again, cfg, "google", "/x", t[0], http)
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            query = {"code": google.authorize(q), "state": q["state"]}
            res = oidc.complete(again, cfg, query, cookie, t[0], http)
            self.assertEqual((res.user_name, res.next), ("alice", "/x"))
            with self.assertRaises(oidc.LoginError):
                oidc.complete(again, cfg, query, cookie, t[0], http)
            self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
            self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'login_failed'").fetchone()[0], 1)

    def test_one_flow_is_used_by_exactly_one_of_many_racing_callbacks(self):
        import threading
        oidc.reset_caches()
        store = self.store()
        users.add(store, "alice", "operator")
        access.set_user_email(store, "alice", "alice@example.test")
        with tempfile.TemporaryDirectory() as tmp:
            google, ms = make_idps(lambda: T0)
            http = Router(google, ms)
            cfg = oidc.load_config(write_config(tmp), ENV)
            google.claims["email"] = "alice@example.test"
            url, cookie = oidc.begin(store, cfg, "google", "/", T0, http)
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            query = {"code": google.authorize(q), "state": q["state"]}
            wins, lock = [], threading.Lock()

            def go():
                s = self.store()
                try:
                    oidc.complete(s, cfg, query, cookie, T0, http)
                    with lock:
                        wins.append(1)
                except oidc.LoginError:
                    pass
            threads = [threading.Thread(target=go) for _ in range(8)]
            [th.start() for th in threads]
            [th.join() for th in threads]
            self.assertEqual(len(wins), 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)


class RaceSqlite(unittest.TestCase):
    def test_one_flow_is_used_by_exactly_one_of_many_racing_callbacks(self):
        import threading
        oidc.reset_caches()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            access.set_user_email(store, "alice", "alice@example.test")
            google, ms = make_idps(lambda: T0)
            http = Router(google, ms)
            cfg = oidc.load_config(write_config(tmp), ENV)
            google.claims["email"] = "alice@example.test"
            url, cookie = oidc.begin(store, cfg, "google", "/", T0, http)
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            query = {"code": google.authorize(q), "state": q["state"]}
            wins, lock = [], threading.Lock()

            def go():
                s = Store(path)
                try:
                    oidc.complete(s, cfg, query, cookie, T0, http)
                    with lock:
                        wins.append(1)
                except oidc.LoginError:
                    pass
                finally:
                    s.close()
            threads = [threading.Thread(target=go) for _ in range(8)]
            [th.start() for th in threads]
            [th.join() for th in threads]
            self.assertEqual(len(wins), 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
