import unittest

from volt.library import index_words, search
from volt.store import Store


class LibraryContract(unittest.TestCase):
    def test_search_returns_the_span_not_the_file(self):
        store = Store()
        asset = store.mint_asset(owner="desk", camera="iso-3")
        store.add_essence(asset.id, role="hi-res", location="media/a.mxf")
        index_words(
            store,
            asset.id,
            [
                ("timeout", "01:12:08:00", "01:12:08:12"),
                ("steal", "01:12:08:12", "01:12:09:00"),
            ],
        )
        hits = search(store, "steal")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].asset_id, asset.id)
        self.assertEqual(hits[0].tc_in, "01:12:08:12")
        self.assertFalse(hasattr(hits[0], "location"))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_reindex_does_not_duplicate_spans(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        words = [("steal", "01:00:00:00", "01:00:00:08")]
        index_words(store, asset.id, words)
        index_words(store, asset.id, words)
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM span WHERE asset_id = ?", (asset.id,)
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
