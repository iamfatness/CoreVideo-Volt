"""Archive tier (migration 20): upload, verify, delete the local copy; restore downloads back. Hermetic: a local backend and fakes."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_operator_api import Api
from volt import archive, serve, storage
from volt.storage import LocalBackend, StorageError
from volt.store import ArchivedError, NotClearedError, Store

DATA = b"the hi-res essence " * 1000


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store()
        self.backend = LocalBackend(self.root / "cold")
        self.asset, self.path = self.make_asset("clip.mxf")

    def make_asset(self, name, data=DATA, open_file=False):
        a = self.store.mint_asset(owner="desk")
        self.store.set_cleared(a.id, True)
        path = self.root / name
        path.write_bytes(data)
        self.store.add_essence(a.id, "hi-res", str(path), open_file=open_file, frame_rate="25", timecode_start="01:00:00:00")
        return a.id, path

    def row(self, asset=None):
        return self.store.conn.execute("SELECT * FROM essence WHERE asset_id = ? AND role = 'hi-res'", (asset or self.asset,)).fetchone()

    def copies(self):
        """How many copies of the bytes exist: the local file and the archived object."""
        row = self.row()
        local = Path(row["location"]).is_file()
        remote = bool(row["archive_key"]) and self.backend.exists(row["archive_key"])
        return int(local) + int(remote)


class Request(Base):
    def test_an_ordinary_asset_queues_a_job(self):
        job = archive.request_archive(self.store, self.asset)
        self.assertEqual(self.store.get_job(job).type, "archive")

    def test_refusals_say_why(self):
        self.store.set_hold(self.asset, True)
        with self.assertRaises(archive.ArchiveRefused) as ctx:
            archive.request_archive(self.store, self.asset)
        self.assertTrue(any("legal hold" in r for r in ctx.exception.reasons))
        other, _ = self.make_asset("rec.mxf", open_file=True)
        with self.assertRaises(archive.ArchiveRefused) as ctx:
            archive.request_archive(self.store, other)
        self.assertTrue(any("recording" in r for r in ctx.exception.reasons))
        bare = self.store.mint_asset(owner="desk").id
        with self.assertRaises(archive.ArchiveRefused) as ctx:
            archive.request_archive(self.store, bare)
        self.assertTrue(any("hi-res" in r for r in ctx.exception.reasons))
        with self.assertRaises(KeyError):
            archive.request_archive(self.store, "nope")

    def test_an_archived_asset_cannot_be_archived_again_and_restore_needs_an_archive(self):
        archive.run_archive(self.store, self.asset, self.backend, "cold")
        with self.assertRaises(archive.ArchiveRefused):
            archive.request_archive(self.store, self.asset)
        other, _ = self.make_asset("b.mxf")
        with self.assertRaises(archive.ArchiveRefused):
            archive.request_restore(self.store, other)


class Archiving(Base):
    def test_upload_verify_record_then_delete_local(self):
        archive.run_archive(self.store, self.asset, self.backend, "cold")
        row = self.row()
        self.assertEqual((row["tier"], row["archive_sha256"]), ("archived", sha(DATA)))
        self.assertTrue(row["archive_key"].startswith(f"archive/{self.asset}/"))
        self.assertEqual(self.backend.restore_state(row["archive_key"]), "archived")
        self.assertFalse(self.path.exists())
        self.assertIsNotNone(self.store.get_asset(self.asset) and self.store.conn.execute(
            "SELECT archived_at FROM asset WHERE id = ?", (self.asset,)).fetchone()["archived_at"])

    def test_never_zero_copies_whatever_fails(self):
        for target in ("put_file", "set_storage_class"):
            with self.subTest(fails=target):
                asset, path = self.make_asset(f"x-{target}.mxf")
                bad = LocalBackend(self.root / "cold")
                with mock.patch.object(bad, target, side_effect=StorageError("down")):
                    with self.assertRaises(StorageError):
                        archive.run_archive(self.store, asset, bad, "cold")
                self.assertTrue(path.is_file())                                    # the local copy is still there
                self.assertIn(self.row(asset)["tier"], (None, "online", "archiving"))   # and nothing says archived
                archive.run_archive(self.store, asset, self.backend, "cold")        # a rerun finishes the job
                self.assertEqual(self.row(asset)["tier"], "archived")

    def test_a_crash_after_the_record_before_the_delete_is_finished_by_a_rerun(self):
        with mock.patch("volt.archive.os.remove", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                archive.run_archive(self.store, self.asset, self.backend, "cold")
        self.assertEqual(self.row()["tier"], "archived")
        self.assertEqual(self.copies(), 2)                                          # both exist: nothing lost
        archive.run_archive(self.store, self.asset, self.backend, "cold")
        self.assertEqual(self.copies(), 1)
        self.assertFalse(self.path.exists())

    def test_an_upload_that_does_not_verify_leaves_the_file_and_the_tier_alone(self):
        class Liar(LocalBackend):
            def put_file(self, local_path, key):
                obj = super().put_file(local_path, key)
                return storage.StoredObject(obj.backend, obj.key, obj.url, obj.size, "0" * 64)
        with self.assertRaises(archive.ArchiveFailed):
            archive.run_archive(self.store, self.asset, Liar(self.root / "cold"), "cold")
        self.assertTrue(self.path.is_file())
        self.assertNotEqual(self.row()["tier"], "archived")

    def test_a_missing_local_file_is_a_permanent_failure(self):
        self.path.unlink()
        with self.assertRaises(archive.ArchiveFailed):
            archive.run_archive(self.store, self.asset, self.backend, "cold")

    def test_legal_hold_placed_after_the_request_stops_the_job(self):
        archive.request_archive(self.store, self.asset)
        self.store.set_hold(self.asset, True)
        with self.assertRaises(archive.ArchiveRefused):
            archive.run_archive(self.store, self.asset, self.backend, "cold")
        self.assertTrue(self.path.is_file())


class Restoring(Base):
    def setUp(self):
        super().setUp()
        archive.run_archive(self.store, self.asset, self.backend, "cold")

    def test_restore_downloads_verifies_and_goes_online(self):
        self.assertFalse(self.path.exists())
        archive.run_restore(self.store, self.asset, self.backend)
        self.assertEqual(self.path.read_bytes(), DATA)
        row = self.row()
        self.assertEqual(row["tier"], "online")
        self.assertIsNone(self.store.conn.execute("SELECT archived_at FROM asset WHERE id = ?", (self.asset,)).fetchone()["archived_at"])
        archive.run_restore(self.store, self.asset, self.backend)    # idempotent once online

    def test_a_restore_that_takes_time_waits_and_polls(self):
        states = iter(["archived", "restoring", "restoring", "online"])
        calls = []
        slow = LocalBackend(self.root / "cold")
        slow.restore_state = lambda key: next(states)
        slow.restore = lambda key, days=7: calls.append(("restore", key))
        with self.assertRaises(archive.Wait):
            archive.run_restore(self.store, self.asset, slow)       # asks for the restore, sees it restoring, waits
        self.assertEqual(self.row()["tier"], "restoring")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(archive.Wait):
            archive.run_restore(self.store, self.asset, slow)       # still restoring
        self.assertEqual(len(calls), 1)                             # asked once, not again
        archive.run_restore(self.store, self.asset, slow)           # online: downloads
        self.assertEqual(self.row()["tier"], "online")
        self.assertEqual(self.path.read_bytes(), DATA)

    def test_a_download_that_does_not_match_is_not_put_in_place(self):
        class Corrupt(LocalBackend):
            def get_file(self, key, dest):
                Path(dest).write_bytes(b"not the essence")
        with self.assertRaises(archive.ArchiveFailed):
            archive.run_restore(self.store, self.asset, Corrupt(self.root / "cold"))
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.root.glob("*.restore")))         # the temp file is gone
        self.assertIn(self.row()["tier"], ("archived", "restoring"))


class Refusals(Base):
    def test_an_archived_asset_cannot_be_published_or_taken(self):
        self.store.assert_publishable(self.asset)                    # fine while online
        archive.run_archive(self.store, self.asset, self.backend, "cold")
        with self.assertRaises(ArchivedError):
            self.store.assert_publishable(self.asset)
        self.assertFalse(issubclass(ArchivedError, NotClearedError))  # callers can tell the two apart

    def test_the_proxy_build_and_the_nle_export_refuse(self):
        from volt import nle, record
        archive.run_archive(self.store, self.asset, self.backend, "cold")
        with self.assertRaises(ArchivedError):
            record.build_proxy(self.store, self.asset, self.root / "proxy")
        with self.assertRaises(ArchivedError):
            archive.require_online(self.store, self.asset)
        self.assertTrue(hasattr(nle, "resolve"))

    def test_blocked_by_says_archived(self):
        from volt import operator_state
        self.assertEqual(operator_state._blocked_by(True, False, True), "archived")
        self.assertEqual(operator_state._blocked_by(True, True, True), "legal hold")
        self.assertIsNone(operator_state._blocked_by(True, False, False))


class ArchiveApi(Api):
    def setUp(self):
        super().setUp()
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        a = self.store.mint_asset(owner="desk")
        self.store.set_cleared(a.id, True)
        path = Path(self.tmp2.name) / "c.mxf"
        path.write_bytes(DATA)
        self.store.add_essence(a.id, "hi-res", str(path), frame_rate="25")
        self.asset = a.id

    def test_archive_and_rehydrate_routes(self):
        status, out, _ = self.call("POST", f"/api/assets/{self.asset}/archive", {})
        self.assertEqual((status, out["ok"]), (200, True))
        self.assertEqual(self.store.get_job(out["job"]).type, "archive")
        archive.run_archive(self.store, self.asset, LocalBackend(Path(self.tmp2.name) / "cold"), "cold")
        _, state, _ = self.call("GET", "/api/state")
        mine = [x for x in state["assets"] if x["id"] == self.asset][0]
        self.assertEqual(mine["tier"], "archived")
        status, out, _ = self.call("POST", f"/api/assets/{self.asset}/rehydrate", {})
        self.assertEqual((status, out["ok"]), (200, True))
        self.assertEqual(self.store.get_job(out["job"]).type, "restore")
        self.assertEqual(serve.ROUTE_ROLES["act_asset_archive"], "operator")
        self.assertEqual(serve.ROUTE_ROLES["act_asset_rehydrate"], "operator")

    def test_refusals_carry_a_code_and_reasons(self):
        self.store.set_hold(self.asset, True)
        status, out, _ = self.call("POST", f"/api/assets/{self.asset}/archive", {})
        self.assertEqual((status, out["error"]["code"]), (409, "archive_refused"))
        self.assertTrue(out["reasons"])
        status, out, _ = self.call("POST", f"/api/assets/{self.asset}/rehydrate", {})
        self.assertEqual((status, out["error"]["code"]), (409, "archive_refused"))
        status, out, _ = self.call("POST", "/api/assets/nope/archive", {})
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))

    def test_audited(self):
        self.call("POST", f"/api/assets/{self.asset}/archive", {})
        self.assertEqual([r["action"] for r in self.store.list_audit(self.asset)], ["archive"])


if __name__ == "__main__":
    unittest.main()
