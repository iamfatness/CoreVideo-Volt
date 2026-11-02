"""Shared setup for the story tests (save, status, float): a real HTTP server over a SQLite file, and the same
classes again over live Postgres when VOLT_TEST_PG_DSN is set. Fakes only; nothing here touches the network."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.pgutil import PgCase
from volt import serve, users
from volt.rundown import attach
from volt.library import index_words
from volt.store import Store


class Behaviour:
    """Provides self.open_store() and self.target (what serve.DB_PATH points at)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM = self.target, None, self.dir / "out", None
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, token=None, headers=None):
        h = dict(headers or {})
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=60)
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
        finally:
            conn.close()

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def ok(self, path, body=None, **kw):
        s, p = self.post(path, body, **kw)
        self.assertEqual((s, p.get("ok")), (200, True), p)
        return p

    def refused(self, status, code, path, body=None, **kw):
        s, p = self.post(path, body, **kw)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        self.assertTrue(p["error"]["reason"])
        return p

    def new_show(self, title="Friday"):
        return self.ok("/api/shows", {"title": title})["show"]["id"]

    def new_item(self, show, slug="story", script=""):
        return self.ok(f"/api/shows/{show}/items", {"slug": slug, "script": script})["item"]

    def row(self, item):
        return self.store.conn.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()

    def status(self, item):
        return self.row(item)["status"]

    def revs(self, item):
        return [r["rev"] for r in self.store.conn.execute(
            "SELECT rev FROM story_rev WHERE item_id = ? ORDER BY rev", (item,))]

    def events(self, item):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM story_event WHERE item_id = ? ORDER BY at, id", (item,))]

    def span(self):
        a = self.store.mint_asset(owner="desk")
        self.store.set_cleared(a.id, True)
        index_words(self.store, a.id, [("w", "01:00:00:00", "01:00:01:00")])
        return self.store.conn.execute("SELECT id FROM span WHERE asset_id = ?", (a.id,)).fetchone()["id"]

    def with_media(self, *items):
        for item in items:
            attach(self.store, item, self.span())

    def user(self, name, role="operator"):
        """A per-user token (the audit actor and the approver identity come from it)."""
        _, token = users.add(self.store, name, role)
        return token


def build(namespace, *bases):
    """Define Sqlite<Name> and Postgres<Name> test classes for each behaviour class."""

    class _Sqlite:
        def open_store(self):
            self.target = str(self.dir / "v.db")
            return Store(self.target)

    class _Pg:
        def open_store(self):
            self.target = self.dsn
            return self.store()

    for base in bases:
        namespace[f"Sqlite{base.__name__}"] = type(f"Sqlite{base.__name__}", (_Sqlite, base, unittest.TestCase), {})
        namespace[f"Postgres{base.__name__}"] = type(f"Postgres{base.__name__}", (_Pg, base, PgCase), {})
