"""Request economy (docs/superpowers/specs/2026-11-01-scale-operations-design.md section 3): one store per request, a shared
cache of the room observation, HTTP/1.1 keep-alive with a Content-Length on every response, a bigger listen queue, and the
load script. Nothing here is a capacity claim: the numbers belong to the box that measured them (docs/operations.md)."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from volt import operator_state, serve, users
from volt.rooms import FakeRoom, LiveError
from volt.rundown import add_item, open_show
from volt.store import Store

from tests.test_rooms import obs

ADMIN = "a" * 24


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class CountingRoom(FakeRoom):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.observed = 0

    def observe(self):
        self.observed += 1
        return super().observe()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "volt.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        _, self.token = users.add(self.store, "alice", "operator")
        self.show = open_show(self.store, "Final Four")
        add_item(self.store, self.show, "first")
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.Store, serve.ROOM_CACHE, serve.MAX_BODY)
        serve.DB_PATH, serve.TOKEN, serve.ROOM = self.path, None, None
        self.addCleanup(self._restore)
        self.srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.Store, serve.ROOM_CACHE, serve.MAX_BODY = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)

    def get(self, path, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {self.token}"
        c = self.conn()
        try:
            c.request("GET", path, headers=h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def post(self, path, body=None, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {self.token}"
        c = self.conn()
        try:
            c.request("POST", path, json.dumps(body or {}), h)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


class OneStore(Base):
    def _counting(self):
        made = []
        real = serve.Store

        class Counting(real):
            def __init__(self, *a, **kw):
                made.append(1)
                super().__init__(*a, **kw)

        serve.Store = Counting
        return made

    def test_one_store_per_request(self):
        self.get("/api/state")          # the first request on a file also bootstraps it (one more Store); count after that
        made = self._counting()
        status, _, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(made), 1, "a bearer-authenticated GET opened more than one store")
        del made[:]
        status, _ = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(len(made), 1, "a bearer-authenticated POST opened more than one store")

    def test_a_refused_token_costs_one_store_not_three(self):
        self.get("/api/state")
        made = self._counting()
        c = self.conn()
        c.request("GET", "/api/state", headers={"Authorization": "Bearer nope-nope-nope-nope"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 401)
        self.assertEqual(len(made), 1)


class SqliteBootstrap(Base):
    """Every request used to open its Store with init=True, which on SQLite runs the schema script and BEGIN IMMEDIATE (the write lock)
    for a read-only poll. Under concurrent desks those requests queue on one lock: 5 desks got a fifth of one desk's throughput."""

    def test_requests_do_not_rerun_the_schema_bootstrap(self):
        calls = []
        real = Store._migrate

        def counting(store):
            calls.append(1)
            return real(store)
        Store._migrate = counting
        self.addCleanup(setattr, Store, "_migrate", real)
        for _ in range(6):
            self.assertEqual(self.get("/api/state")[0], 200)
        self.post(f"/api/shows/{self.show}/hold", {"reason": "x"})
        self.assertLessEqual(len(calls), 1, "each request ran the migration bootstrap")

    def test_a_fresh_database_file_is_still_bootstrapped_by_the_first_request(self):
        fresh = str(Path(self.tmp.name) / "fresh.db")
        serve.DB_PATH = fresh
        status, _, body = self.get("/api/state", token=False)
        self.assertEqual(status, 200, body)
        self.assertTrue(Path(fresh).exists())

    def test_a_read_request_does_not_wait_for_a_writer(self):
        """A write transaction held open on another connection must not stall the poll (the bootstrap's BEGIN IMMEDIATE would)."""
        self.get("/api/state")     # bootstrapped by now
        writer = Store(self.path)
        try:
            writer.conn.execute("BEGIN IMMEDIATE")
            started = __import__("time").monotonic()
            status, _, _ = self.get("/api/state")
            took = __import__("time").monotonic() - started
        finally:
            writer.conn.rollback()
            writer.close()
        self.assertEqual(status, 200)
        self.assertLess(took, 3.0)


class ObservationCache(unittest.TestCase):
    def test_observe_once_per_ttl(self):
        clock = Clock()
        cache = serve.RoomObservationCache(ttl=1.0, now=clock)
        calls = []

        def factory():
            calls.append(1)
            return {"configured": True, "reachable": True, "state": "idle"}

        for _ in range(50):
            cache.get("r", factory)
        self.assertEqual(len(calls), 1)
        clock.t += 1.1
        out = cache.get("r", factory)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["observedAgeMs"], 0)

    def test_age_is_reported_and_keys_are_separate(self):
        clock = Clock()
        cache = serve.RoomObservationCache(ttl=1.0, now=clock)
        cache.get("a", lambda: {"state": "x"})
        clock.t += 0.4
        self.assertEqual(cache.get("a", lambda: {"state": "y"})["observedAgeMs"], 400)
        self.assertEqual(cache.get("b", lambda: {"state": "z"})["state"], "z")

    def test_the_cached_value_is_a_copy(self):
        cache = serve.RoomObservationCache(ttl=1.0, now=Clock())
        first = cache.get("a", lambda: {"state": "x"})
        first["state"] = "mutated"
        self.assertEqual(cache.get("a", lambda: {"state": "never"})["state"], "x")

    def test_concurrent_misses_make_one_call(self):
        cache = serve.RoomObservationCache(ttl=1.0)
        calls = []
        gate = threading.Event()

        def factory():
            calls.append(1)
            gate.wait(5)
            return {"state": "x"}

        threads = [threading.Thread(target=cache.get, args=("r", factory)) for _ in range(8)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(calls), 1)


class StatePolls(Base):
    def test_room_observed_once_per_second_for_50_polls(self):
        clock = Clock()
        serve.ROOM_CACHE = serve.RoomObservationCache(ttl=1.0, now=clock)
        room = CountingRoom([obs("idle")])
        serve.ROOM = room
        for _ in range(50):
            status, _, body = self.get("/api/state")
            self.assertEqual(status, 200)
        self.assertEqual(room.observed, 1)
        clock.t += 1.1
        self.get("/api/state")
        self.assertEqual(room.observed, 2)

    def test_a_room_error_is_cached_for_the_same_ttl_and_still_unreachable(self):
        clock = Clock()
        serve.ROOM_CACHE = serve.RoomObservationCache(ttl=1.0, now=clock)

        class Down(CountingRoom):
            def observe(self):
                self.observed += 1
                raise LiveError("core unreachable")

        room = Down([obs("idle")])
        serve.ROOM = room
        for _ in range(5):
            _, _, body = self.get("/api/state")
            state = json.loads(body)["room"]
            self.assertEqual((state["configured"], state["reachable"]), (True, False))
        self.assertEqual(room.observed, 1)

    def test_state_payload_unchanged_except_observed_age(self):
        serve.ROOM_CACHE = serve.RoomObservationCache(ttl=1.0, now=Clock())
        serve.ROOM = CountingRoom([obs("producing", path="x.mxf", finalized=False)])
        _, _, body = self.get("/api/state")
        got = json.loads(body)["room"]
        self.assertIn("observedAgeMs", got)
        got.pop("observedAgeMs")
        want = operator_state.room_state(lambda: CountingRoom([obs("producing", path="x.mxf", finalized=False)]))
        self.assertEqual(got, want)

    def test_an_unconfigured_room_is_not_cached_away(self):
        serve.ROOM_CACHE = serve.RoomObservationCache(ttl=1.0, now=Clock())
        _, _, body = self.get("/api/state")
        room = json.loads(body)["room"]
        self.assertEqual((room["configured"], room["reachable"]), (False, False))


class KeepAlive(Base):
    def test_keepalive_two_requests_one_connection(self):
        c = self.conn()
        try:
            c.request("GET", "/health")
            r1 = c.getresponse()
            r1.read()
            sock = c.sock
            self.assertEqual(r1.version, 11)
            self.assertIsNotNone(sock, "the server closed the connection after one response")
            c.request("GET", "/api/state", headers={"Authorization": f"Bearer {self.token}"})
            r2 = c.getresponse()
            r2.read()
            self.assertEqual(r2.status, 200)
            self.assertIs(c.sock, sock, "the second request needed a new connection")
        finally:
            c.close()

    def test_a_refused_post_drains_its_body_so_the_socket_stays_in_step(self):
        """A 401 on a POST never reaches the handler that reads the body; on a kept-alive socket those bytes would be taken for the next
        request. The server reads and drops them, so the same connection serves the next request correctly."""
        c = self.conn()
        try:
            c.request("POST", f"/api/shows/{self.show}/hold", json.dumps({"reason": "x" * 50}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 401)
            sock = c.sock
            c.request("GET", "/health")
            r2 = c.getresponse()
            self.assertEqual(r2.status, 200)
            self.assertEqual(json.loads(r2.read())["status"], "ok")
            self.assertIs(c.sock, sock)
        finally:
            c.close()

    def test_an_oversize_or_chunked_refused_post_closes_the_connection(self):
        serve.MAX_BODY = 64
        c = self.conn()
        try:
            c.request("POST", f"/api/shows/{self.show}/hold", json.dumps({"reason": "x" * 500}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 401)
            self.assertEqual(r.getheader("Connection"), "close")
        finally:
            c.close()

    def test_the_listen_queue_is_bigger_than_the_stdlib_default(self):
        self.assertEqual(serve.VoltServer.request_queue_size, 128)
        self.assertEqual(serve.Handler.protocol_version, "HTTP/1.1")

    def test_every_response_has_content_length(self):
        serve.MAX_BODY = 64
        a = self.store.mint_asset(owner="desk")
        proxy = Path(self.tmp.name) / "p.mp4"
        proxy.write_bytes(b"0123456789" * 100)
        self.store.add_essence(a.id, role="proxy", location=str(proxy))
        cases = [
            ("GET", "/", False, None, None),                                   # static
            ("GET", "/operator/operator.js", False, None, None),
            ("GET", "/health", False, None, None),
            ("GET", "/api/state", True, None, None),                           # JSON
            ("GET", "/api/state", False, None, None),                          # 401
            ("GET", "/nope", True, None, None),                                # 404
            ("GET", f"/api/assets/{a.id}/proxy", True, {"Range": "bytes=0-99"}, None),    # 206
            ("GET", f"/api/assets/{a.id}/proxy", True, None, None),            # 200 media
            ("GET", f"/api/assets/{a.id}/proxy", True, {"Range": "bytes=5000-6000"}, None),   # unsatisfiable
            ("POST", f"/api/shows/{self.show}/hold", True, None, b'{"reason": "x"}'),           # POST ok
            ("POST", f"/api/shows/{self.show}/hold", True, None, b'{"reason": "' + b"x" * 200 + b'"}'),   # 413
            ("POST", "/api/nope", True, None, b"{}"),                          # 404
            ("POST", f"/api/shows/{self.show}/hold", False, None, b"{}"),      # 401
        ]
        for method, path, auth, headers, body in cases:
            h = dict(headers or {})
            if auth:
                h["Authorization"] = f"Bearer {self.token}"
            if body is not None:
                h["Content-Type"] = "application/json"
            c = self.conn()
            try:
                c.request(method, path, body, h)
                r = c.getresponse()
                data = r.read()
                length = r.getheader("Content-Length")
                self.assertIsNotNone(length, f"{method} {path} {r.status} has no Content-Length")
                self.assertEqual(int(length), len(data), f"{method} {path} {r.status}")
            finally:
                c.close()


class LibraryQuery(unittest.TestCase):
    """The load harness found the state poll's asset list was quadratic: no index on essence(asset_id), so each asset's proxy lookup
    scanned the whole essence table (about 2.8 s for 10,000 assets on the box in docs/operations.md)."""

    def test_the_asset_list_does_not_scan_essence_per_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            from bench import load_state
            db = str(Path(tmp) / "l.db")
            load_state.seed(db, 300)
            store = Store(db)
            try:
                plan = " | ".join(str(r[3]) for r in store.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT a.id, e.location, EXISTS (SELECT 1 FROM essence p WHERE p.asset_id = a.id AND p.role = 'proxy') "
                    "FROM asset a LEFT JOIN essence e ON e.asset_id = a.id AND e.role = 'hi-res' WHERE a.deleted_at IS NULL"))
            finally:
                store.close()
        self.assertNotIn("SCAN p", plan)
        self.assertNotIn("AUTOMATIC", plan)
        self.assertIn("essence_by_asset", plan)

    def test_migration_adds_the_index_to_an_existing_database(self):
        from volt import migrations
        m = next(m for m in migrations.MIGRATIONS if m.name == "essence by asset index")
        self.assertTrue(all("IF NOT EXISTS" in s for s in m.sqlite + m.postgres))
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "l.db")
            store = Store(db)
            store.conn.execute("DROP INDEX essence_by_asset")
            store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            store.conn.commit()
            store.close()
            store = Store(db)
            try:
                names = {r[1] for r in store.conn.execute("PRAGMA index_list(essence)")}
                self.assertIn("essence_by_asset", names)
            finally:
                store.close()

    def test_both_schemas_declare_the_index(self):
        from volt import store as store_mod
        root = Path(store_mod.__file__).parent
        self.assertIn("CREATE INDEX IF NOT EXISTS essence_by_asset", (root / "schema.sql").read_text(encoding="utf-8"))
        self.assertIn("CREATE INDEX IF NOT EXISTS essence_by_asset", store_mod.SCHEMA)


class LoadScript(unittest.TestCase):
    def test_load_script_runs_against_in_process_server(self):
        from bench import load_state
        report = load_state.run_in_process(desks=5, seconds=2, assets=20, keepalive=True)
        for key in ("p50Ms", "p95Ms", "p99Ms", "rps", "requests", "errors", "sqlStatementsPerRequest", "keepalive", "assets", "desks"):
            self.assertIn(key, report)
        self.assertGreater(report["requests"], 0)
        self.assertEqual(report["errors"], 0)
        self.assertGreaterEqual(report["sqlStatementsPerRequest"], 1)
        json.dumps(report)

    def test_both_keepalive_modes_are_measurable(self):
        from bench import load_state
        for keep in (True, False):
            report = load_state.run_in_process(desks=2, seconds=1, assets=5, keepalive=keep)
            self.assertEqual(report["keepalive"], keep)
            self.assertEqual(report["errors"], 0)


if __name__ == "__main__":
    unittest.main()
