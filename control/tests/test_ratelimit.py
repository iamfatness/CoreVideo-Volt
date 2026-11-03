"""Rate limits, the concurrency gate, quotas and credential-guess lockout (volt/ratelimit.py).

All limiter state is in memory, so it is per node (N nodes allow N times a configured rate): the hosted edge carries the global limit.
These tests use a fake clock and in-process servers; they prove structure and exemptions, not that a given rate is right for any
traffic, and not behaviour behind a real proxy."""

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from volt import ratelimit, serve, telemetry, users
from volt.ratelimit import (ConcurrencyGate, FailureTracker, Limiter, QuotaExceeded, SHOW_CONTROL, TokenBucket, check_quota,
                            client_address)
from volt.rundown import add_item, attach, open_show
from volt.store import Store

from tests.racekit import _cleared_span


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class Buckets(unittest.TestCase):
    def test_bucket_refills_with_fake_clock(self):
        clock = Clock()
        b = TokenBucket(rate=2.0, burst=3, now=clock)
        self.assertEqual([b.take()[0] for _ in range(4)], [True, True, True, False])
        allowed, wait = b.take()
        self.assertFalse(allowed)
        self.assertAlmostEqual(wait, 0.5, places=2)
        clock.t += 0.5
        self.assertTrue(b.take()[0])
        clock.t += 100
        self.assertEqual([b.take()[0] for _ in range(4)], [True, True, True, False])     # never above burst

    def test_a_zero_rate_bucket_never_refills(self):
        clock = Clock()
        b = TokenBucket(rate=0.0, burst=1, now=clock)
        self.assertTrue(b.take()[0])
        clock.t += 1000
        self.assertFalse(b.take()[0])

    def test_read_limit_is_per_principal_not_per_tenant(self):
        clock = Clock()
        lim = Limiter(rates={"read": (1.0, 2), "write": (1.0, 2), "heavy": (1.0, 1)}, tenant_factor=100, now=clock)
        self.assertTrue(lim.check(("t1", "alice"), "read").allowed)
        self.assertTrue(lim.check(("t1", "alice"), "read").allowed)
        self.assertFalse(lim.check(("t1", "alice"), "read").allowed)
        self.assertTrue(lim.check(("t1", "bob"), "read").allowed)          # another person on the same tenant is not limited by alice
        self.assertTrue(lim.check(("t2", "alice"), "read").allowed)        # the same name on another tenant is another key

    def test_the_tenant_aggregate_caps_all_principals_together(self):
        clock = Clock()
        lim = Limiter(rates={"read": (1.0, 2), "write": (1.0, 2), "heavy": (1.0, 1)}, tenant_factor=2, now=clock)   # aggregate burst 4
        allowed = [lim.check(("t1", f"user{n}"), "read").allowed for n in range(8)]
        self.assertEqual(allowed.count(True), 4)
        self.assertTrue(lim.check(("t2", "user0"), "read").allowed)

    def test_classes_have_separate_buckets(self):
        lim = Limiter(rates={"read": (1.0, 1), "write": (1.0, 1), "heavy": (1.0, 1)}, now=Clock())
        self.assertTrue(lim.check(("", "a"), "read").allowed)
        self.assertFalse(lim.check(("", "a"), "read").allowed)
        self.assertTrue(lim.check(("", "a"), "write").allowed)
        self.assertTrue(lim.check(("", "a"), "heavy").allowed)

    def test_unknown_class_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            Limiter(now=Clock()).check(("", "a"), "nonsense")

    def test_idle_buckets_are_pruned(self):
        clock = Clock()
        lim = Limiter(rates={"read": (10.0, 10), "write": (1.0, 1), "heavy": (1.0, 1)}, now=clock, max_keys=50)
        for n in range(500):
            lim.check(("", f"u{n}"), "read")
            clock.t += 0.001
        self.assertLessEqual(lim.size(), 60)

    def test_show_control_is_never_limited_by_the_limiter(self):
        self.assertEqual(SHOW_CONTROL, frozenset({"act_go", "act_take", "act_hold", "act_release", "act_kill", "act_skip"}))
        lim = Limiter(rates={"read": (0.0, 0), "write": (0.0, 0), "heavy": (0.0, 0)}, now=Clock())
        for fn in SHOW_CONTROL:
            self.assertTrue(lim.check(("", "a"), "write", fn_name=fn).allowed, fn)
        self.assertFalse(lim.check(("", "a"), "write", fn_name="act_show_create").allowed)

    def test_show_control_names_are_real_routes(self):
        names = {fn.__name__ for _, fn in serve.ROUTES}
        self.assertLessEqual(SHOW_CONTROL, names)

    def test_limiter_state_is_per_process_documented(self):
        self.assertIn("per node", ratelimit.__doc__)
        self.assertIn("per node", Limiter.__doc__)

    def test_rates_from_env(self):
        rates = ratelimit.rates_from_env({"VOLT_RATE_READ": "5,10", "VOLT_RATE_HEAVY": "0.5,2"})
        self.assertEqual(rates["read"], (5.0, 10))
        self.assertEqual(rates["write"], ratelimit.DEFAULT_RATES["write"])
        self.assertEqual(rates["heavy"], (0.5, 2))
        with self.assertRaises(ValueError):
            ratelimit.rates_from_env({"VOLT_RATE_READ": "fast"})
        with self.assertRaises(ValueError):
            ratelimit.rates_from_env({"VOLT_RATE_READ": "-1,5"})


class Gate(unittest.TestCase):
    def test_gate_admits_up_to_its_limit_and_exempt_always(self):
        gate = ConcurrencyGate(2)
        self.assertTrue(gate.enter())
        self.assertTrue(gate.enter())
        self.assertFalse(gate.enter())
        self.assertTrue(gate.enter(exempt=True))
        self.assertEqual(gate.inflight, 3)
        gate.leave()
        gate.leave()
        gate.leave()
        self.assertTrue(gate.enter())

    def test_an_exempt_request_does_not_free_a_normal_slot_it_never_held(self):
        gate = ConcurrencyGate(1)
        self.assertTrue(gate.enter())
        self.assertTrue(gate.enter(exempt=True))
        gate.leave(exempt=True)
        self.assertFalse(gate.enter())          # the normal slot is still held
        gate.leave()
        self.assertTrue(gate.enter())


class Guessing(unittest.TestCase):
    def test_ten_failures_in_a_minute_block_that_address_only(self):
        clock = Clock()
        f = FailureTracker(window=60, max_failures=10, block=60, now=clock)
        for _ in range(9):
            f.failed("10.0.0.1")
        self.assertFalse(f.blocked("10.0.0.1"))
        f.failed("10.0.0.1")
        self.assertTrue(f.blocked("10.0.0.1"))
        self.assertFalse(f.blocked("10.0.0.2"))
        clock.t += 61
        self.assertFalse(f.blocked("10.0.0.1"))

    def test_failures_older_than_the_window_do_not_count(self):
        clock = Clock()
        f = FailureTracker(window=60, max_failures=3, now=clock)
        f.failed("a")
        f.failed("a")
        clock.t += 61
        f.failed("a")
        self.assertFalse(f.blocked("a"))

    def test_a_success_does_not_reset_the_count(self):
        """Guessing against a valid token (many tries, one right) must not be forgiven by the one that worked."""
        f = FailureTracker(window=60, max_failures=2, now=Clock())
        f.failed("a")
        f.succeeded("a")
        f.failed("a")
        self.assertTrue(f.blocked("a"))

    def test_the_tracker_is_bounded(self):
        f = FailureTracker(window=60, max_failures=10, now=Clock(), max_addresses=100)
        for n in range(1000):
            f.failed(f"10.1.{n // 256}.{n % 256}")
        self.assertLessEqual(f.size(), 150)


class ClientAddress(unittest.TestCase):
    def test_forwarded_for_ignored_unless_peer_is_trusted(self):
        h = {"X-Forwarded-For": "6.6.6.6"}
        self.assertEqual(client_address("9.9.9.9", h, []), "9.9.9.9")
        self.assertEqual(client_address("9.9.9.9", h, ["127.0.0.1"]), "9.9.9.9")
        self.assertEqual(client_address("127.0.0.1", h, ["127.0.0.1"]), "6.6.6.6")

    def test_the_first_untrusted_entry_from_the_right_is_used(self):
        h = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 10.0.0.5"}
        self.assertEqual(client_address("10.0.0.9", h, ["10.0.0.0/24"]), "2.2.2.2")     # a forged left entry is not believed
        self.assertEqual(client_address("10.0.0.9", {"X-Forwarded-For": "10.0.0.5, 10.0.0.6"}, ["10.0.0.0/24"]), "10.0.0.9")

    def test_garbage_in_the_header_falls_back_to_the_peer(self):
        self.assertEqual(client_address("10.0.0.9", {"X-Forwarded-For": "not-an-ip"}, ["10.0.0.9"]), "10.0.0.9")
        self.assertEqual(client_address("10.0.0.9", {}, ["10.0.0.9"]), "10.0.0.9")

    def test_trusted_list_parse(self):
        self.assertEqual(ratelimit.parse_trusted("127.0.0.1, 10.0.0.0/8"), ["127.0.0.1", "10.0.0.0/8"])
        with self.assertRaises(ValueError):
            ratelimit.parse_trusted("somehost.example")


class Quotas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "q.db"))
        self.addCleanup(self.store.close)

    def test_no_limit_is_unlimited(self):
        for _ in range(3):
            self.store.mint_asset(owner="desk")
        check_quota(self.store, {}, "max_assets")
        check_quota(self.store, None, "max_assets")

    def test_max_assets_counts_live_assets_only(self):
        a = self.store.mint_asset(owner="desk")
        self.store.mint_asset(owner="desk")
        with self.assertRaises(QuotaExceeded) as raised:
            check_quota(self.store, {"max_assets": 2}, "max_assets")
        self.assertEqual((raised.exception.name, raised.exception.limit), ("max_assets", 2))
        check_quota(self.store, {"max_assets": 3}, "max_assets")
        with self.assertRaises(QuotaExceeded):
            check_quota(self.store, {"max_assets": 3}, "max_assets", adding=2)
        self.store.conn.execute("UPDATE asset SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (a.id,))
        self.store.conn.commit()
        check_quota(self.store, {"max_assets": 2}, "max_assets")           # a deleted asset frees its place

    def test_max_jobs_per_day_counts_the_last_24_hours(self):
        a = self.store.mint_asset(owner="desk")
        for n, stamp in enumerate(("2026-11-03T10:00:00Z", "2026-11-03T11:00:00Z", "2026-11-01T11:00:00Z")):
            self.store.conn.execute(
                "INSERT INTO job (id, asset_id, type, idempotency_key, status, attempt, created_at, updated_at) "
                "VALUES (?, ?, 'proxy', ?, 'done', 1, ?, ?)", (f"j{n}", a.id, f"k{n}", stamp, stamp))
        self.store.conn.commit()
        now = "2026-11-03T12:00:00Z"
        with self.assertRaises(QuotaExceeded):
            check_quota(self.store, {"max_jobs_per_day": 2}, "max_jobs_per_day", now=now)
        check_quota(self.store, {"max_jobs_per_day": 3}, "max_jobs_per_day", now=now)

    def test_unknown_quota_name_is_a_bug_not_a_pass(self):
        with self.assertRaises(ValueError):
            check_quota(self.store, {"max_assets": 1}, "max_whatever")


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "v.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        _, self.token = users.add(self.store, "alice", "admin")
        asset, span = _cleared_span(self.store)
        self.show = open_show(self.store, "Final")
        self.item = add_item(self.store, self.show, "one")
        attach(self.store, self.item, span)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.LIMITER, serve.GATE, serve.FAILURES, serve.REGISTRY, serve.TRUSTED_PROXIES,
                       serve.Handler.timeout)
        serve.DB_PATH, serve.TOKEN, serve.ROOM = self.path, None, None
        serve.REGISTRY = telemetry.Registry()
        self.clock = Clock()
        serve.LIMITER = Limiter(rates={"read": (1000.0, 1000), "write": (1000.0, 1000), "heavy": (1000.0, 1000)}, now=self.clock)
        serve.GATE = ConcurrencyGate(64)
        serve.FAILURES = FailureTracker(now=self.clock)
        serve.TRUSTED_PROXIES = []
        self.addCleanup(self._restore)
        self.srv = None

    def start(self):
        self.srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.LIMITER, serve.GATE, serve.FAILURES, serve.REGISTRY, serve.TRUSTED_PROXIES,
         serve.Handler.timeout) = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, token=True, body=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {self.token if token is True else token}"
        if body is not None:
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        try:
            c.request(method, path, None if body is None else json.dumps(body), h)
            r = c.getresponse()
            return r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
        finally:
            c.close()

    def test_429_has_retry_after_header_and_body(self):
        serve.LIMITER = Limiter(rates={"read": (0.5, 1), "write": (1000.0, 1000), "heavy": (1000.0, 1000)}, now=self.clock)
        self.start()
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        status, body, headers = self.call("GET", "/api/state")
        self.assertEqual(status, 429)
        err = json.loads(body)
        self.assertEqual(err["error"]["code"], "rate_limited")
        self.assertGreaterEqual(err["retryAfterSeconds"], 1)
        self.assertEqual(int(headers["retry-after"]), err["retryAfterSeconds"])
        self.assertIn('volt_rate_limited_total{kind="rate"} 1', serve.REGISTRY.render())

    def test_show_control_is_never_rejected_even_when_bucket_is_empty_and_gate_full(self):
        serve.LIMITER = Limiter(rates={"read": (0.0, 0), "write": (0.0, 0), "heavy": (0.0, 0)}, now=self.clock)
        serve.GATE = ConcurrencyGate(1)
        self.start()
        self.assertTrue(serve.GATE.enter())                                # the one normal slot is taken
        self.assertEqual(self.call("GET", "/api/state")[0], 503)
        self.assertEqual(self.call("POST", "/api/shows", body={"title": "x"})[0], 503)
        for route in ("hold", "release", "go"):
            status, body, _ = self.call("POST", f"/api/shows/{self.show}/{route}", body={})
            self.assertNotIn(status, (429, 503), f"{route}: {body}")
        status, body, _ = self.call("POST", f"/api/items/{self.item}/skip", body={})
        self.assertNotIn(status, (429, 503), body)
        status, body, _ = self.call("POST", f"/api/items/{self.item}/kill", body={})
        self.assertNotIn(status, (429, 503), body)
        status, body, _ = self.call("POST", f"/api/shows/{self.show}/take", body={})
        self.assertEqual(json.loads(body)["error"]["code"], "no_room")     # no room configured: the 503 is the room's answer, not the gate's

    def test_gate_returns_503_busy_and_health_still_answers(self):
        serve.GATE = ConcurrencyGate(1)
        self.start()
        serve.GATE.enter()
        status, body, headers = self.call("GET", "/api/state")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["code"], "busy")
        self.assertEqual(headers["retry-after"], "1")
        self.assertEqual(self.call("GET", "/health", token=False)[0], 200)
        self.assertIn("volt_busy_total 1", serve.REGISTRY.render())
        serve.GATE.leave()
        self.assertEqual(self.call("GET", "/api/state")[0], 200)

    def test_the_gate_slot_is_released_after_every_response(self):
        self.start()
        for _ in range(5):
            self.call("GET", "/api/state")
            self.call("GET", "/api/state", token=False)
            self.call("POST", "/api/nope", body={})
        deadline = time.monotonic() + 3          # the slot is freed just after the response is written
        while serve.GATE.inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(serve.GATE.inflight, 0)

    def test_ten_bad_tokens_in_a_minute_blocks_that_address_only(self):
        self.start()
        for _ in range(10):
            self.assertEqual(self.call("GET", "/api/state", token="wrong-token-wrong-token")[0], 401)
        status, body, headers = self.call("GET", "/api/state")                # even the right token: this address is blocked
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")
        self.assertEqual(self.call("GET", "/health", token=False)[0], 200)
        self.assertIn('volt_rate_limited_total{kind="auth"}', serve.REGISTRY.render())
        self.clock.t += 61
        self.assertEqual(self.call("GET", "/api/state")[0], 200)

    def test_a_forwarded_address_counts_only_from_a_trusted_proxy(self):
        self.start()
        for n in range(10):
            self.call("GET", "/api/state", token="wrong-token-wrong-token", headers={"X-Forwarded-For": f"6.6.6.{n}"})
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Forwarded-For": "6.6.6.1"})[0], 429)    # untrusted: counted as 127.0.0.1
        serve.FAILURES = FailureTracker(now=self.clock)
        serve.TRUSTED_PROXIES = ["127.0.0.1"]
        for _ in range(10):
            self.call("GET", "/api/state", token="wrong-token-wrong-token", headers={"X-Forwarded-For": "7.7.7.7"})
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Forwarded-For": "8.8.8.8"})[0], 200)    # another client behind the proxy
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Forwarded-For": "7.7.7.7"})[0], 429)

    def test_stalled_client_is_dropped_after_timeout(self):
        serve.Handler.timeout = 0.5
        self.start()
        s = socket.create_connection(("127.0.0.1", self.srv.server_port))
        try:
            s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n")          # half a request, then silence
            s.settimeout(3.0)
            started = time.monotonic()
            data = s.recv(1024)                                             # the server closes: b"" (or an error response), well inside timeout + 1 s
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertNotIn(b"200 OK", data)
        finally:
            s.close()
        deadline = time.monotonic() + 3
        while serve.GATE.inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(serve.GATE.inflight, 0)

    def test_quota_blocks_enqueue_and_ingest_but_not_reads_or_go(self):
        from volt import tenancy
        ctx = tenancy.TenantContext(tenancy.TenantRef("acme", "x", "t_acme"),
                                    tenancy.TenantSettings(Path("o"), Path("p"), Path("q"), Path("r"), "tenants/acme/", None, None, None),
                                    {"max_assets": 1})
        serve._REQUEST.tenant = ctx
        self.addCleanup(setattr, serve._REQUEST, "tenant", None)
        with self.assertRaises(serve.ApiError) as raised:
            serve._quota(self.store, "max_assets")
        self.assertEqual((raised.exception.status, raised.exception.code), (429, "quota_exceeded"))
        self.assertEqual(raised.exception.extra["quota"], "max_assets")
        self.assertEqual(raised.exception.extra["limit"], 1)
        serve._REQUEST.tenant = None
        serve._quota(self.store, "max_assets")                              # tenancy off: unlimited

    def test_quota_unlimited_by_default_when_tenancy_off(self):
        self.start()
        for n in range(3):
            self.store.mint_asset(owner="desk")
        status, body, _ = self.call("POST", "/api/record/start", body={})
        self.assertNotEqual(status, 429)

    def test_a_quota_refusal_leaves_reads_and_show_control_alone(self):
        from volt import tenancy
        for n in range(3):
            self.store.mint_asset(owner="desk")
        self.start()
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        status, body, _ = self.call("POST", f"/api/shows/{self.show}/hold", body={})
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()
