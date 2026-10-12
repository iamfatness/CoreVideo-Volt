"""A scene cut is a span. Search returns it."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import shots
from volt.library import search
from volt.store import Store


class ShotSpans(unittest.TestCase):
    def test_cuts_become_spans_and_search_returns_the_shot(self):
        with TemporaryDirectory() as tmp:
            store = Store()
            asset = store.mint_asset(owner="desk")
            src = Path(tmp) / "iso.mp4"
            src.write_bytes(b"media")
            store.add_essence(
                asset.id, role="hi-res", location=str(src),
                open_file=False, frame_rate="25", timecode_start="01:00:00:00",
            )
            count = shots.index_shots(store, asset.id, cuts=[4.0, 9.0])
            self.assertEqual(count, 3)
            self.assertEqual(shots.shots(store, asset.id), [
                ("01:00:00:00", "01:00:04:00", "shot 1"),
                ("01:00:04:00", "01:00:09:00", "shot 2"),
                ("01:00:09:00", "01:00:09:01", "shot 3"),
            ])
            hit = search(store, "shot 2")
            self.assertEqual(len(hit), 1)
            self.assertEqual((hit[0].asset_id, hit[0].tc_in), (asset.id, "01:00:04:00"))

    def test_reindex_does_not_duplicate(self):
        with TemporaryDirectory() as tmp:
            store = Store()
            asset = store.mint_asset(owner="desk")
            src = Path(tmp) / "iso.mp4"
            src.write_bytes(b"media")
            store.add_essence(asset.id, role="hi-res", location=str(src), open_file=False, frame_rate="25")
            shots.index_shots(store, asset.id, cuts=[2.0])
            shots.index_shots(store, asset.id, cuts=[2.0])
            self.assertEqual(len(shots.shots(store, asset.id)), 2)

    def test_open_file_is_refused(self):
        store = Store()
        asset = store.mint_asset(owner="desk")
        store.add_essence(asset.id, role="hi-res", location="/tmp/open.mp4", open_file=True)
        with self.assertRaises(shots.ShotError):
            shots.index_shots(store, asset.id, cuts=[1.0])


if __name__ == "__main__":
    unittest.main()
