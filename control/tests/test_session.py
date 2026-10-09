import unittest

from volt.library import index_words
from volt.publish import publish
from volt.rundown import add_item, attach, open_show
from volt.session import attach_hit, list_assets, publish_item, rundown, seek_hit
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

    def test_rundown_item_shows_the_published_url(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
        publish(store, span_id)
        show_id = open_show(store, "Final Four")
        item_id = add_item(store, show_id, "Steal")
        attach(store, item_id, span_id)
        rows = rundown(store, show_id)
        self.assertEqual(rows[0].slug, "Steal")
        self.assertEqual(len(rows[0].urls), 3)
        self.assertTrue(all(url.startswith("volt://") for url in rows[0].urls))

    def test_search_hit_can_be_attached_and_published(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
        show_id = open_show(store, "Final Four")
        item_id = add_item(store, show_id, "Steal")
        attach_hit(store, item_id, "steal")
        urls = publish_item(store, item_id)
        self.assertEqual(len(urls), 3)
        self.assertEqual(len(rundown(store, show_id)[0].urls), 3)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_missing_word_does_not_invent_a_hit(self):
        store = Store()
        self.assertIsNone(seek_hit(store, "steal"))


if __name__ == "__main__":
    unittest.main()
