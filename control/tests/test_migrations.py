"""Schema migrations: a database at any historical shape migrates forward to the current one, on both backends.

The shapes in historical_shapes.py are the real schemas from git history (the SQLite SCHEMA and schema.sql at
each commit that changed them), not hand-written approximations.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from volt import migrations, pg
from volt.store import Store

from tests.historical_shapes import SHAPES
from tests.pgutil import PgCase


def sqlite_columns(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}


class Registry(unittest.TestCase):
    def test_versions_are_one_to_n(self):
        self.assertEqual([m.version for m in migrations.MIGRATIONS],
                         list(range(1, len(migrations.MIGRATIONS) + 1)))

    def test_every_neutral_type_in_a_migration_is_known(self):
        for m in migrations.MIGRATIONS:
            for _table, _column, neutral in m.columns:
                self.assertIn(neutral, migrations._TYPES)

    def test_legacy_list_types_map_to_neutral_types(self):
        for _t, _c, neutral in migrations.legacy_columns():
            self.assertIn(neutral, migrations._TYPES)


class SqliteForward(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fresh = Store(":memory:")
        self.addCleanup(fresh.close)
        self.latest = sqlite_columns(fresh.conn)

    def old_db(self, ddl):
        path = str(Path(self.tmp.name) / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(ddl)
        conn.close()
        return path

    def test_every_historical_shape_migrates_to_the_current_columns(self):
        for name, commit, sqlite_ddl, _pg in SHAPES:
            with self.subTest(shape=name, commit=commit):
                store = Store(self.old_db(sqlite_ddl))
                try:
                    self.assertEqual(sqlite_columns(store.conn), self.latest)
                    versions = {r[0] for r in store.conn.execute("SELECT version FROM schema_version")}
                    self.assertEqual(versions, set(range(1, migrations.LATEST + 1)))
                finally:
                    store.close()
                Path(self.tmp.name, "old.db").unlink()

    def test_existing_rows_survive_the_migration(self):
        _name, _c, ddl, _pg = SHAPES[0]
        path = self.old_db(ddl)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Old show', 'then')")
        conn.execute("INSERT INTO item (id, show_id, position, slug, status, created_at) "
                     "VALUES ('i1', 's1', 1, 'open', 'aired', 'then')")
        conn.execute("INSERT INTO asset (id, owner, retention_class, created_at) VALUES ('a1', 'desk', 'show', 'then')")
        conn.execute("INSERT INTO job (id, asset_id, type, idempotency_key, status, created_at, updated_at) "
                     "VALUES ('j1', 'a1', 'proxy', 'k', 'queued', 'then', 'then')")
        conn.commit()
        conn.close()
        store = Store(path)
        try:
            item = store.conn.execute("SELECT slug, status, as_run_in FROM item WHERE id = 'i1'").fetchone()
            self.assertEqual((item["slug"], item["status"], item["as_run_in"]), ("open", "aired", None))
            job = store.conn.execute("SELECT status, claimed_by, run_after FROM job WHERE id = 'j1'").fetchone()
            self.assertEqual((job["status"], job["claimed_by"], job["run_after"]), ("queued", None, None))
            self.assertEqual(store.get_job("j1").status, "queued")
        finally:
            store.close()

    def test_opening_again_changes_nothing(self):
        path = self.old_db(SHAPES[0][2])
        Store(path).close()
        store = Store(path)
        try:
            rows = store.conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version").fetchall()
            self.assertEqual([r[0] for r in rows], list(range(1, migrations.LATEST + 1)))
            first = [tuple(r) for r in rows]
        finally:
            store.close()
        store = Store(path)
        try:
            rows = store.conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version").fetchall()
            self.assertEqual([tuple(r) for r in rows], first)
        finally:
            store.close()

    def test_a_database_with_no_version_table_is_taken_to_the_latest(self):
        # The shape every deployed database had before schema_version existed: current columns, no record.
        path = self.old_db(SHAPES[-1][2])
        store = Store(path)
        try:
            self.assertEqual(sqlite_columns(store.conn), self.latest)
            self.assertEqual(migrations.current_version(migrations.SqliteBackend(store.conn)), migrations.LATEST)
        finally:
            store.close()

    def test_a_new_numbered_migration_runs_once_and_is_recorded(self):
        path = str(Path(self.tmp.name) / "m.db")
        Store(path).close()
        extra = migrations.Migration(migrations.LATEST + 1, "asset note", columns=(("asset", "note", "TEXT"),))
        with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (extra,)):
            store = Store(path)
            try:
                self.assertIn("note", sqlite_columns(store.conn)["asset"])
                self.assertEqual(migrations.current_version(migrations.SqliteBackend(store.conn)), extra.version)
            finally:
                store.close()

    def test_a_legacy_list_addition_still_lands_on_an_already_versioned_database(self):
        # The old way of adding a column: append to pg._ADDED_COLUMNS. It must keep working.
        path = str(Path(self.tmp.name) / "l.db")
        Store(path).close()
        with mock.patch.object(pg, "_ADDED_COLUMNS", pg._ADDED_COLUMNS + (("asset", "storage_key", "TEXT"),
                                                                        ("job", "scheduled_at", "TIMESTAMPTZ"))):
            store = Store(path)
            try:
                cols = sqlite_columns(store.conn)
                self.assertIn("storage_key", cols["asset"])
                self.assertIn("scheduled_at", cols["job"])
            finally:
                store.close()

    def test_a_failing_migration_rolls_back_and_is_not_recorded(self):
        path = str(Path(self.tmp.name) / "f.db")
        Store(path).close()
        bad = migrations.Migration(migrations.LATEST + 1, "broken",
                                   columns=(("asset", "half_done", "TEXT"),),
                                   sqlite=("THIS IS NOT SQL",))
        with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (bad,)):
            with self.assertRaises(sqlite3.Error):
                Store(path)
        store = Store(path)
        try:
            self.assertNotIn("half_done", sqlite_columns(store.conn)["asset"])
            self.assertEqual(migrations.current_version(migrations.SqliteBackend(store.conn)), migrations.LATEST)
        finally:
            store.close()

    def test_two_processes_starting_on_an_old_file_do_not_collide(self):
        import threading

        path = self.old_db(SHAPES[0][2])
        errors = []

        def work():
            try:
                Store(path).close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])


class PostgresForward(PgCase):
    def pg_columns(self, conn):
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()").fetchall()
        out = {}
        for table, column in rows:
            out.setdefault(table, set()).add(column)
        return out

    def test_every_historical_shape_migrates_to_the_current_columns(self):
        fresh = self.pg_columns(self.store().conn)
        for name, commit, _sqlite, pg_ddl in SHAPES:
            with self.subTest(shape=name, commit=commit):
                admin = self.admin()
                admin.execute("DROP SCHEMA IF EXISTS " + self.schema + " CASCADE")
                admin.execute("CREATE SCHEMA " + self.schema)
                admin.execute(pg_ddl)
                store = Store(self.dsn)
                try:
                    self.assertEqual(self.pg_columns(store.conn), fresh)
                    versions = {r[0] for r in store.conn.execute("SELECT version FROM schema_version")}
                    self.assertEqual(versions, set(range(1, migrations.LATEST + 1)))
                finally:
                    store.close()

    def short_lock_dsn(self):
        from urllib.parse import quote
        path = quote("-csearch_path=" + self.schema)
        return self.dsn.replace(path, quote("-csearch_path=" + self.schema + " -clock_timeout=1500"))

    def test_a_steady_state_start_takes_no_table_lock(self):
        # The old bootstrap ran ALTER TABLE ... ADD COLUMN IF NOT EXISTS on every start. That takes an
        # exclusive table lock even when the column exists, so it queued behind any open read.
        import psycopg

        self.store()
        reader = self.store(init=False)
        with reader.conn.transaction():  # a slow request holding its read transaction open
            reader.conn.execute("SELECT 1 FROM job LIMIT 1")

            def old_style_add(backend, table, column, neutral):
                backend.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {backend.type_of(neutral)}")

            with mock.patch.object(migrations.PostgresBackend, "columns", return_value=set()),                     mock.patch.object(migrations._Backend, "add_column", old_style_add):
                with self.assertRaises(psycopg.errors.LockNotAvailable):  # the setup does catch the old way
                    Store(self.short_lock_dsn())
            try:
                Store(self.short_lock_dsn()).close()
            except psycopg.errors.LockNotAvailable:
                self.fail("bootstrap waited on a table lock")

    def test_a_new_numbered_migration_runs_once_on_postgres(self):
        self.store()
        extra = migrations.Migration(migrations.LATEST + 1, "asset note", columns=(("asset", "note", "TEXT"),))
        with mock.patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS + (extra,)):
            store = self.store()
            self.assertIn("note", self.pg_columns(store.conn)["asset"])
            self.assertEqual(
                [r[0] for r in store.conn.execute("SELECT version FROM schema_version ORDER BY version")],
                list(range(1, extra.version + 1)))
            self.store()  # again: no duplicate-version error


V8 = next(shape for shape in SHAPES if shape[0] == "v8_cue_fail")
NEW_SPAN_COLUMNS = {"label", "origin", "run_id", "frame_in", "frame_out"}


def _seed_v8(execute):
    """Rows as a version-8 database would hold them: a transcript word with no frame columns, an essence."""
    execute("INSERT INTO asset (id, owner, retention_class, created_at) VALUES ('a1', 'desk', 'show', '2026-01-01T00:00:00Z')")
    execute("INSERT INTO essence (id, asset_id, role, location, frame_rate, timecode_start, created_at) "
            "VALUES ('e1', 'a1', 'hi-res', 'x.mxf', '25', '01:00:00:00', '2026-01-01T00:00:00Z')")
    execute("INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at) "
            "VALUES ('w1', 'a1', 'transcript', '01:00:00:00', '01:00:00:10', 'hello', '2026-01-01T00:00:00Z')")


class Version8ToNine(unittest.TestCase):
    """The shape every deployed database has today (store.SCHEMA at version 8, versions 1-8 recorded) takes
    exactly migration 9 and keeps its rows."""

    def test_sqlite_version_8_runs_only_migration_9(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v8.db")
            conn = sqlite3.connect(path)
            conn.executescript(V8[2])
            backend = migrations.SqliteBackend(conn)
            backend.ensure_version_table()
            for m in migrations.MIGRATIONS[:8]:
                backend.record(m)
            _seed_v8(conn.execute)
            conn.commit()
            before = conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version").fetchall()
            self.assertEqual(NEW_SPAN_COLUMNS & sqlite_columns(conn)["span"], set())
            conn.close()
            store = Store(path)
            try:
                cols = sqlite_columns(store.conn)
                self.assertTrue(NEW_SPAN_COLUMNS <= cols["span"])
                self.assertIn("duration_frames", cols["essence"])
                self.assertIn("shot_run", cols)
                self.assertEqual(
                    [tuple(r) for r in store.conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version")][:8],
                    [tuple(r) for r in before])
                self.assertEqual([r[0] for r in store.conn.execute("SELECT version FROM schema_version ORDER BY version")],
                                 list(range(1, migrations.LATEST + 1)))
                index = store.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'span_asset_kind_frame'").fetchone()
                self.assertIn("asset_id, kind, frame_in", index[0])
                row = store.conn.execute("SELECT text, label, origin, run_id, frame_in, frame_out FROM span").fetchone()
                self.assertEqual(tuple(row), ("hello", None, None, None, None, None))
                self.assertIsNone(store.conn.execute("SELECT duration_frames FROM essence").fetchone()[0])
                from volt import shots
                self.assertEqual(shots.pending_frames(store, "a1"), 1)
                shots.backfill_frames(store, "a1")
                self.assertEqual(tuple(store.conn.execute("SELECT frame_in, frame_out FROM span").fetchone()), (90000, 90010))
            finally:
                store.close()


class Version8ToNinePostgres(PgCase):
    def test_postgres_version_8_runs_only_migration_9(self):
        admin = self.admin()
        admin.execute(V8[3])
        backend = migrations.PostgresBackend(admin)
        backend.ensure_version_table()
        for m in migrations.MIGRATIONS[:8]:
            backend.record(m)
        _seed_v8(admin.execute)
        store = Store(self.dsn)
        self.stores.append(store)
        cols = PostgresForward.pg_columns(None, store.conn)
        self.assertTrue(NEW_SPAN_COLUMNS <= cols["span"])
        self.assertIn("duration_frames", cols["essence"])
        self.assertIn("shot_run", cols)
        self.assertEqual([r[0] for r in store.conn.execute("SELECT version FROM schema_version ORDER BY version")],
                         list(range(1, migrations.LATEST + 1)))
        index = store.conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'span_asset_kind_frame'").fetchone()
        self.assertIn("(asset_id, kind, frame_in)", index[0])
        row = store.conn.execute("SELECT text, label, origin, run_id, frame_in, frame_out FROM span").fetchone()
        self.assertEqual(tuple(row), ("hello", None, None, None, None, None))
        from volt import shots
        self.assertEqual(shots.pending_frames(store, "a1"), 1)
        shots.backfill_frames(store, "a1")
        self.assertEqual(tuple(store.conn.execute("SELECT frame_in, frame_out FROM span").fetchone()), (90000, 90010))
        self.stores.pop().close()
        again = self.store()  # opening again applies nothing and does not fail
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0], migrations.LATEST)


if __name__ == "__main__":
    unittest.main()
