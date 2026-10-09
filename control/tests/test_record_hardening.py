import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.test_record import _clip
from volt import record
from volt.record import RecordError, build_proxy, record_start, wrap
from volt.store import Store


def _status(store, key):
    return store.conn.execute(
        "SELECT status FROM job WHERE idempotency_key = ?", (key,)
    ).fetchone()["status"]


class ProxyJobIsFoundByKey(unittest.TestCase):
    def test_failure_lands_on_the_assets_own_proxy_job(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store()
            started = record_start(store, "cv:iso-3", root / "missing.mp4", owner="desk")
            decoy = store.enqueue(started.asset_id, "proxy", "proxy:older-pass")
            with self.assertRaises(RecordError):
                build_proxy(store, started.asset_id, root / "proxy")
            self.assertEqual(_status(store, "proxy:cv:iso-3"), "failed")
            self.assertEqual(_status(store, "proxy:older-pass"), "queued")
            self.assertTrue(decoy.id)

    def test_wrap_completes_the_assets_own_proxy_job(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            store.enqueue(started.asset_id, "proxy", "proxy:decoy")
            wrap(store, started.asset_id, settle=0)
            self.assertEqual(_status(store, "proxy:cv:iso-3"), "done")
            self.assertEqual(_status(store, "proxy:decoy"), "queued")


class WrapRefusesAGrowingFile(unittest.TestCase):
    def test_growing_file_is_not_wrapped(self):
        with TemporaryDirectory() as tmp:
            iso = Path(tmp) / "iso.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            stop = threading.Event()

            def writer():
                while not stop.is_set():
                    with iso.open("ab") as handle:
                        handle.write(b"\0" * 64)
                    time.sleep(0.01)

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                with self.assertRaises(RecordError):
                    wrap(store, started.asset_id, settle=0.2)
            finally:
                stop.set()
                thread.join()
            row = store.conn.execute(
                "SELECT open, checksum FROM essence WHERE role = 'hi-res'"
            ).fetchone()
            self.assertEqual(row["open"], 1)
            self.assertIsNone(row["checksum"])

    def test_wrap_twice_returns_the_same_checksum_without_rehashing(self):
        with TemporaryDirectory() as tmp:
            iso = Path(tmp) / "iso.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            first = wrap(store, started.asset_id, settle=0)
            with mock.patch.object(record, "sha256", side_effect=AssertionError("rehashed")):
                self.assertEqual(wrap(store, started.asset_id, settle=0), first)


class CurrentProxyIsNotRebuilt(unittest.TestCase):
    def test_wrapped_asset_with_a_current_proxy_skips_ffmpeg(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            first = build_proxy(store, started.asset_id, root / "proxy")
            wrap(store, started.asset_id, settle=0)
            with mock.patch.object(record.subprocess, "run", side_effect=AssertionError("ffmpeg ran")):
                self.assertEqual(build_proxy(store, started.asset_id, root / "proxy"), first)

    def test_open_asset_always_refreshes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "iso.mp4"
            _clip(iso)
            store = Store()
            started = record_start(store, "cv:iso-3", iso, owner="desk")
            build_proxy(store, started.asset_id, root / "proxy")
            with mock.patch.object(record.subprocess, "run", wraps=record.subprocess.run) as run:
                build_proxy(store, started.asset_id, root / "proxy")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
