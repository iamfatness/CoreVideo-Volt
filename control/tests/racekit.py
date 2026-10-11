"""Shared setup for the cross-process race tests: one set of tests, run on SQLite (a file shared by two
connections) and, when VOLT_TEST_PG_DSN is set, on live Postgres. See test_race_*.py."""

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt.store import Store

from tests.pgutil import PgCase


# ---- two backends, one set of tests --------------------------------------------------------------

class SqliteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "volt.db")
        self.opened = []
        self.addCleanup(self._close)

    def _close(self):
        for s in self.opened:
            try:
                s.close()
            except Exception:  # noqa: BLE001 - a store a test already broke or a thread already closed
                pass

    def new_store(self):
        s = Store(self.db)
        s.conn.execute("PRAGMA busy_timeout=20000")
        self.opened.append(s)
        return s

    def in_thread(self, fn, busy_ms=None):
        """fn(store) on its own connection in a thread, opened now and released by wait()/finish()."""
        def open_store():
            s = Store(self.db)
            if busy_ms is not None:
                s.conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            return s
        return Bg(open_store, fn)


class PgBase(PgCase):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def new_store(self):
        return self.store()

    def in_thread(self, fn, busy_ms=None):
        return Bg(lambda: Store(self.dsn), fn)


class Bg:
    """Run fn(store) in a thread, on a connection opened before anything is held (a second process is already
    running when the race starts). release() lets it go; wait() gives it a bounded chance to finish (it may be
    blocked on a lock, which is the point); finish() joins for real, with any error kept for the test to look at."""

    def __init__(self, open_store, fn):
        self.result = None
        self.error = None
        self.ready = threading.Event()
        self.gate = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(open_store, fn), daemon=True)
        self.thread.start()
        assert self.ready.wait(30), "second connection did not open"

    def _run(self, open_store, fn):
        store = None
        try:
            store = open_store()
            self.ready.set()
            self.gate.wait(60)
            self.result = fn(store)
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            self.ready.set()
        finally:
            if store is not None:
                store.close()

    def release(self):
        self.gate.set()

    def wait(self, timeout=1.0):
        self.release()
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def finish(self, timeout=30.0):
        self.release()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "second writer is stuck: a deadlock"
        return self.result


def _cleared_span(store):
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    span_id = store.add_span(asset.id, "transcript", "01:00:00:00", "01:00:02:00", text="steal")
    return asset, span_id
