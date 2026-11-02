"""The device-control migration: forward from every historical shape, on SQLite and on Postgres."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from volt import migrations
from volt.store import Store

from tests.historical_shapes import SHAPES
from tests.pgutil import PgCase

NEW_CUE_COLUMNS = {"offset_ms", "preroll_ms", "group_key", "due_at", "claimed_at", "preload_status", "preload_detail", "preloaded_at"}
NEW_TABLES = {"device_status", "device_event", "device_safety", "macro", "macro_step", "device_confirm"}


class SqliteForward(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def old_db(self, ddl):
        path = str(Path(self.tmp.name) / "old.db")
        Path(path).unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(ddl)
        conn.close()
        return path

    def test_device_migration_is_recorded(self):
        self.assertIn("device control", [m.name for m in migrations.MIGRATIONS])

    def test_forward_from_every_historical_shape_sqlite(self):
        for name, commit, ddl, _pg in SHAPES:
            with self.subTest(shape=name, commit=commit):
                store = Store(self.old_db(ddl))
                try:
                    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(cue)")}
                    self.assertTrue(NEW_CUE_COLUMNS <= cols, cols)
                    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    self.assertTrue(NEW_TABLES <= tables, NEW_TABLES - tables)
                finally:
                    store.close()

    def test_existing_cues_read_back_with_zero_offset_and_no_group(self):
        for name, commit, ddl, _pg in SHAPES:
            conn = sqlite3.connect(self.old_db(ddl))
            has_cue = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cue'").fetchone()
            if not has_cue:
                conn.close()
                continue
            with self.subTest(shape=name):
                conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Old', 'then')")
                conn.execute("INSERT INTO item (id, show_id, position, slug, status, created_at) VALUES ('i1', 's1', 1, 'x', 'ready', 'then')")
                conn.execute("INSERT INTO cue (id, item_id, position, room, command, payload, status, created_at) "
                             "VALUES ('c1', 'i1', 1, 'corevideo.pro', 'overlay.take', 'on', 'stored', 'then')")
                conn.commit()
                conn.close()
                store = Store(str(Path(self.tmp.name) / "old.db"))
                try:
                    r = store.conn.execute(
                        "SELECT COALESCE(offset_ms, 0) AS o, COALESCE(preroll_ms, 0) AS p, group_key, due_at, preload_status FROM cue WHERE id = 'c1'").fetchone()
                    self.assertEqual((r["o"], r["p"], r["group_key"], r["due_at"], r["preload_status"]), (0, 0, None, None, None))
                    from volt.cues import cues
                    self.assertEqual([c.id for c in cues(store, "i1")], ["c1"])
                finally:
                    store.close()


class PostgresForward(PgCase):
    def test_forward_from_every_historical_shape_postgres(self):
        for name, commit, _sqlite, pg_ddl in SHAPES:
            with self.subTest(shape=name, commit=commit):
                admin = self.admin()
                admin.execute("DROP SCHEMA IF EXISTS " + self.schema + " CASCADE")
                admin.execute("CREATE SCHEMA " + self.schema)
                admin.execute(pg_ddl)
                store = Store(self.dsn)
                try:
                    cols = {r["column_name"] for r in store.conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'cue'")}
                    self.assertTrue(NEW_CUE_COLUMNS <= cols)
                    tables = {r["table_name"] for r in store.conn.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
                    self.assertTrue(NEW_TABLES <= tables, NEW_TABLES - tables)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
