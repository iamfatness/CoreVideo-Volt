import unittest

from volt.store import HoldError, NotClearedError, Store


class StoreContract(unittest.TestCase):
    def test_mint_does_not_wait_for_transcode(self):
        store = Store()
        asset = store.mint_asset(owner="desk", show_name="final-four", camera="iso-3")
        self.assertEqual(asset.owner, "desk")
        self.assertFalse(asset.legal_hold)
        self.assertFalse(asset.cleared)
        essence = store.add_essence(asset.id, role="hi-res", location="quarantine/card.mxf", open_file=True)
        self.assertTrue(essence)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_proxy_is_not_a_second_asset(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.add_essence(asset.id, role="hi-res", location="media/a.mxf", open_file=True)
        store.add_essence(asset.id, role="proxy", location="proxy/a.mp4", open_file=True)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 2)

    def test_replay_does_not_mint(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        job = store.enqueue(asset.id, "proxy", "proxy-v1")
        store.fail_job(job.id, "encoder died")
        again = store.enqueue(asset.id, "proxy", "proxy-v1")
        self.assertEqual(again.id, job.id)
        self.assertEqual(again.attempt, 2)
        self.assertEqual(again.status, "queued")
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0], 1)

    def test_hold_blocks_delete_and_uncleared_blocks_publish(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.set_hold(asset.id, True)
        with self.assertRaises(HoldError):
            store.delete_asset(asset.id)
        with self.assertRaises(NotClearedError):
            store.assert_publishable(asset.id)
        store.set_hold(asset.id, False)
        store.set_cleared(asset.id, True)
        store.assert_publishable(asset.id)
        self.assertTrue(store.delete_asset(asset.id))

    def test_span_holds_timecode_not_a_path(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        span_id = store.add_span(asset.id, "transcript", "01:12:08:00", "01:12:09:12", text="steal")
        row = store.conn.execute("SELECT * FROM span WHERE id = ?", (span_id,)).fetchone()
        self.assertEqual(row["asset_id"], asset.id)
        self.assertEqual(row["text"], "steal")
        self.assertNotIn("location", row.keys())


if __name__ == "__main__":
    unittest.main()
