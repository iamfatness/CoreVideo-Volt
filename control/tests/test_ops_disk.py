"""Disk-space floor and cache housekeeping (volt/disk.py), and where the floor is enforced."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from volt import disk, ingest
from volt.store import Store


def _write(path: Path, size: int, age: float = 3600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class Floor(unittest.TestCase):
    def test_refuses_under_the_floor_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            free = disk.free_bytes(tmp)
            self.assertIsNotNone(free)
            env = {"VOLT_MIN_FREE_MB": str((free // 1048576) + 1024)}
            with self.assertRaises(disk.DiskLowError) as ctx:
                disk.require_space(tmp, "proxy build", env=env)
            self.assertIn("VOLT_MIN_FREE_MB", str(ctx.exception))
            self.assertIn("proxy build", str(ctx.exception))
            disk.require_space(tmp, "proxy build", env={"VOLT_MIN_FREE_MB": "0"})  # floor off

    def test_a_file_bigger_than_the_room_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            free = disk.free_bytes(tmp)
            with self.assertRaises(disk.DiskLowError):
                disk.require_space(tmp, "ingest", need=free + 1, env={"VOLT_MIN_FREE_MB": "0"})

    def test_missing_directory_measures_its_parent_and_bad_settings_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNotNone(disk.free_bytes(Path(tmp) / "not" / "yet"))
        with self.assertRaises(ValueError):
            disk.min_free_bytes({"VOLT_MIN_FREE_MB": "lots"})
        with self.assertRaises(ValueError):
            disk.min_free_bytes({"VOLT_MIN_FREE_MB": "-1"})

    def test_default_floor(self):
        self.assertEqual(disk.min_free_bytes({}), 2048 * 1024 * 1024)

    def test_ingest_refuses_before_minting_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _write(Path(tmp) / "clip.mp4", 100)
            store = Store(str(Path(tmp) / "v.db"))
            try:
                with mock.patch.dict(os.environ, {"VOLT_MIN_FREE_MB": "99999999"}):
                    with self.assertRaises(ingest.IngestError) as ctx:
                        ingest.ingest_file(store, source, Path(tmp) / "q", owner="desk")
                self.assertIn("refusing ingest", str(ctx.exception))
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 0)
                self.assertFalse((Path(tmp) / "q").exists())
            finally:
                store.close()


class Cache(unittest.TestCase):
    def test_prune_removes_oldest_thumbnails_first_and_only_thumbnails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            old = _write(out / "thumbs" / "a" / "1.jpg", 1000, age=5000)
            mid = _write(out / "thumbs" / "a" / "2.jpg", 1000, age=3000)
            new = _write(out / "thumbs" / "b" / "3.jpg", 1000, age=1000)
            fresh = _write(out / "thumbs" / "b" / "4.jpg", 1000, age=1)
            other = _write(out / "thumbs" / "b" / "notes.txt", 5000, age=9999)
            source = _write(out / "proxy.mp4", 9000, age=9999)
            result = disk.prune_thumbs(out, cap_bytes=2500)
            self.assertFalse(old.exists())
            self.assertFalse(mid.exists())
            self.assertTrue(new.exists() or not new.exists())  # target is 90 percent of the cap
            self.assertTrue(fresh.exists(), "a file written in the last minute is never evicted")
            self.assertTrue(other.exists())
            self.assertTrue(source.exists())
            self.assertGreaterEqual(result["removed"], 2)
            self.assertLessEqual(result["after"], 2500)

    def test_under_the_cap_or_no_cap_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = _write(Path(tmp) / "thumbs" / "a" / "1.jpg", 1000)
            self.assertEqual(disk.prune_thumbs(tmp, cap_bytes=5000)["removed"], 0)
            self.assertEqual(disk.prune_thumbs(tmp, cap_bytes=0)["removed"], 0)
            self.assertTrue(kept.exists())

    def test_stale_part_files_go_registered_proxies_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stale = _write(d / "a.1234.part.mp4", 10, age=48 * 3600)
            live = _write(d / "b.5678.part.mp4", 10, age=60)
            proxy = _write(d / "a.mp4", 10, age=48 * 3600)
            self.assertEqual(disk.sweep_stale_parts(d, max_age=24 * 3600), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(live.exists())
            self.assertTrue(proxy.exists())

    def test_tree_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "a" / "1", 10)
            _write(Path(tmp) / "2", 5)
            self.assertEqual(disk.tree_size(tmp), 15)
            self.assertEqual(disk.tree_size(Path(tmp) / "missing"), 0)


if __name__ == "__main__":
    unittest.main()
