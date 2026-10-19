"""Watch folder: enqueue ingest for files whose size and mtime have stayed still."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from volt import watch
from volt.store import Store


def _jobs(store):
    return list(store.conn.execute("SELECT id, type, idempotency_key FROM job WHERE type = 'ingest'"))


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class WatchScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.quarantine = self.root / "quarantine"
        self.store = Store()
        self.seen = {}
        self.now = Clock()

    def scan(self, roots=None, **kw):
        kw.setdefault("seen", self.seen)
        kw.setdefault("now", self.now)
        return watch.scan(
            self.store,
            roots if roots is not None else [self.inbox],
            self.quarantine,
            **kw,
        )

    def test_growing_file_is_not_requested(self):
        clip = self.inbox / "card.mov"
        clip.write_bytes(b"aa")
        self.assertEqual(self.scan(stable_seconds=5), [])
        clip.write_bytes(b"aaaa")
        self.now.t += 1
        self.assertEqual(self.scan(stable_seconds=5), [])
        self.assertEqual(_jobs(self.store), [])
        self.assertFalse(self.quarantine.exists() and any(self.quarantine.iterdir()))

    def test_stable_file_is_requested_once(self):
        clip = self.inbox / "card.mov"
        clip.write_bytes(b"stable-bytes")
        self.assertEqual(self.scan(stable_seconds=5), [])
        self.now.t += 5
        first = self.scan(stable_seconds=5)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].status, "queued")
        self.assertEqual(len(_jobs(self.store)), 1)
        self.assertFalse((self.quarantine / f"{first[0].asset_id}-{clip.name}").exists())
        second = self.scan(stable_seconds=5)
        self.assertEqual(second, [])
        self.assertEqual(len(_jobs(self.store)), 1)

    def test_path_outside_roots_is_never_requested(self):
        other = self.root / "other"
        other.mkdir()
        stray = other / "card.mov"
        stray.write_bytes(b"outside")
        self.scan(stable_seconds=0)
        self.now.t += 10
        self.scan(stable_seconds=0)
        self.assertEqual(_jobs(self.store), [])

    def test_skips_dotfiles_part_suffix_and_directories(self):
        (self.inbox / ".hidden.mov").write_bytes(b"dot")
        (self.inbox / "card.mov.part").write_bytes(b"part")
        nested = self.inbox / "sub"
        nested.mkdir()
        (nested / "nested.mov").write_bytes(b"nested")
        self.scan(stable_seconds=0)
        self.now.t += 10
        self.scan(stable_seconds=0)
        self.assertEqual(_jobs(self.store), [])

    def test_source_key_matches_http_so_watch_and_operator_share_an_asset(self):
        clip = self.inbox / "card.mov"
        clip.write_bytes(b"same-file")
        self.scan(stable_seconds=5)
        self.now.t += 5
        result = self.scan(stable_seconds=5)[0]
        st = clip.resolve().stat()
        key = f"file:{os.path.normcase(str(clip.resolve()))}:{st.st_size}:{st.st_mtime_ns}"
        asset = self.store.get_asset(result.asset_id)
        self.assertEqual(asset.source_key, key)
        from volt import ingest
        replay = ingest.request(
            self.store, clip, owner="desk", quarantine=self.quarantine, source_key=key)
        self.assertEqual(replay.asset_id, result.asset_id)
        self.assertFalse(replay.minted)
        self.assertEqual(len(_jobs(self.store)), 1)

    def test_persisted_seen_lets_a_second_scan_enqueue_after_stable_seconds(self):
        # Production change that would fail this: CLI seen dying with the process (empty dict on --once).
        clip = self.inbox / "card.mov"
        clip.write_bytes(b"wait-then-ingest")
        seen_path = self.root / "watch-seen.json"
        first = watch.load_seen(seen_path)
        self.assertEqual(self.scan(seen=first, stable_seconds=5), [])
        watch.save_seen(seen_path, first)
        self.assertEqual(_jobs(self.store), [])
        self.now.t += 5
        second = watch.load_seen(seen_path)
        queued = self.scan(seen=second, stable_seconds=5)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].status, "queued")
        self.assertEqual(len(_jobs(self.store)), 1)


class RunnerWatch(unittest.TestCase):
    def test_runner_appends_watch_periodic_when_volt_watch_is_set(self):
        from volt import runner as runner_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = str(root / "v.db")
            Store(db).close()
            captured = []
            real = runner_mod.Runner

            def wrap(*args, **kwargs):
                r = real(*args, **kwargs)
                captured.append(r)
                return r

            env = {
                **os.environ,
                "VOLT_WATCH": "1",
                "VOLT_INGEST_ROOTS": str(root / "inbox"),
                "VOLT_QUARANTINE": str(root / "q"),
                "VOLT_WATCH_EVERY": "2",
                "VOLT_CACHE_GC_EVERY": "0",
                "VOLT_PURGE_EVERY": "0",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("volt.runner.Runner", side_effect=wrap):
                    self.assertEqual(runner_mod.main([db, "--once"]), 0)
            self.assertTrue(captured)
            self.assertIn("watch", [p.name for p in captured[0].periodic])

    def test_runner_does_not_append_watch_when_flag_is_off(self):
        from volt import runner as runner_mod

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "v.db")
            Store(db).close()
            captured = []
            real = runner_mod.Runner

            def wrap(*args, **kwargs):
                r = real(*args, **kwargs)
                captured.append(r)
                return r

            env = {**os.environ, "VOLT_WATCH": "0", "VOLT_CACHE_GC_EVERY": "0", "VOLT_PURGE_EVERY": "0"}
            env.pop("VOLT_WATCH", None)
            env["VOLT_WATCH"] = "0"
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("volt.runner.Runner", side_effect=wrap):
                    self.assertEqual(runner_mod.main([db, "--once"]), 0)
            self.assertNotIn("watch", [p.name for p in captured[0].periodic])


class WatchSeenCli(unittest.TestCase):
    def test_once_at_default_stable_writes_seen_next_to_the_db_and_does_not_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "card.mov").write_bytes(b"clip")
            db = str(root / "v.db")
            Store(db).close()
            env = dict(os.environ, VOLT_INGEST_ROOTS=str(inbox), VOLT_QUARANTINE=str(root / "q"))
            env.pop("VOLT_WATCH_STABLE_SECONDS", None)
            env.pop("VOLT_WATCH_SEEN", None)
            out = subprocess.run(
                [sys.executable, "-m", "volt", "watch", "--once", db],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            store = Store(db, init=False)
            jobs = store.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'ingest'").fetchone()[0]
            store.close()
            self.assertEqual(jobs, 0)
            seen_path = Path(db + ".watch-seen.json")
            self.assertTrue(seen_path.exists())
            past = time.time() - 10
            data = watch.load_seen(seen_path)
            for path, (size, mtime_ns, _) in list(data.items()):
                data[path] = (size, mtime_ns, past)
            watch.save_seen(seen_path, data)
            out = subprocess.run(
                [sys.executable, "-m", "volt", "watch", "--once", db],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            store = Store(db, init=False)
            jobs = store.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'ingest'").fetchone()[0]
            store.close()
            self.assertEqual(jobs, 1)
