"""Slot length: show.slot_seconds, its route, and its place in the /rundown and /api/state payloads."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.historical_shapes import SHAPES
from tests.test_operator_api import Api
from tests.test_take import _show
from volt import migrations
from volt.store import Store


class SlotRoute(Api):
    def setUp(self):
        super().setUp()
        self.show, _items = _show(self.store, [[], []])

    def slot(self, body):
        return self.call("POST", f"/api/shows/{self.show}/slot", body)

    def test_a_new_show_has_no_slot(self):
        status, state, _ = self.call("GET", f"/api/state?show={self.show}")
        self.assertEqual(status, 200)
        self.assertIsNone(state["rundown"]["slotSeconds"])

    def test_set_read_back_and_clear(self):
        status, out, _ = self.slot({"seconds": 900})
        self.assertEqual((status, out["ok"], out["rundown"]["slotSeconds"]), (200, True, 900))
        _, state, _ = self.call("GET", f"/api/state?show={self.show}")
        self.assertEqual(state["rundown"]["slotSeconds"], 900)
        status, out, _ = self.slot({"seconds": None})
        self.assertEqual((status, out["rundown"]["slotSeconds"]), (200, None))

    def test_bad_values_are_refused_with_a_code(self):
        for bad in (0, -5, 86400, 12.5, "900", True, [], {}):
            status, out, _ = self.slot({"seconds": bad})
            self.assertEqual(status, 422, bad)
            self.assertEqual(out["error"]["code"], "bad_input", bad)
        status, out, _ = self.slot({})
        self.assertEqual(status, 422)
        status, out, _ = self.slot({"seconds": 60, "extra": 1})
        self.assertEqual(status, 422)
        for ok in (1, 86399):
            self.assertEqual(self.slot({"seconds": ok})[0], 200, ok)

    def test_unknown_show_is_404(self):
        status, out, _ = self.call("POST", "/api/shows/nope/slot", {"seconds": 60})
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))

    def test_the_write_is_audited_and_an_operator_may_make_it(self):
        self.slot({"seconds": 600})
        rows = self.store.list_audit(self.show)
        self.assertEqual([(r["action"], r["target_kind"]) for r in rows], [("slot", "show")])

    def test_the_route_is_classified(self):
        from volt import serve
        self.assertEqual(serve.ROUTE_ROLES["act_show_slot"], "operator")


class SlotMigration(unittest.TestCase):
    def test_every_historical_shape_gets_the_column(self):
        for name, _commit, ddl, _pg in SHAPES:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "old.db"
                conn = sqlite3.connect(str(path))
                conn.executescript(ddl)
                conn.commit()
                conn.close()
                store = Store(str(path))
                cols = {r[1] for r in store.conn.execute("PRAGMA table_info(show)")}
                self.assertIn("slot_seconds", cols, name)
                self.assertEqual(migrations.current_version(migrations.SqliteBackend(store.conn)), migrations.LATEST)
                store.close()

    def test_head_is_at_least_18(self):
        self.assertGreaterEqual(migrations.LATEST, 18)


if __name__ == "__main__":
    unittest.main()
