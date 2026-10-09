import re
import tempfile
import threading
import unittest
from pathlib import Path

from volt import store as store_module
from volt.store import Store


class MintIsAtomic(unittest.TestCase):
    def test_source_key_is_set_in_the_insert(self):
        store = Store()
        asset = store.mint_source("rec:1", owner="desk")
        self.assertEqual(asset.source_key, "rec:1")
        calls = []
        store.conn.set_trace_callback(calls.append)
        store.mint_source("rec:2", owner="desk")
        self.assertFalse([c for c in calls if c.lstrip().upper().startswith("UPDATE ASSET")])

    def test_losing_a_race_returns_the_winner(self):
        store = Store()
        winner = store.mint_source("rec:1", owner="desk")
        original = store.find_by_source
        seen = []

        def stale(key):
            seen.append(key)
            return None if len(seen) == 1 else original(key)

        store.find_by_source = stale
        again = store.mint_source("rec:1", owner="desk")
        self.assertEqual(again.id, winner.id)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)


class SharedAcrossThreads(unittest.TestCase):
    def test_store_file_usable_from_many_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "volt.db")
            Store(path).close()
            errors = []

            def work(n):
                try:
                    s = Store(path)
                    s.mint_source(f"rec:{n}", owner="desk")
                    s.close()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            check = Store(path)
            self.assertEqual(check.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 8)
            check.close()


class SchemaParity(unittest.TestCase):
    def test_postgres_schema_has_the_same_tables_and_columns(self):
        def parse(sql):
            out = {}
            for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);?", sql, re.S):
                cols = [ln.split()[0] for ln in body.splitlines()
                        if ln.strip() and not ln.strip().upper().startswith(("UNIQUE", "PRIMARY"))]
                out[name] = cols
            return out

        pg = parse((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
        lite = parse(store_module.SCHEMA.replace("\n);", "\n);\n"))
        self.assertEqual(pg, lite)


if __name__ == "__main__":
    unittest.main()
