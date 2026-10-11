"""Shared setup for live Postgres tests: a throwaway schema per test, so one database serves every test."""

import os
import unittest
import uuid
from urllib.parse import quote

from volt.store import Store

DSN = os.environ.get("VOLT_TEST_PG_DSN")


def have_pool() -> bool:
    try:
        import psycopg_pool  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class PgCase(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.schema = "volt_t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        sep = "&" if "?" in DSN else "?"
        self.dsn = f"{DSN}{sep}options={quote('-csearch_path=' + self.schema)}"
        self.stores = []
        self.addCleanup(self._drop)

    def _drop(self):
        import psycopg

        from volt import pg

        pg.close_pools()
        for s in self.stores:
            try:
                s.close()
            except Exception:  # noqa: BLE001 - a store a test already broke
                pass
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def store(self, **kw):
        s = Store(self.dsn, **kw)
        self.stores.append(s)
        return s

    def admin(self):
        """A raw autocommit connection to the same schema, for DDL and for looking at the server."""
        import psycopg

        conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(conn.close)
        return conn
