import unittest

from volt.library import index_words
from volt.rundown import add_item, attach, items, kill, open_show, reorder, take
from volt.store import NotClearedError, Store


class RundownContract(unittest.TestCase):
    def test_item_points_at_a_span_and_reorder_does_not_copy_media(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        show_id = open_show(store, "Final Four")
        first = add_item(show_id=show_id, slug="Open", script="Welcome", store=store)
        second = add_item(store, show_id, "Steal")
        attach(store, second, span_id)
        reorder(store, show_id, [second, first])
        ordered = items(store, show_id)
        self.assertEqual(ordered[0].id, second)
        self.assertEqual(ordered[0].span_id, span_id)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 0)

    def test_kill_does_not_delete_the_asset_and_uncleared_cannot_air(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        index_words(store, asset.id, [("steal", "01:00:00:00", "01:00:00:08")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        show_id = open_show(store, "Final Four")
        item_id = add_item(store, show_id, "Steal")
        attach(store, item_id, span_id)
        with self.assertRaises(NotClearedError):
            take(store, item_id)
        store.set_cleared(asset.id, True)
        take(store, item_id)
        self.assertEqual(items(store, show_id)[0].status, "on air")
        kill(store, item_id)
        self.assertEqual(items(store, show_id)[0].status, "killed")
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
