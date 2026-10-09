import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt.ingest import ingest_file
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


class IngestContract(unittest.TestCase):
    def test_card_mints_once_and_replay_does_not(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = root / "A001.mp4"
            _clip(card)
            store = Store()
            first = ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A001")
            self.assertTrue(first.minted)
            self.assertEqual(first.status, "done")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
            second = ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A001")
            self.assertFalse(second.minted)
            self.assertEqual(second.asset_id, first.asset_id)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 1)

    def test_unreadable_file_stays_in_quarantine(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "broken.mp4"
            bad.write_bytes(b"not a media file")
            store = Store()
            result = ingest_file(store, bad, root / "quarantine", owner="desk", source_key="card:bad")
            self.assertEqual(result.status, "quarantine")
            self.assertEqual(result.essence_id, "")
            job = store.conn.execute("SELECT status, error FROM job WHERE id = ?", (result.job_id,)).fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
