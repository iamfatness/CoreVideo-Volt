"""Two `serve` processes against one database (docs/superpowers/specs/2026-11-01-scale-operations-design.md section 2).

go, skip and kill are ordered by the show's own lock in the database, not by anything in the process; record start and stop
are ordered by a lease row. These tests run the HTTP handlers on two connections in threads, and two real `serve` processes
on loopback, on SQLite and, with VOLT_TEST_PG_DSN set, on Postgres. Nothing here proves a load balancer or a second host."""

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from volt import serve
from volt.cues import add_cue
from volt.rooms import FakeRoom
from volt.rundown import add_item, attach, open_show
from volt.store import Store
from volt.sequencing import LeaseBusy, exclusive, record_lease_name

from tests.racekit import PgBase, SqliteBase, _cleared_span
from tests.test_rooms import obs

CONTROL = Path(__file__).resolve().parents[1]


def _statuses(store, show):
    return {r["id"]: r["status"] for r in store.conn.execute("SELECT id, status FROM item WHERE show_id = ?", (show,)).fetchall()}


class FakeCoreHttp:
    """A CoreVideo Pro core as an HTTP server: /invoke takes `delay` seconds, /state says the lower third is on air."""

    def __init__(self, delay=1.5):
        self.entered = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.entered.set()
                time.sleep(delay)
                self._reply({"ok": True})

            def do_GET(self):
                self._reply({"lowerThirdOnAir": True})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Node:
    """One `python -m volt.serve --port 0` process; its address is read from the line it prints at start."""

    def __init__(self, db, env_extra):
        env = dict(os.environ, VOLT_PYTHON_UNBUFFERED="1", **env_extra)
        for name in ("VOLT_TOKEN", "VOLT_DB", "VOLT_LOG_DIR", "VOLT_TEST_PG_DSN"):
            env.pop(name, None)
        env.update(env_extra)
        self.proc = subprocess.Popen([sys.executable, "-u", "-m", "volt.serve", "--port", "0", "--db", db],
                                     cwd=CONTROL, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.port = None
        self.lines = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.lines.append(line)
            found = re.search(r"http://127\.0\.0\.1:(\d+)", line)
            if found:
                self.port = int(found.group(1))
                break
        if self.port is None:
            self.stop()
            raise AssertionError("serve did not start: " + "".join(self.lines))
        threading.Thread(target=lambda: [None for _ in self.proc.stdout], daemon=True).start()   # drain so it never blocks on a full pipe

    def post(self, path, body=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("POST", path, json.dumps(body or {}), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)


class MultiServe:
    def _show(self, n_items=1, cue=False):
        store = self.new_store()
        asset, span = _cleared_span(store)
        show = open_show(store, "Final Four")
        ids = []
        for n in range(n_items):
            item = add_item(store, show, f"item-{n}")
            attach(store, item, span)
            if cue:
                add_cue(store, item, "corevideo.pro", "overlay.take", "on")
            ids.append(item)
        return store, show, ids

    # ---- _SEQ is gone and nothing needed it ----------------------------------------------------

    def test_serve_has_no_seq(self):
        self.assertFalse(hasattr(serve, "_SEQ"))
        source = Path(serve.__file__).read_text(encoding="utf-8")
        self.assertNotIn("with _SEQ", source)
        self.assertNotIn("_REC", source)

    def test_go_skip_kill_outcomes_hold_their_invariants_without_seq(self):
        """Threads on separate connections hammer go, skip and kill with no process lock: no item airs twice, at most one is on air,
        and every refusal is a clean 409."""
        for round_ in range(3):
            store, show, ids = self._show(n_items=6)
            wins = []
            errors = []
            gate = threading.Barrier(8)

            def worker(kind, arg):
                st = self._open()       # opened and closed on this thread: a sqlite connection belongs to its thread
                try:
                    gate.wait(30)
                    if kind == "go":
                        out = serve.act_go(st, show, {})
                        wins.append(out["target"])
                    elif kind == "skip":
                        serve.act_skip(st, arg, {})
                    else:
                        serve.act_kill(st, arg, {})
                except serve.ApiError as exc:
                    if exc.status != 409:
                        errors.append(exc)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    st.close()

            jobs = [("go", None)] * 4 + [("skip", ids[4]), ("skip", ids[5]), ("kill", ids[3]), ("kill", ids[2])]
            threads = [threading.Thread(target=worker, args=j) for j in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
            self.assertFalse(any(t.is_alive() for t in threads), "a worker is stuck: a deadlock")
            self.assertEqual(errors, [])
            status = _statuses(store, show)
            self.assertLessEqual(list(status.values()).count("on air"), 1)
            aired = store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ? AND as_run_in IS NOT NULL", (show,)).fetchone()[0]
            self.assertEqual(aired, len([w for w in wins if w]), "an item aired twice or an air was lost")
            self.assertEqual(len(set(w for w in wins if w)), len([w for w in wins if w]), "two goes took the same item")

    # ---- the record lease ---------------------------------------------------------------------

    def test_record_start_second_caller_gets_record_busy(self):
        store = self.new_store()
        started = threading.Event()
        release = threading.Event()

        class Room(FakeRoom):
            def invoke(self, action, args):
                started.set()
                release.wait(30)
                super().invoke(action, args)

        room = Room([obs("idle")])
        first = {}

        def run_first():
            other = self._open()
            try:
                first["out"] = serve.act_record_start(other, None, {"owner": "a"})
            except BaseException as exc:  # noqa: BLE001
                first["err"] = exc
            finally:
                other.close()

        saved, saved_poll = serve.ROOM, serve.POLL
        serve.ROOM, serve.POLL = room, {"attempts": 1, "interval": 0, "sleep": lambda _: None}
        try:
            t = threading.Thread(target=run_first, daemon=True)
            t.start()
            self.assertTrue(started.wait(30), "the first record start never reached the room")
            with self.assertRaises(serve.ApiError) as raised:
                serve.act_record_start(store, None, {"owner": "b"})
            self.assertEqual((raised.exception.status, raised.exception.code), (409, "record_busy"))
            release.set()
            t.join(60)
        finally:
            release.set()
            serve.ROOM, serve.POLL = saved, saved_poll

    def test_record_lease_is_per_room_key(self):
        a, b = self.new_store(), self.new_store()
        with exclusive(a, record_lease_name("room-1"), holder="a"):
            with exclusive(b, record_lease_name("room-2"), holder="b"):
                with self.assertRaises(LeaseBusy):
                    with exclusive(b, record_lease_name("room-1"), holder="b"):
                        pass

    # ---- two real processes -------------------------------------------------------------------

    def _db_arg(self):
        raise NotImplementedError

    def _open(self):
        raise NotImplementedError

    def test_two_serve_subprocesses_one_take(self):
        store, show, (item,) = self._show(n_items=1, cue=True)
        core = FakeCoreHttp(delay=1.5)
        self.addCleanup(core.close)
        env = {"VOLT_ROOM_URL": core.url, "VOLT_MIN_FREE_MB": "1"}
        a = Node(self._db_arg(), env)
        self.addCleanup(a.stop)
        b = Node(self._db_arg(), env)
        self.addCleanup(b.stop)
        results = {}

        def first():
            results["a"] = a.post(f"/api/shows/{show}/take")

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(core.entered.wait(30), "the first take never reached the room")
        results["b"] = b.post(f"/api/shows/{show}/take")       # the other node, while the first waits on the room
        t.join(60)
        self.assertEqual(results["a"][0], 200, results["a"])
        self.assertEqual(results["b"][0], 409, results["b"])
        self.assertEqual(results["b"][1]["error"]["code"], "taking")
        self.assertEqual(_statuses(store, show)[item], "on air")


class SqliteMultiServe(MultiServe, SqliteBase):
    def _db_arg(self):
        return self.db

    def _open(self):
        st = Store(self.db)
        st.conn.execute("PRAGMA busy_timeout=20000")
        return st


class PostgresMultiServe(MultiServe, PgBase):
    def _db_arg(self):
        return self.dsn

    def _open(self):
        return Store(self.dsn)


if __name__ == "__main__":
    unittest.main()
