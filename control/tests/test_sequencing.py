"""Exclusive leases (volt/sequencing.py): one holder at a time across processes, expiry covers a crashed holder.
Each case runs on SQLite and, with VOLT_TEST_PG_DSN set, on live Postgres (tests/racekit.py)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from volt import migrations, users
from volt import store as store_mod
from volt.sequencing import LeaseBusy, exclusive, record_lease_name
from volt.store import Store

from tests.historical_shapes import SHAPES
from tests.pgutil import PgCase
from tests.racekit import PgBase, SqliteBase


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class LeaseTests:
    def test_second_holder_is_refused_while_lease_is_live(self):
        a, b = self.new_store(), self.new_store()
        clock = Clock()
        with exclusive(a, "record:r1", holder="node-a", now=clock):
            with self.assertRaises(LeaseBusy) as raised:
                with exclusive(b, "record:r1", holder="node-b", now=clock):
                    self.fail("the second holder got in")
        self.assertEqual(raised.exception.name, "record:r1")
        self.assertEqual(raised.exception.holder, "node-a")

    def test_other_names_do_not_contend(self):
        a, b = self.new_store(), self.new_store()
        with exclusive(a, "record:r1", holder="a"):
            with exclusive(b, "record:r2", holder="b"):
                pass

    def test_lease_expires_and_can_be_taken(self):
        a, b = self.new_store(), self.new_store()
        clock = Clock()
        cm = exclusive(a, "record:r1", ttl=120, holder="node-a", now=clock)
        cm.__enter__()                       # node-a "crashes": never leaves the block
        clock.t += 119
        with self.assertRaises(LeaseBusy):
            with exclusive(b, "record:r1", holder="node-b", now=clock):
                pass
        clock.t += 2
        with exclusive(b, "record:r1", holder="node-b", now=clock):
            row = b.conn.execute("SELECT holder FROM exclusive_lease WHERE name = ?", ("record:r1",)).fetchone()
            self.assertEqual(row["holder"], "node-b")

    def test_release_on_exception(self):
        a, b = self.new_store(), self.new_store()
        with self.assertRaises(ValueError):
            with exclusive(a, "record:r1", holder="a"):
                raise ValueError("boom")
        with exclusive(b, "record:r1", holder="b"):
            pass
        self.assertIsNone(a.conn.execute("SELECT 1 FROM exclusive_lease").fetchone())

    def test_same_holder_may_renew(self):
        a = self.new_store()
        clock = Clock()
        with exclusive(a, "record:r1", holder="same", now=clock):
            with exclusive(a, "record:r1", holder="same", now=clock):
                pass

    def test_release_does_not_free_a_lease_that_was_taken_over(self):
        """A holder whose lease expired and was taken must not delete the new holder's row on its way out."""
        a, b = self.new_store(), self.new_store()
        clock = Clock()
        with exclusive(a, "record:r1", ttl=10, holder="a", now=clock):
            clock.t += 11
            cm = exclusive(b, "record:r1", ttl=10, holder="b", now=clock)
            cm.__enter__()
        row = b.conn.execute("SELECT holder FROM exclusive_lease WHERE name = ?", ("record:r1",)).fetchone()
        self.assertEqual(row["holder"], "b")
        cm.__exit__(None, None, None)

    def test_record_lease_name(self):
        self.assertEqual(record_lease_name("room-1"), "record:room-1")

    def test_default_holder_names_the_process(self):
        a = self.new_store()
        with exclusive(a, "x"):
            row = a.conn.execute("SELECT holder FROM exclusive_lease").fetchone()
            self.assertTrue(row["holder"])


class SqliteLeaseTests(LeaseTests, SqliteBase):
    pass


class PostgresLeaseTests(LeaseTests, PgBase):
    pass


def _migration():
    return next(m for m in migrations.MIGRATIONS if m.name == "exclusive lease")


class MigrationTests(unittest.TestCase):
    def test_the_registry_names_the_lease_table(self):
        m = _migration()
        self.assertTrue(all("IF NOT EXISTS" in s for s in m.sqlite + m.postgres))
        self.assertLessEqual(m.version, migrations.LATEST)

    def test_a_database_without_the_table_gains_it_and_keeps_its_rows(self):
        m = _migration()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            store.conn.execute("DROP TABLE exclusive_lease")
            store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            store.conn.commit()
            store.close()
            store = Store(path)
            try:
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(exclusive_lease)")],
                                 ["name", "holder", "expires_at"])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
                with exclusive(store, "x", holder="h"):
                    pass
            finally:
                store.close()

    def test_the_oldest_historical_shape_gains_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(SHAPES[0][2])
            conn.close()
            store = Store(path)
            try:
                names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertIn("exclusive_lease", names)
            finally:
                store.close()

    def test_both_schemas_declare_it(self):
        root = Path(store_mod.__file__).parent
        self.assertIn("CREATE TABLE IF NOT EXISTS exclusive_lease", (root / "schema.sql").read_text(encoding="utf-8"))
        self.assertIn("CREATE TABLE IF NOT EXISTS exclusive_lease", store_mod.SCHEMA)


class MigrationPostgres(PgCase):
    def test_a_postgres_database_without_the_table_gains_it(self):
        m = _migration()
        store = self.store()
        store.conn.execute("DROP TABLE exclusive_lease")
        store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        rows = again.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'exclusive_lease' ORDER BY ordinal_position").fetchall()
        self.assertEqual([r[0] for r in rows], ["name", "holder", "expires_at"])


if __name__ == "__main__":
    unittest.main()
