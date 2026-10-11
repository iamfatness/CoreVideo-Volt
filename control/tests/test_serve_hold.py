import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.test_playout import Clock, _show
from volt import serve
from volt.playout import hold, release
from volt.store import Store


class RundownHold(unittest.TestCase):
    def test_rundown_json_reports_the_hold_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "volt.db")
            store = Store(path)
            show, _ids = _show(store)
            old, serve.DB_PATH = serve.DB_PATH, path
            srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_port}/rundown?show={show}"
            try:
                body = json.load(urllib.request.urlopen(url))
                self.assertIsNone(body["hold"])
                self.assertEqual(len(body["items"]), 3)
                hold(store, show, "tech", Clock())
                body = json.load(urllib.request.urlopen(url))
                self.assertEqual(body["hold"], {"reason": "tech", "heldAt": "2026-10-10T20:00:01Z"})
                release(store, show)
                self.assertIsNone(json.load(urllib.request.urlopen(url))["hold"])
            finally:
                srv.shutdown()
                srv.server_close()
                serve.DB_PATH = old
                store.close()


if __name__ == "__main__":
    unittest.main()
