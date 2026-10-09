import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt.record import build_proxy, record_start, wrap
from volt.store import Store


def _clip(path: Path) -> None:
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class RecordContract(unittest.TestCase):
    def test_record_start_mints_while_open_and_proxy_is_not_a_new_asset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso-3.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk", camera="iso-3")
            self.assertTrue(started.minted)
            open_flag = store.conn.execute(
                "SELECT open FROM essence WHERE id = ?", (started.essence_id,)
            ).fetchone()["open"]
            self.assertEqual(open_flag, 1)
            proxy_id = build_proxy(store, started.asset_id, root / "proxy")
            self.assertTrue(proxy_id)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 2)
            again = record_start(store, "cv:iso-3", iso, owner="desk", camera="iso-3")
            self.assertFalse(again.minted)
            self.assertEqual(again.asset_id, started.asset_id)

    def test_wrap_closes_and_checksums_without_minting(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso-3.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            build_proxy(store, started.asset_id, root / "proxy")
            digest = wrap(store, started.asset_id)
            self.assertEqual(len(digest), 64)
            opens = store.conn.execute(
                "SELECT open FROM essence WHERE asset_id = ?", (started.asset_id,)
            ).fetchall()
            self.assertTrue(all(row["open"] == 0 for row in opens))
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_refreshing_an_open_proxy_does_not_mint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso-3.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            first = build_proxy(store, started.asset_id, root / "proxy")
            second = build_proxy(store, started.asset_id, root / "proxy")
            self.assertEqual(first, second)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM essence WHERE role = 'proxy'").fetchone()[0],
                1,
            )
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
