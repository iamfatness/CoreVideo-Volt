"""Three server-side gaps: unpublish a version, delete a show, and attach racing a take.

The unpublish and attach-race tests run on SQLite and, when VOLT_TEST_PG_DSN is set, on live Postgres
(tests/racekit.py). The HTTP tests use the operator API harness (SQLite).
"""

import threading
import unittest
from unittest import mock

from volt import building, playout, publish as pub, retention, serve
from volt.playout import advance, go
from volt.session import rundown
from volt.rundown import ItemNotReady, add_item, attach, items, open_show
from volt.storage import StorageError, StoredObject
from volt.store import _id, _now

from tests import test_operator_api as base
from tests.racekit import PgBase, SqliteBase, _cleared_span


class MemBackend:
    id = "mem"

    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.fail_delete = False

    def put(self, key):
        self.objects[key] = b"x"
        return StoredObject(self.id, key, f"mem://{key}", 1, "0" * 64)

    def url(self, key, expires=None):
        return f"mem://{key}"

    def exists(self, key):
        return key in self.objects

    def delete(self, key):
        if self.fail_delete:
            raise StorageError("bucket unreachable")
        self.deleted.append(key)
        self.objects.pop(key, None)


def _version(store, backend, asset_id, span_id, kind="house", key=None, put=True, backend_id="mem"):
    """A done version whose object is in `backend`, as publish_files leaves it."""
    vid = _id()
    key = key or f"{asset_id}/{span_id}-{kind}.mp4"
    if put:
        backend.put(key)
    store.conn.execute(
        "INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at, storage_backend, storage_key,"
        " storage_sha256) VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?, ?)",
        (vid, asset_id, span_id, kind, f"mem://{key}", _now(), backend_id, key, "0" * 64))
    store.conn.commit()
    return vid


def _row(store, vid):
    return store.conn.execute("SELECT * FROM version WHERE id = ?", (vid,)).fetchone()


class UnpublishTests:
    def setUp(self):
        super().setUp()
        self.s = self.new_store()
        self.mem = MemBackend()
        self.asset, self.span = _cleared_span(self.s)

    def test_withdraw_keeps_the_row_removes_the_url_and_deletes_the_owned_object(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        show = open_show(self.s, "S")
        item = add_item(self.s, show, "a")
        attach(self.s, item, self.span)
        self.assertEqual(len(rundown(self.s, show)[0].urls), 1)
        out = pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual((out.storage, out.already), ("deleted", False))
        row = _row(self.s, vid)
        self.assertEqual(row["status"], "withdrawn")      # the record is kept
        self.assertIsNotNone(row["withdrawn_at"])
        self.assertIsNotNone(row["storage_deleted_at"])
        self.assertEqual(self.mem.objects, {})
        self.assertEqual(rundown(self.s, show)[0].urls, [])  # gone from the item's list
        outcomes = [r["outcome"] for r in self.s.conn.execute(
            "SELECT outcome FROM purge_audit WHERE ref = ?", (vid,)).fetchall()]
        self.assertEqual(sorted(outcomes), ["deleted", "withdrawn"])

    def test_a_second_call_is_a_no_op(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        pub.unpublish(self.s, vid, backend=self.mem)
        again = pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual((again.already, again.storage), (True, "already_deleted"))
        self.assertEqual(len(self.mem.deleted), 1)

    def test_a_failed_storage_delete_withdraws_and_a_retry_finishes_it(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        self.mem.fail_delete = True
        with self.assertRaises(pub.UnpublishError) as raised:
            pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual((raised.exception.status, raised.exception.code), (502, "storage_failed"))
        row = _row(self.s, vid)
        self.assertEqual(row["status"], "withdrawn")
        self.assertIsNone(row["storage_deleted_at"])
        self.assertTrue(self.mem.objects)
        # While the object is still there, a purge without --include-published is refused.
        self.s.conn.execute("UPDATE asset SET deleted_at = ? WHERE id = ?", (_now(), self.asset.id))
        self.s.conn.commit()
        self.assertTrue(any("stored object" in w for w in retention.blockers(self.s, self.asset.id, purge=True)))
        self.mem.fail_delete = False
        out = pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual((out.already, out.storage), (True, "deleted"))
        self.assertEqual(self.mem.objects, {})
        self.assertIsNotNone(_row(self.s, vid)["storage_deleted_at"])
        self.assertEqual(retention.blockers(self.s, self.asset.id, purge=True), [])

    def test_a_key_volt_did_not_write_is_never_deleted(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span, key="registered/elsewhere.mp4")
        out = pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual(out.storage, "not_owned")
        self.assertIn("registered/elsewhere.mp4", self.mem.objects)
        self.assertEqual(self.mem.deleted, [])
        self.assertEqual(_row(self.s, vid)["status"], "withdrawn")

    def test_an_object_another_active_version_uses_is_kept(self):
        key = f"{self.asset.id}/shared.mp4"
        a = _version(self.s, self.mem, self.asset.id, self.span, "house", key=key)
        # a second row on the same key (cannot happen via publish; guards the refcount)
        b = _id()
        self.s.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at, storage_backend, storage_key)"
            " VALUES (?, ?, ?, 'ott', 'done', 'mem://x', ?, 'mem', ?)", (b, self.asset.id, self.span, _now(), key))
        self.s.conn.commit()
        self.assertEqual(pub.unpublish(self.s, a, backend=self.mem).storage, "kept_shared")
        self.assertIn(key, self.mem.objects)
        self.assertEqual(pub.unpublish(self.s, b, backend=self.mem).storage, "deleted")
        self.assertEqual(self.mem.objects, {})

    def test_a_placeholder_version_has_nothing_to_delete(self):
        vid = _id()
        self.s.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, url, created_at)"
            " VALUES (?, ?, ?, 'house', 'done', 'volt://house/a/b', ?)", (vid, self.asset.id, self.span, _now()))
        self.s.conn.commit()
        self.assertEqual(pub.unpublish(self.s, vid, backend=self.mem).storage, "none")
        self.assertEqual(_row(self.s, vid)["status"], "withdrawn")

    def test_refused_while_an_item_using_the_span_is_on_air(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        show = open_show(self.s, "S")
        item = add_item(self.s, show, "a")
        attach(self.s, item, self.span)
        go(self.s, show)
        with self.assertRaises(pub.UnpublishError) as raised:
            pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "on_air"))
        self.assertEqual(_row(self.s, vid)["status"], "done")
        self.assertTrue(self.mem.objects)
        go(self.s, show)                                   # the item leaves the air (show over)
        self.assertEqual(pub.unpublish(self.s, vid, backend=self.mem).storage, "deleted")

    def test_the_object_is_not_deleted_if_the_item_went_on_air_after_the_withdraw(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        show = open_show(self.s, "S")
        item = add_item(self.s, show, "a")
        attach(self.s, item, self.span)
        real = pub._on_air_with
        calls = []

        def air_after_withdraw(store, span_id):
            calls.append(1)
            if len(calls) == 1:                            # the check just before the delete: a take lands first
                go(self.new_store(), show)
            return real(store, span_id)

        with mock.patch.object(pub, "_on_air_with", air_after_withdraw):
            out = pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual(out.storage, "pending_on_air")
        self.assertTrue(self.mem.objects)
        self.assertEqual(self.mem.deleted, [])

    def test_refused_while_a_writer_holds_the_version(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        job = self.s.enqueue(self.asset.id, "publish", f"publish:{self.span}:house")
        self.s.conn.execute(
            "UPDATE job SET status = 'running', claimed_by = 'w', lease_expires_at = ? WHERE id = ?",
            (pub._stamp(600), job.id))
        self.s.conn.commit()
        with self.assertRaises(pub.UnpublishError) as raised:
            pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual(raised.exception.code, "publish_busy")

    def test_unknown_version(self):
        with self.assertRaises(KeyError):
            pub.unpublish(self.s, "nope", backend=self.mem)

    def test_republish_after_withdrawal_brings_the_version_back(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        pub.unpublish(self.s, vid, backend=self.mem)

        def render(store, span, kind):
            return self.mem.put(f"{span['asset_id']}/{span['id']}-{kind}.mp4")

        versions = pub.publish(self.s, self.span, renderer=render)
        house = [v for v in versions if v.kind == "house"][0]
        self.assertEqual((house.id, house.status), (vid, "done"))
        row = _row(self.s, vid)
        self.assertEqual((row["status"], row["withdrawn_at"], row["storage_deleted_at"]), ("done", None, None))

    def test_delete_blocker_tells_active_from_withdrawn(self):
        vid = _version(self.s, self.mem, self.asset.id, self.span)
        why = retention.blockers(self.s, self.asset.id)
        self.assertEqual(len(why), 1)
        self.assertIn("active publish version", why[0])
        pub.unpublish(self.s, vid, backend=self.mem)
        self.assertEqual(retention.blockers(self.s, self.asset.id), [])
        self.assertTrue(retention.tombstone(self.s, self.asset.id))


class UnpublishSqlite(UnpublishTests, SqliteBase):
    pass


class UnpublishPg(UnpublishTests, PgBase):
    pass


class AttachRaceTests:
    def _show(self):
        self.a, self.b = self.new_store(), self.new_store()
        asset, self.span1 = _cleared_span(self.a)
        self.span2 = self.a.add_span(asset.id, "transcript", "01:00:03:00", "01:00:05:00", text="other")
        self.show = open_show(self.a, "Race")
        self.item = add_item(self.a, self.show, "x")
        attach(self.a, self.item, self.span1)

    def _span(self):
        return self.a.conn.execute("SELECT span_id, status FROM item WHERE id = ?", (self.item,)).fetchone()

    def test_an_attach_that_checked_ready_cannot_change_the_media_of_an_item_that_went_on_air(self):
        """act_attach reads the item; a take on another connection lands before the attach writes."""
        self._show()
        real = serve.attach

        def take_lands_first(store, item_id, span_id):
            go(self.b, self.show)
            return real(store, item_id, span_id)

        with mock.patch.object(serve, "attach", take_lands_first):
            with self.assertRaises(serve.ApiError) as raised:
                serve.act_attach(self.a, self.item, {"span": self.span2})
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "not_attachable"))
        row = self._span()
        self.assertEqual((row["status"], row["span_id"]), ("on air", self.span1))

    def test_attach_refuses_an_item_that_is_not_ready(self):
        self._show()
        go(self.b, self.show)
        with self.assertRaises(ItemNotReady):
            attach(self.a, self.item, self.span2)
        self.assertEqual(self._span()["span_id"], self.span1)

    def test_attach_and_take_racing_on_threads_never_change_the_aired_media(self):
        """Attach and go run on separate connections, released together, many times. Whatever the order, the span
        that was on air when the item went to air is the span the item has afterwards."""
        real_take = playout.take
        for _ in range(12):
            self._show()
            aired = {}

            def spy(store, item_id):
                aired["span"] = store.conn.execute(
                    "SELECT span_id FROM item WHERE id = ?", (item_id,)).fetchone()["span_id"]
                return real_take(store, item_id)

            barrier = threading.Barrier(2)

            def go_when_ready(store):
                barrier.wait(10)
                return advance(store, self.show)

            def attach_when_ready(store):
                barrier.wait(10)
                return _try_attach(store, self.item, self.span2)

            with mock.patch.object(playout, "take", spy):
                t_attach = self.in_thread(attach_when_ready)
                t_go = self.in_thread(go_when_ready)
                t_attach.release()
                t_go.release()
                attached = t_attach.finish()
                t_go.finish()
            row = self._span()
            self.assertEqual(row["status"], "on air")
            self.assertEqual(row["span_id"], aired["span"], "the item's media changed after it went on air")
            self.assertEqual(aired["span"], self.span2 if attached else self.span1)


def _try_attach(store, item, span):
    try:
        attach(store, item, span)
    except ItemNotReady:
        return False
    return True


class AttachRaceSqlite(AttachRaceTests, SqliteBase):
    pass


class AttachRacePg(AttachRaceTests, PgBase):
    pass


# ---- HTTP -----------------------------------------------------------------------------------------

class ApiTests(base.Api):
    def setUp(self):
        super().setUp()
        self.mem = MemBackend()
        self._saved_backend = serve.UNPUBLISH_BACKEND
        serve.UNPUBLISH_BACKEND = self.mem
        self.asset, self.span = _cleared_span(self.store)

    def tearDown(self):
        serve.UNPUBLISH_BACKEND = self._saved_backend
        super().tearDown()

    def test_unpublish_endpoint(self):
        vid = _version(self.store, self.mem, self.asset.id, self.span)
        self.refused(404, "not_found", "/api/versions/nope/unpublish")
        self.refused(422, "bad_input", f"/api/versions/{vid}/unpublish", {"x": 1})
        s, p = self.post(f"/api/versions/{vid}/unpublish")
        self.assertEqual(s, 200, p)
        self.assertEqual((p["version"]["status"], p["storage"], p["alreadyWithdrawn"]), ("withdrawn", "deleted", False))
        s, p = self.post(f"/api/versions/{vid}/unpublish")
        self.assertEqual((s, p["alreadyWithdrawn"], p["storage"]), (200, True, "already_deleted"))
        state = self.call("GET", "/api/state")[1]
        spans = [sp for sp in state["spans"] if sp["id"] == self.span]
        self.assertEqual([v["status"] for v in spans[0]["versions"]], ["withdrawn"])
        self.assertIsNone(spans[0]["versions"][0]["url"])

    def test_unpublish_needs_the_token_when_one_is_set(self):
        vid = _version(self.store, self.mem, self.asset.id, self.span)
        serve.TOKEN = "t" * 24
        self.refused(401, "unauthorized", f"/api/versions/{vid}/unpublish")
        s, _ = self.post(f"/api/versions/{vid}/unpublish", token="t" * 24)
        self.assertEqual(s, 200)

    def test_unpublish_on_air_and_storage_failure(self):
        vid = _version(self.store, self.mem, self.asset.id, self.span)
        show = open_show(self.store, "S")
        item = add_item(self.store, show, "a")
        attach(self.store, item, self.span)
        go(self.store, show)
        p = self.refused(409, "on_air", f"/api/versions/{vid}/unpublish")
        self.assertEqual(p["items"][0]["id"], item)
        go(self.store, show)
        self.mem.fail_delete = True
        p = self.refused(502, "storage_failed", f"/api/versions/{vid}/unpublish")
        self.assertTrue(p["withdrawn"])
        self.mem.fail_delete = False
        s, p = self.post(f"/api/versions/{vid}/unpublish")
        self.assertEqual((s, p["storage"]), (200, "deleted"))

    def test_asset_delete_refusal_names_active_versions_and_allows_withdrawn(self):
        vid = _version(self.store, self.mem, self.asset.id, self.span)
        p = self.refused(409, "delete_refused", f"/api/assets/{self.asset.id}/delete")
        self.assertIn("active publish version", p["reasons"][0])
        self.post(f"/api/versions/{vid}/unpublish")
        s, p = self.post(f"/api/assets/{self.asset.id}/delete")
        self.assertEqual(s, 200, p)

    def test_delete_show(self):
        show = open_show(self.store, "Gone")
        keep = open_show(self.store, "Keep")
        a, b, c = (add_item(self.store, show, n) for n in "abc")
        add_item(self.store, keep, "k")
        attach(self.store, a, self.span)
        self.store.conn.execute("UPDATE item SET status = 'killed' WHERE id = ?", (b,))
        self.store.conn.execute("UPDATE item SET status = 'skipped' WHERE id = ?", (c,))
        self.store.conn.commit()
        self.post(f"/api/items/{a}/cues", {"command": "record.start"})
        self.refused(404, "not_found", "/api/shows/nope/delete")
        self.refused(422, "bad_input", f"/api/shows/{show}/delete", {"x": 1})
        s, p = self.post(f"/api/shows/{show}/delete")
        self.assertEqual((s, p["removedItems"], p["removedCues"]), (200, 3, 1), p)
        q = self.store.conn.execute
        self.assertIsNone(q("SELECT 1 FROM show WHERE id = ?", (show,)).fetchone())
        self.assertEqual(q("SELECT COUNT(*) FROM item WHERE show_id = ?", (show,)).fetchone()[0], 0)
        self.assertIsNotNone(q("SELECT 1 FROM show WHERE id = ?", (keep,)).fetchone())
        self.assertEqual(q("SELECT COUNT(*) FROM item").fetchone()[0], 1)
        self.assertEqual(q("SELECT COUNT(*) FROM span WHERE id = ?", (self.span,)).fetchone()[0], 1)
        self.assertEqual(q("SELECT COUNT(*) FROM asset WHERE id = ?", (self.asset.id,)).fetchone()[0], 1)
        self.refused(404, "not_found", f"/api/shows/{show}/delete")

    def test_delete_show_refuses_on_air_and_aired_and_changes_nothing(self):
        show = open_show(self.store, "Played")
        a, b = add_item(self.store, show, "a"), add_item(self.store, show, "b")
        attach(self.store, a, self.span)
        attach(self.store, b, self.span)
        go(self.store, show)
        p = self.refused(409, "show_not_deletable", f"/api/shows/{show}/delete")      # a on air
        self.assertEqual(p["items"], [{"id": a, "status": "on air"}])
        go(self.store, show)
        self.refused(409, "show_not_deletable", f"/api/shows/{show}/delete")          # a aired, b on air
        go(self.store, show)
        self.refused(409, "show_not_deletable", f"/api/shows/{show}/delete")          # both aired
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ?", (show,)).fetchone()[0], 2)

    def test_attach_endpoint_refuses_a_non_ready_item(self):
        show = open_show(self.store, "S")
        item = add_item(self.store, show, "a")
        attach(self.store, item, self.span)
        go(self.store, show)
        other = self.store.add_span(self.asset.id, "transcript", "01:00:03:00", "01:00:05:00", text="z")
        self.refused(409, "not_attachable", f"/api/items/{item}/attach", {"span": other})


class ShowDeleteRaceTests:
    def test_a_go_and_a_show_delete_get_one_outcome(self):
        a, b = self.new_store(), self.new_store()
        _, span = _cleared_span(a)
        show = open_show(a, "R")
        item = add_item(a, show, "x")
        attach(a, item, span)
        take_first = {"done": False}
        orig = building.show_lock

        def lock_after_take(show_id):
            if not take_first["done"]:                     # delete_show has checked the show exists; a go lands now
                take_first["done"] = True
                go(b, show)
            return orig(show_id)

        with mock.patch.object(building, "show_lock", lock_after_take):
            with self.assertRaises(building.BuildError) as raised:
                building.delete_show(a, show)
        self.assertEqual(raised.exception.code, "show_not_deletable")
        self.assertEqual(a.conn.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()["status"], "on air")


class ShowDeleteRaceSqlite(ShowDeleteRaceTests, SqliteBase):
    pass


class ShowDeleteRacePg(ShowDeleteRaceTests, PgBase):
    pass


if __name__ == "__main__":
    unittest.main()
