import unittest

from volt.cues import add_cue, arm, cues
from volt.library import index_words
from volt.rundown import add_item, attach, open_show
from volt.store import NotClearedError, Store


class CueContract(unittest.TestCase):
    def test_cue_is_stored_on_the_item_and_take_does_not_dispatch(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        show_id = open_show(store, "Final Four")
        item_id = add_item(store, show_id, "Steal")
        attach(store, item_id, span_id)
        cue_id = add_cue(store, item_id, "corevideo.pro", "show-input.take", "camera-2")
        armed = arm(store, item_id)
        self.assertEqual(armed[0].id, cue_id)
        self.assertEqual(armed[0].status, "armed")
        self.assertEqual(cues(store, item_id)[0].command, "show-input.take")
        self.assertEqual(store.conn.execute("SELECT status FROM item WHERE id = ?", (item_id,)).fetchone()["status"], "on air")

    def test_uncleared_span_does_not_arm_the_cue(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        index_words(store, asset.id, [("steal", "01:00:00:00", "01:00:00:08")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        show_id = open_show(store, "Final Four")
        item_id = add_item(store, show_id, "Steal")
        attach(store, item_id, span_id)
        add_cue(store, item_id, "vmix", "Input", "2")
        with self.assertRaises(NotClearedError):
            arm(store, item_id)
        self.assertEqual(cues(store, item_id)[0].status, "stored")


if __name__ == "__main__":
    unittest.main()
