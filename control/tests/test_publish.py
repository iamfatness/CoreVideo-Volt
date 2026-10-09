import unittest

from volt.library import index_words
from volt.publish import publish
from volt.store import NotClearedError, Store


class PublishContract(unittest.TestCase):
    def _span(self, store, cleared=True):
        asset = store.mint_asset(owner="desk")
        if cleared:
            store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        return asset, span_id

    def test_one_span_three_versions_same_asset(self):
        store = Store()
        asset, span_id = self._span(store)
        versions = publish(store, span_id)
        self.assertEqual([v.kind for v in versions], ["house", "ott", "social"])
        self.assertTrue(all(v.url and v.url.startswith("volt://") for v in versions))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 3)

    def test_retry_does_not_mint_a_second_version(self):
        store = Store()
        asset, span_id = self._span(store)
        publish(store, span_id, fail="ott")
        publish(store, span_id)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 3)
        ott = store.conn.execute("SELECT status, url FROM version WHERE kind = 'ott'").fetchone()
        self.assertEqual(ott["status"], "done")
        self.assertTrue(ott["url"])

    def test_uncleared_cannot_publish(self):
        store = Store()
        _, span_id = self._span(store, cleared=False)
        with self.assertRaises(NotClearedError):
            publish(store, span_id)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
