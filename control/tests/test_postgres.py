"""Postgres store. Live tests run only when VOLT_TEST_PG_DSN is set (needs psycopg).

Each test gets its own throwaway schema, dropped afterward, so a shared
database is safe. The adapter's pure helpers are tested everywhere.
"""

import os
import re
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

from volt import pg
from volt import store as store_module
from volt.library import index_words, search
from volt.playout import go, on_air
from volt.rundown import add_item, attach, open_show
from volt.session import list_assets, rundown
from volt.store import Store

DSN = os.environ.get("VOLT_TEST_PG_DSN")


class Adapter(unittest.TestCase):
    def test_placeholders_become_percent_s(self):
        self.assertEqual(pg.translate("SELECT 1 WHERE a = ? AND b = ?"), "SELECT 1 WHERE a = %s AND b = %s")

    def test_question_mark_in_a_literal_is_left_alone(self):
        self.assertEqual(pg.translate("SELECT '?' WHERE a = ?"), "SELECT '?' WHERE a = %s")

    def test_literal_percent_is_escaped(self):
        self.assertEqual(pg.translate("WHERE text LIKE '50%' AND a = ?"), "WHERE text LIKE '50%%' AND a = %s")

    def test_dsn_detection(self):
        self.assertTrue(pg.is_dsn("postgresql://u@h/db"))
        self.assertTrue(pg.is_dsn("postgres://u@h/db"))
        self.assertFalse(pg.is_dsn(":memory:"))
        self.assertFalse(pg.is_dsn("volt.db"))

    def test_sqlite_path_never_imports_psycopg(self):
        import sys
        had = "psycopg" in sys.modules
        Store().close()
        self.assertEqual("psycopg" in sys.modules, had)


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class LivePostgres(unittest.TestCase):
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

        for s in self.stores:
            s.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def store(self, **kw):
        s = Store(self.dsn, **kw)
        self.stores.append(s)
        return s

    def test_schema_applies_to_a_fresh_database_and_matches_sqlite(self):
        store = self.store()
        rows = store.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = ? ORDER BY table_name, ordinal_position",
            (self.schema,),
        ).fetchall()
        live = {}
        for table, column in rows:
            live.setdefault(table, []).append(column)
        lite = {
            name: [ln.split()[0] for ln in body.splitlines()
                   if ln.strip() and not ln.strip().upper().startswith(("UNIQUE", "PRIMARY"))]
            for name, body in re.findall(
                r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", store_module.SCHEMA, re.S)
        }
        self.assertEqual(live, lite)

    def test_an_older_database_gains_the_columns_added_since(self):
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True) as old:
            old.execute("CREATE TABLE show (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        store = self.store()
        cols = {r["column_name"] for r in store.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'show'")}
        self.assertTrue({"hold_reason", "held_at"} <= cols)

    def test_schema_is_idempotent(self):
        self.store()
        again = self.store()
        again.mint_asset(owner="desk")

    def test_concurrent_bootstrap_does_not_collide(self):
        errors = []

        def work():
            try:
                self.store()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])

    def test_asset_roundtrip_rows_by_name_and_index_and_iso_timestamps(self):
        store = self.store()
        asset = store.mint_asset(owner="desk", show_name="final-four", camera="iso-3")
        self.assertIs(asset.legal_hold, False)
        self.assertIs(asset.cleared, False)
        store.set_hold(asset.id, True)
        store.set_cleared(asset.id, True)
        got = store.get_asset(asset.id)
        self.assertTrue(got.legal_hold and got.cleared)
        row = store.conn.execute("SELECT id, created_at FROM asset").fetchone()
        self.assertEqual(row["id"], row[0])
        self.assertRegex(row["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_duplicate_source_key_raises_the_backend_integrity_error(self):
        store = self.store()
        store.mint_source("rec:1", owner="desk")
        with self.assertRaises(store.IntegrityError):
            store.mint_asset(owner="desk", source_key="rec:1")
        store.conn.rollback()

    def test_losing_a_mint_race_returns_the_winner(self):
        store = self.store()
        winner = store.mint_source("rec:1", owner="desk")
        original = store.find_by_source
        seen = []

        def stale(key):
            seen.append(key)
            return None if len(seen) == 1 else original(key)

        store.find_by_source = stale
        self.assertEqual(store.mint_source("rec:1", owner="desk").id, winner.id)

    def test_failed_job_replays_without_minting(self):
        store = self.store()
        asset = store.mint_asset(owner="desk")
        job = store.enqueue(asset.id, "proxy", "proxy-v1")
        store.fail_job(job.id, "encoder died")
        again = store.enqueue(asset.id, "proxy", "proxy-v1")
        self.assertEqual((again.id, again.status, again.attempt), (job.id, "queued", 2))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_open_essence_wraps_and_session_reports_it(self):
        store = self.store()
        asset = store.mint_asset(owner="desk", camera="iso-3")
        essence = store.add_essence(asset.id, role="hi-res", location="media/a.mxf", open_file=True)
        self.assertTrue(list_assets(store)[0].open)
        store.wrap_essence(essence, "abc")
        self.assertFalse(list_assets(store)[0].open)

    def test_search_rundown_and_playout_with_as_run(self):
        store = self.store()
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00"),
                                      ("block", "01:12:10:00", "01:12:11:00")])
        self.assertEqual(len(search(store, "STE")), 1)
        show = open_show(store, "Final Four")
        ids = []
        for n, span in enumerate(store.conn.execute("SELECT id FROM span ORDER BY tc_in").fetchall()):
            item = add_item(store, show, f"item-{n}")
            attach(store, item, span["id"])
            ids.append(item)
        ticks = iter(f"2026-10-10T20:00:0{n}Z" for n in range(1, 9))
        go(store, show, lambda: next(ticks))
        go(store, show, lambda: next(ticks))
        self.assertEqual(on_air(store, show), ids[1])
        rows = rundown(store, show)
        self.assertEqual(rows[0].status, "aired")
        self.assertEqual(rows[0].as_run_in, "2026-10-10T20:00:01Z")
        self.assertEqual(rows[0].as_run_out, "2026-10-10T20:00:02Z")

    def test_per_request_connections_see_committed_data(self):
        self.store().mint_asset(owner="desk", show_name="final-four")
        request = Store(self.dsn, init=False)
        self.stores.append(request)
        self.assertEqual([r.show_name for r in list_assets(request)], ["final-four"])


if __name__ == "__main__":
    unittest.main()
