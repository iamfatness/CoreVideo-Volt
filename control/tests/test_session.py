import unittest

from volt.library import index_words
from volt.session import list_assets, seek_hit
from volt.store import Store


class SessionContract(unittest.TestCase):
    def test_hit_seeks_the_proxy_not_the_hires(self):
        store = Store()
        asset = store.mint_asset(owner="desk", show_name="final-four", camera="iso-3")
        store.add_essence(asset.id, role="hi-res", location="media/iso-3.mxf")
        store.add_essence(asset.id, role="proxy", location="proxy/iso-3.mp4")
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        rows = list_assets(store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].camera, "iso-3")
        hit = seek_hit(store, "steal")
        self.assertEqual(hit.tc_in, "01:12:08:12")
        self.assertEqual(hit.proxy, "proxy/iso-3.mp4")
        self.assertNotIn("mxf", hit.proxy)

    def test_missing_word_does_not_invent_a_hit(self):
        store = Store()
        self.assertIsNone(seek_hit(store, "steal"))


if __name__ == "__main__":
    unittest.main()
