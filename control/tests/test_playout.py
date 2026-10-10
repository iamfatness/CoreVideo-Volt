import tempfile
import unittest
from pathlib import Path

from volt.library import index_words
from volt.playout import PlayoutError, go, on_air, skip
from volt.rundown import add_item, attach, items, kill, open_show
from volt.store import NotClearedError, Store


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-10-10T20:00:{self.n:02d}Z"


def _show(store, count=3, cleared=True):
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, cleared)
    show = open_show(store, "Final Four")
    ids = []
    index_words(store, asset.id, [(f"w{n}", f"01:00:0{n}:00", f"01:00:0{n}:08") for n in range(count)])
    for n, span in enumerate(store.conn.execute("SELECT id FROM span ORDER BY tc_in").fetchall()):
        item = add_item(store, show, f"item-{n}")
        attach(store, item, span["id"])
        ids.append(item)
    return show, ids


def _row(store, item_id):
    return store.conn.execute("SELECT status, as_run_in, as_run_out FROM item WHERE id = ?", (item_id,)).fetchone()


class Playout(unittest.TestCase):
    def test_go_walks_the_order_and_writes_as_run_from_the_clock(self):
        store = Store()
        show, (a, b, c) = _show(store)
        clock = Clock()
        self.assertEqual(go(store, show, clock), a)
        self.assertEqual(go(store, show, clock), b)
        ra, rb = _row(store, a), _row(store, b)
        self.assertEqual(ra["status"], "aired")
        self.assertEqual(ra["as_run_in"], "2026-10-10T20:00:01Z")
        self.assertEqual(ra["as_run_out"], "2026-10-10T20:00:02Z")
        self.assertEqual(rb["status"], "on air")
        self.assertEqual(rb["as_run_in"], ra["as_run_out"])
        self.assertIsNone(rb["as_run_out"])

    def test_end_of_show_closes_the_last_item(self):
        store = Store()
        show, (a,) = _show(store, count=1)
        clock = Clock()
        go(store, show, clock)
        self.assertIsNone(go(store, show, clock))
        self.assertEqual(_row(store, a)["status"], "aired")
        self.assertIsNotNone(_row(store, a)["as_run_out"])
        self.assertIsNone(on_air(store, show))

    def test_killed_item_is_skipped_and_recorded_not_aired(self):
        store = Store()
        show, (a, b, c) = _show(store)
        kill(store, b)
        clock = Clock()
        go(store, show, clock)
        self.assertEqual(go(store, show, clock), c)
        rb = _row(store, b)
        self.assertEqual(rb["status"], "skipped")
        self.assertIsNone(rb["as_run_in"])

    def test_operator_skip_marks_a_ready_item(self):
        store = Store()
        show, (a, b, c) = _show(store)
        skip(store, b)
        self.assertEqual(_row(store, b)["status"], "skipped")
        go(store, show, Clock())
        self.assertEqual(go(store, show, Clock()), c)

    def test_cannot_skip_the_item_on_air(self):
        store = Store()
        show, (a, b, c) = _show(store)
        go(store, show, Clock())
        with self.assertRaises(PlayoutError):
            skip(store, a)

    def test_uncleared_next_item_changes_nothing(self):
        store = Store()
        show, (a, b, c) = _show(store)
        clock = Clock()
        go(store, show, clock)
        store.conn.execute("UPDATE asset SET cleared = 0")
        store.conn.commit()
        with self.assertRaises(NotClearedError):
            go(store, show, clock)
        self.assertEqual(_row(store, a)["status"], "on air")
        self.assertIsNone(_row(store, a)["as_run_out"])
        self.assertEqual(_row(store, b)["status"], "ready")

    def test_restart_resumes_the_on_air_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "volt.db")
            store = Store(path)
            show, (a, b, c) = _show(store)
            go(store, show, Clock())
            go(store, show, Clock())
            store.close()
            again = Store(path)
            self.assertEqual(on_air(again, show), b)
            self.assertIsNotNone(_row(again, b)["as_run_in"])
            self.assertEqual(go(again, show, Clock()), c)
            again.close()


class Migration(unittest.TestCase):
    def test_existing_db_without_as_run_columns_is_upgraded(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript("""
              CREATE TABLE item (id TEXT PRIMARY KEY, show_id TEXT NOT NULL, position INTEGER NOT NULL,
                slug TEXT NOT NULL, script TEXT, status TEXT NOT NULL, span_id TEXT, created_at TEXT NOT NULL);
              INSERT INTO item VALUES ('i1','s1',1,'x',NULL,'ready',NULL,'2026-01-01T00:00:00Z');
            """)
            conn.commit()
            conn.close()
            store = Store(path)
            row = store.conn.execute("SELECT as_run_in, as_run_out FROM item").fetchone()
            self.assertEqual((row[0], row[1]), (None, None))
            store.close()


if __name__ == "__main__":
    unittest.main()
