import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_record import _clip
from volt.live import LiveError, finish_recording, start_recording
from volt.rooms import CoreVideoProRoom, FakeRoom, Observation
from volt.store import Store


def obs(state, session="s1", path=None, finalized=False, stale=False, health="healthy", error=None):
    return Observation(state, health, finalized, session, path, stale, error)


def _no_sleep(_):
    return None


class StartRecording(unittest.TestCase):
    def test_ack_alone_mints_nothing(self):
        store = Store()
        room = FakeRoom([obs("requested", path=None)] * 5)
        with self.assertRaises(LiveError):
            start_recording(store, room, owner="desk", attempts=3, sleep=_no_sleep)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 0)

    def test_mints_open_essence_once_producing_with_a_path(self):
        store = Store()
        room = FakeRoom([obs("requested"), obs("preparing"), obs("producing", path="C:/iso/a.mp4")])
        rec = start_recording(store, room, owner="desk", camera="iso-3", sleep=_no_sleep)
        self.assertTrue(rec.minted)
        row = store.conn.execute("SELECT location, open FROM essence WHERE asset_id = ?", (rec.asset_id,)).fetchone()
        self.assertEqual((row["location"], row["open"]), (str(Path("C:/iso/a.mp4")), 1))
        self.assertEqual(store.get_asset(rec.asset_id).source_key, "cv:s1")
        self.assertTrue(room.invoked("transport.record.set", [True]))

    def test_stale_or_unhealthy_producing_is_not_evidence(self):
        store = Store()
        for bad in (obs("producing", path="p", stale=True), obs("producing", path="p", health="failed")):
            with self.assertRaises(LiveError):
                start_recording(store, FakeRoom([bad] * 3), owner="desk", attempts=3, sleep=_no_sleep)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 0)

    def test_failed_or_unknown_state_fails_closed(self):
        store = Store()
        for state in ("failed", "interrupted", "warp-speed"):
            with self.assertRaises(LiveError):
                start_recording(store, FakeRoom([obs(state, path="p")]), owner="desk", sleep=_no_sleep)

    def test_restart_with_same_session_does_not_mint_twice(self):
        store = Store()
        script = [obs("producing", path="C:/iso/a.mp4")]
        first = start_recording(store, FakeRoom(script), owner="desk", sleep=_no_sleep)
        second = start_recording(store, FakeRoom(script), owner="desk", sleep=_no_sleep)
        self.assertFalse(second.minted)
        self.assertEqual(first.asset_id, second.asset_id)


class FinishRecording(unittest.TestCase):
    def _started(self, root):
        iso = root / "iso.mp4"
        _clip(iso)
        store = Store()
        room_script = [obs("producing", path=str(iso))]
        rec = start_recording(store, FakeRoom(room_script), owner="desk", sleep=_no_sleep)
        return store, rec, iso

    def test_wraps_only_after_completed_and_finalized(self):
        with TemporaryDirectory() as tmp:
            store, rec, iso = self._started(Path(tmp))
            room = FakeRoom([obs("stopping", path=str(iso)), obs("finalizing", path=str(iso)),
                             obs("completed", path=str(iso), finalized=True)])
            digest = finish_recording(store, room, rec.asset_id, sleep=_no_sleep)
            self.assertEqual(len(digest), 64)
            self.assertTrue(room.invoked("transport.record.set", [False]))
            self.assertEqual(store.conn.execute("SELECT open FROM essence WHERE role='hi-res'").fetchone()[0], 0)

    def test_completed_but_not_finalized_does_not_wrap(self):
        with TemporaryDirectory() as tmp:
            store, rec, iso = self._started(Path(tmp))
            room = FakeRoom([obs("completed", path=str(iso), finalized=False)])
            with self.assertRaises(LiveError):
                finish_recording(store, room, rec.asset_id, attempts=3, sleep=_no_sleep)
            self.assertEqual(store.conn.execute("SELECT open FROM essence WHERE role='hi-res'").fetchone()[0], 1)

    def test_failed_or_interrupted_never_wraps(self):
        for state in ("failed", "interrupted"):
            with TemporaryDirectory() as tmp:
                store, rec, iso = self._started(Path(tmp))
                room = FakeRoom([obs(state, path=str(iso), finalized=True)])
                with self.assertRaises(LiveError):
                    finish_recording(store, room, rec.asset_id, sleep=_no_sleep)
                self.assertEqual(store.conn.execute("SELECT open FROM essence WHERE role='hi-res'").fetchone()[0], 1)


class _Core(BaseHTTPRequestHandler):
    posted = []
    recording = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"available": True, "stale": False, "ageMs": 5,
                           "snapshot": {"recording": self.recording}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _Core.posted.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CoreVideoProWire(unittest.TestCase):
    def setUp(self):
        _Core.posted = []
        _Core.recording = {
            "artifactPath": "C:/iso/a.mp4",
            "lifecycle": {"sessionId": "s9", "desiredActive": True, "state": "producing",
                          "health": "healthy", "finalized": False},
        }
        self.srv = HTTPServer(("127.0.0.1", 0), _Core)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_invoke_shape_and_token(self):
        room = CoreVideoProRoom(self.base, token="t0k")
        room.invoke("transport.record.set", [True])
        path, auth, body = _Core.posted[0]
        self.assertEqual((path, auth, body), ("/invoke", "Bearer t0k", {"action": "transport.record.set", "args": [True]}))

    def test_observe_reads_lifecycle_and_artifact(self):
        o = CoreVideoProRoom(self.base).observe()
        self.assertEqual((o.state, o.session_id, o.artifact_path, o.finalized, o.stale), ("producing", "s9", "C:/iso/a.mp4", False, False))

    def test_missing_lifecycle_is_unknown_never_healthy(self):
        _Core.recording = {"status": "recording"}
        o = CoreVideoProRoom(self.base).observe()
        self.assertEqual(o.state, "unknown")
        self.assertFalse(o.usable())

    def test_unreachable_core_raises(self):
        with self.assertRaises(LiveError):
            CoreVideoProRoom("http://127.0.0.1:1", timeout=0.3).observe()


if __name__ == "__main__":
    unittest.main()
