"""Show building over HTTP: create a show, edit its rundown, cue it, ingest a file, and the millisecond as-run.

Each behaviour class runs on SQLite, and on live Postgres when VOLT_TEST_PG_DSN is set (same pattern as
test_shot_writes.py). Rules under test live in volt/building.py; the page and the API only call them.
"""

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.pgutil import PgCase
from tests.test_playout import Clock
from volt import building, serve
from volt.cues import add_cue
from volt.library import index_words
from volt.playout import advance, go, rewind
from volt.rundown import add_item, attach, open_show
from volt.store import Store

TOKEN = "t" * 24


class Behaviour:
    """Provides self.open_store() and self.target (what serve.DB_PATH points at)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM = self.target, None, self.dir / "out", None
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)   # port 0: the OS picks a free one
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, serve.ROOM = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers
    def call(self, method, path, body=None, token=None, headers=None, raw=None, ctype="application/json"):
        h = dict(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            h["Content-Type"] = ctype
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=60)
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
        finally:
            conn.close()

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def ok(self, path, body=None):
        s, p = self.post(path, body)
        self.assertEqual((s, p["ok"]), (200, True), p)
        return p

    def refused(self, status, code, path, body=None, **kw):
        s, p = self.post(path, body, **kw)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        self.assertTrue(p["error"]["reason"])
        return p

    def new_show(self, title="Friday"):
        return self.ok("/api/shows", {"title": title})["show"]["id"]

    def new_items(self, show, *slugs):
        return [self.ok(f"/api/shows/{show}/items", {"slug": s})["item"] for s in slugs]

    def order(self, show):
        return [r["id"] for r in self.store.conn.execute(
            "SELECT id FROM item WHERE show_id = ? ORDER BY position, id", (show,))]

    def status(self, item):
        return self.store.conn.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()["status"]

    def span(self):
        a = self.store.mint_asset(owner="desk")
        self.store.set_cleared(a.id, True)
        index_words(self.store, a.id, [("w", "01:00:00:00", "01:00:01:00")])
        return self.store.conn.execute("SELECT id FROM span WHERE asset_id = ?", (a.id,)).fetchone()["id"]

    def air(self, show, n=1):
        """Put the first n items on air in turn (go), so the last is on air and the others aired."""
        for item in self.order(show):
            attach(self.store, item, self.span())
        for _ in range(n):
            self.ok(f"/api/shows/{show}/go")


class Shows(Behaviour):
    def test_create_a_show_and_see_it_in_state(self):
        p = self.ok("/api/shows", {"title": "  Friday night  "})
        show = p["show"]
        self.assertEqual(show["title"], "Friday night")
        self.assertFalse(p["existed"])
        self.assertEqual(p["rundown"]["items"], [])
        s, state = self.get(f"/api/state?show={show['id']}")
        self.assertEqual([x["id"] for x in state["shows"]], [show["id"]])
        self.assertEqual(state["orderVersion"], p["orderVersion"])

    def test_a_chosen_id_makes_a_retry_harmless(self):
        a = self.ok("/api/shows", {"title": "T", "id": "friday-1"})
        b = self.ok("/api/shows", {"title": "T", "id": "friday-1"})
        self.assertEqual((a["existed"], b["existed"]), (False, True))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM show").fetchone()[0], 1)
        self.refused(409, "id_conflict", "/api/shows", {"title": "Other", "id": "friday-1"})

    def test_bad_input_changes_nothing(self):
        for body in ({}, {"title": ""}, {"title": "   "}, {"title": 5}, {"title": "x" * 201},
                     {"title": "ok", "id": "has space"}, {"title": "ok", "id": "a" * 129},
                     {"title": "ok", "surprise": 1}):
            self.refused(422, "bad_input", "/api/shows", body)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM show").fetchone()[0], 0)

    def test_auth_json_and_size_rules(self):
        serve.TOKEN = TOKEN
        self.refused(401, "unauthorized", "/api/shows", {"title": "x"})
        self.refused(401, "unauthorized", "/api/shows", {"title": "x"}, token="wrong" * 6)
        self.assertEqual(self.post("/api/shows", {"title": "x"}, token=TOKEN)[0], 200)
        serve.TOKEN = None
        self.refused(403, "bad_host", "/api/shows", {"title": "x"}, headers={"Host": "evil.example"})
        s, p = self.post("/api/shows", raw=b"title=x", ctype="application/x-www-form-urlencoded")
        self.assertEqual((s, p["error"]["code"]), (415, "unsupported_media_type"))
        s, p = self.post("/api/shows", raw=b"{nope")
        self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))
        with mock.patch.object(serve, "MAX_BODY", 200):
            s, p = self.post("/api/shows", {"title": "x" * 190, "id": "y" * 100})
        self.assertEqual((s, p["error"]["code"]), (413, "too_large"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM show").fetchone()[0], 1)  # only the authorised one


class Items(Behaviour):
    def test_add_appends_in_order_and_returns_the_rundown(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "Open", "Interview", "Close")
        self.assertEqual(self.order(show), [a, b, c])
        p = self.ok(f"/api/shows/{show}/items", {"slug": "Extra", "script": "Say hello."})
        self.assertEqual([i["slug"] for i in p["rundown"]["items"]], ["Open", "Interview", "Close", "Extra"])
        self.assertEqual(p["rundown"]["items"][0]["status"], "ready")
        self.assertEqual(self.store.conn.execute("SELECT script FROM item WHERE id = ?", (p["item"],)).fetchone()[0],
                         "Say hello.")

    def test_a_chosen_id_is_idempotent_and_never_taken_over(self):
        show = self.new_show()
        other = self.new_show("Other")
        a = self.ok(f"/api/shows/{show}/items", {"slug": "Open", "id": "item-1"})
        b = self.ok(f"/api/shows/{show}/items", {"slug": "Open", "id": "item-1"})
        self.assertEqual((a["existed"], b["existed"], len(b["rundown"]["items"])), (False, True, 1))
        self.refused(409, "id_conflict", f"/api/shows/{show}/items", {"slug": "Different", "id": "item-1"})
        self.refused(409, "id_conflict", f"/api/shows/{other}/items", {"slug": "Open", "id": "item-1"})

    def test_refusals(self):
        show = self.new_show()
        self.refused(404, "not_found", "/api/shows/nope/items", {"slug": "x"})
        for body in ({}, {"slug": ""}, {"slug": "  "}, {"slug": 1}, {"slug": "x" * 201},
                     {"slug": "x", "script": 5}, {"slug": "x", "extra": 1}, {"slug": "x", "id": "bad id"}):
            self.refused(422, "bad_input", f"/api/shows/{show}/items", body)
        self.assertEqual(self.order(show), [])

    def test_concurrent_adds_get_distinct_positions(self):
        show = self.new_show()
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda n: self.post(f"/api/shows/{show}/items", {"slug": f"s{n}"}), range(8)))
        self.assertTrue(all(s == 200 for s, _ in results), results)
        positions = [r[0] for r in self.store.conn.execute("SELECT position FROM item WHERE show_id = ?", (show,))]
        self.assertEqual(sorted(positions), list(range(1, 9)))

    def test_rename(self):
        show = self.new_show()
        a, b = self.new_items(show, "Open", "Close")
        p = self.ok(f"/api/items/{a}/rename", {"slug": "  Cold open "})
        self.assertEqual(p["rundown"]["items"][0]["slug"], "Cold open")
        self.ok(f"/api/items/{a}/rename", {"script": "New words"})
        row = self.store.conn.execute("SELECT slug, script FROM item WHERE id = ?", (a,)).fetchone()
        self.assertEqual((row["slug"], row["script"]), ("Cold open", "New words"))
        self.refused(422, "bad_input", f"/api/items/{a}/rename", {})
        self.refused(422, "bad_input", f"/api/items/{a}/rename", {"slug": " "})
        self.refused(422, "bad_input", f"/api/items/{a}/rename", {"slug": "x", "other": 1})
        self.refused(404, "not_found", "/api/items/nope/rename", {"slug": "x"})

    def test_an_item_that_is_on_air_or_aired_cannot_be_renamed_removed_or_detached(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "Open", "Mid", "Close")
        self.air(show, n=2)                                           # a aired, b on air
        for item in (a, b):
            self.refused(409, "not_editable", f"/api/items/{item}/rename", {"slug": "x"})
            self.refused(409, "not_removable", f"/api/items/{item}/remove")
            self.refused(409, "not_attachable", f"/api/items/{item}/detach")
        self.assertEqual(self.order(show), [a, b, c])
        self.assertEqual([self.status(i) for i in (a, b, c)], ["aired", "on air", "ready"])

    def test_remove_takes_the_item_and_its_cues_out(self):
        show = self.new_show()
        a, b = self.new_items(show, "Open", "Close")
        self.ok(f"/api/items/{a}/cues", {"command": "overlay.take", "payload": "on"})
        p = self.ok(f"/api/items/{a}/remove")
        self.assertEqual([i["id"] for i in p["rundown"]["items"]], [b])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM cue").fetchone()[0], 0)
        self.refused(404, "not_found", f"/api/items/{a}/remove")
        self.refused(422, "bad_input", f"/api/items/{b}/remove", {"x": 1})

    def test_a_killed_item_can_be_removed_and_a_skipped_one_too(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "a", "b", "c")
        self.ok(f"/api/items/{a}/kill")
        self.ok(f"/api/items/{b}/skip")
        self.ok(f"/api/items/{a}/remove")
        self.ok(f"/api/items/{b}/remove")
        self.assertEqual(self.order(show), [c])

    def test_detach_and_attach(self):
        show = self.new_show()
        (a,) = self.new_items(show, "Open")
        span = self.span()
        self.ok(f"/api/items/{a}/attach", {"span": span})
        p = self.ok(f"/api/items/{a}/detach")
        self.assertTrue(p["wasAttached"])
        self.assertIsNone(self.store.conn.execute("SELECT span_id FROM item WHERE id = ?", (a,)).fetchone()[0])
        self.assertFalse(self.ok(f"/api/items/{a}/detach")["wasAttached"])        # nothing to take off: still a 200
        self.refused(404, "not_found", "/api/items/nope/detach")


class Reorder(Behaviour):
    def version(self, show):
        return self.get(f"/api/state?show={show}")[1]["orderVersion"]

    def test_reorder_with_the_current_version_moves_and_returns_a_new_one(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "a", "b", "c")
        v = self.version(show)
        p = self.ok(f"/api/shows/{show}/reorder", {"items": [c, a, b], "version": v})
        self.assertEqual(self.order(show), [c, a, b])
        self.assertEqual([i["id"] for i in p["rundown"]["items"]], [c, a, b])
        self.assertNotEqual(p["orderVersion"], v)
        self.assertEqual(p["orderVersion"], self.version(show))

    def test_a_stale_version_is_refused_and_nothing_moves(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "a", "b", "c")
        v = self.version(show)
        self.ok(f"/api/shows/{show}/reorder", {"items": [b, a, c], "version": v})      # operator one wins
        self.refused(409, "stale_order", f"/api/shows/{show}/reorder", {"items": [c, b, a], "version": v})
        self.assertEqual(self.order(show), [b, a, c])
        fresh = self.version(show)
        d = self.new_items(show, "d")[0]                                             # an add changes the version too
        self.refused(409, "stale_order", f"/api/shows/{show}/reorder", {"items": [a, b, c, d], "version": fresh})
        self.assertEqual(self.order(show), [b, a, c, d])

    def test_two_operators_with_one_version_exactly_one_wins(self):
        show = self.new_show()
        a, b, c = self.new_items(show, "a", "b", "c")
        v = self.version(show)
        wants = [[c, b, a], [b, c, a], [a, c, b], [c, a, b]]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda w: self.post(f"/api/shows/{show}/reorder", {"items": w, "version": v}), wants))
        self.assertEqual(sorted(s for s, _ in results), [200, 409, 409, 409], results)
        won = wants[[s for s, _ in results].index(200)]
        self.assertEqual(self.order(show), won)
        self.assertTrue(all(p["error"]["code"] == "stale_order" for s, p in results if s == 409))

    def test_without_a_version_it_still_works_for_older_clients(self):
        show = self.new_show()
        a, b = self.new_items(show, "a", "b")
        self.ok(f"/api/shows/{show}/reorder", {"items": [b, a]})
        self.assertEqual(self.order(show), [b, a])

    def test_the_list_must_be_exact(self):
        show = self.new_show()
        other = self.new_show("Other")
        a, b = self.new_items(show, "a", "b")
        (x,) = self.new_items(other, "x")
        for items in ([a], [a, a], [a, b, x], [a, "ghost"], "ab", [1, 2]):
            self.refused(422, "bad_input", f"/api/shows/{show}/reorder", {"items": items})
        self.refused(422, "bad_input", f"/api/shows/{show}/reorder", {"items": [a, b], "version": 5})
        self.refused(404, "not_found", "/api/shows/nope/reorder", {"items": []})
        self.assertEqual(self.order(show), [a, b])
        self.assertEqual(self.order(other), [x])

    def test_a_ready_item_cannot_move_above_what_is_on_air_or_has_aired(self):
        show = self.new_show()
        a, b, c, d = self.new_items(show, "a", "b", "c", "d")
        self.air(show, n=2)                                                          # a aired, b on air
        before = self.order(show)
        self.refused(409, "order_locked", f"/api/shows/{show}/reorder", {"items": [d, a, b, c]})
        self.refused(409, "order_locked", f"/api/shows/{show}/reorder", {"items": [b, a, c, d]})  # played items swap
        self.refused(409, "order_locked", f"/api/shows/{show}/reorder", {"items": [a, c, b, d]})  # c above on air
        self.assertEqual(self.order(show), before)
        self.ok(f"/api/shows/{show}/reorder", {"items": [a, b, d, c]})               # the ready ones can swap
        self.assertEqual(self.order(show), [a, b, d, c])
        self.ok(f"/api/shows/{show}/go")
        self.assertEqual(self.status(d), "on air")                                   # and go follows the new order


class Cues(Behaviour):
    def test_commands_say_which_have_a_verified_adapter(self):
        s, p = self.get("/api/cue-commands")
        self.assertEqual((s, p["ok"]), (200, True))
        by = {c["command"]: c["verified"] for c in p["commands"]}
        self.assertEqual(set(by), {"show-input.take", "overlay.take", "audio.snapshot", "record.arm",
                                   "record.start", "stream.start"})
        self.assertEqual({c for c, v in by.items() if v},
                         {"show-input.take", "overlay.take", "record.start", "stream.start"})
        self.assertEqual({c for c, v in by.items() if not v}, {"audio.snapshot", "record.arm"})
        self.assertEqual(self.get("/api/cue-commands?x=1")[1]["error"]["code"], "bad_input")

    def test_add_list_and_remove(self):
        show = self.new_show()
        (a,) = self.new_items(show, "Open")
        p = self.ok(f"/api/items/{a}/cues", {"command": "overlay.take", "payload": "on"})
        cue = p["cue"]
        self.assertEqual((cue["room"], cue["command"], cue["payload"], cue["status"], cue["verified"]),
                         ("corevideo.pro", "overlay.take", "on", "stored", True))
        self.assertNotIn("warning", p)
        second = self.ok(f"/api/items/{a}/cues", {"command": "stream.start"})["cue"]
        s, listing = self.get(f"/api/items/{a}/cues")
        self.assertEqual([c["id"] for c in listing["cues"]], [cue["id"], second["id"]])          # in position order
        self.assertEqual(len(listing["commands"]), 6)
        state = self.get(f"/api/state?show={show}")[1]
        self.assertEqual([c["command"] for c in state["cues"][a]], ["overlay.take", "stream.start"])
        r = self.ok(f"/api/cues/{cue['id']}/remove")
        self.assertEqual((r["removed"], r["item"]), (cue["id"], a))
        self.assertEqual([c["id"] for c in self.get(f"/api/items/{a}/cues")[1]["cues"]], [second["id"]])
        self.refused(404, "not_found", f"/api/cues/{cue['id']}/remove")

    def test_an_unverified_command_is_stored_with_a_warning(self):
        show = self.new_show()
        (a,) = self.new_items(show, "Open")
        p = self.ok(f"/api/items/{a}/cues", {"command": "audio.snapshot", "payload": "mix-1"})
        self.assertFalse(p["cue"]["verified"])
        self.assertIn("no verified adapter", p["warning"])

    def test_refusals(self):
        show = self.new_show()
        a, b = self.new_items(show, "Open", "Close")
        self.refused(422, "bad_command", f"/api/items/{a}/cues", {"command": "transport.nuke"})
        self.refused(422, "bad_input", f"/api/items/{a}/cues", {"command": "overlay.take", "room": "vmix"})
        for body in ({}, {"command": ""}, {"command": 5}, {"command": "overlay.take", "payload": 1},
                     {"command": "overlay.take", "payload": "x" * 2001}, {"command": "overlay.take", "x": 1}):
            self.refused(422, "bad_input", f"/api/items/{a}/cues", body)
        self.refused(404, "not_found", "/api/items/nope/cues", {"command": "overlay.take"})
        self.assertEqual(self.get("/api/items/nope/cues")[0], 404)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM cue").fetchone()[0], 0)

    def test_only_a_ready_item_takes_cue_edits_and_a_fired_cue_stays(self):
        show = self.new_show()
        a, b = self.new_items(show, "Open", "Close")
        keep = add_cue(self.store, b, "corevideo.pro", "overlay.take", "on")
        self.air(show, n=1)                                                           # a on air
        self.refused(409, "not_editable", f"/api/items/{a}/cues", {"command": "overlay.take"})
        on_air_cue = add_cue(self.store, a, "corevideo.pro", "stream.start", "")
        self.refused(409, "not_editable", f"/api/cues/{on_air_cue}/remove")
        self.store.conn.execute("UPDATE cue SET status = 'fired' WHERE id = ?", (keep,))
        self.store.conn.commit()
        self.refused(409, "cue_fired", f"/api/cues/{keep}/remove")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM cue").fetchone()[0], 2)

    def test_a_take_stops_at_an_unverified_cue_added_here(self):
        from volt.rooms import FakeRoom
        from tests.test_rooms import obs
        show = self.new_show()
        (a,) = self.new_items(show, "Open")
        attach(self.store, a, self.span())
        self.ok(f"/api/items/{a}/cues", {"command": "record.arm"})
        serve.ROOM = FakeRoom([obs("idle", session=None)])
        with mock.patch.dict(serve.POLL, attempts=1, interval=0, sleep=lambda _: None):
            s, p = self.post(f"/api/shows/{show}/take")
        self.assertEqual((s, p["error"]["code"]), (409, "cue_unsupported"))
        self.assertEqual(self.status(a), "ready")


class Ingest(Behaviour):
    def setUp(self):
        super().setUp()
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        self.outside = self.dir / "elsewhere"
        self.outside.mkdir()
        patch = mock.patch.dict(os.environ, {"VOLT_INGEST_ROOTS": str(self.inbox)})
        patch.start()
        self.addCleanup(patch.stop)

    def clip(self, path):
        subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2",
                               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def assets(self):
        return self.store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_a_file_in_an_allowed_folder_is_minted_quarantined_and_its_jobs_are_returned(self):
        card = self.inbox / "A002.mp4"
        self.clip(card)
        p = self.ok("/api/ingest", {"path": str(card), "camera": "iso-4", "show": "Friday"})
        self.assertEqual((p["status"], p["minted"], p["quarantined"]), ("done", True, False))
        self.assertTrue(p["essence"])
        self.assertEqual({j["type"] for j in p["jobs"]}, {"ingest", "proxy", "asr"})
        self.assertEqual(p["assetState"]["camera"], "iso-4")
        self.assertFalse(p["assetState"]["cleared"])                                   # ingest never clears
        row = self.store.conn.execute("SELECT location, frame_rate FROM essence WHERE asset_id = ?", (p["asset"],)).fetchone()
        self.assertIn("quarantine", row["location"])                                   # the copy, not the source
        self.assertTrue(Path(row["location"]).is_file())
        self.assertEqual(row["frame_rate"], "25")
        again = self.ok("/api/ingest", {"path": str(card)})                            # replay: same asset, nothing new
        self.assertEqual((again["asset"], again["minted"], self.assets()), (p["asset"], False, 1))

    @unittest.skipUnless(shutil.which("ffprobe"), "needs ffprobe")
    def test_a_file_that_does_not_parse_stays_in_quarantine(self):
        bad = self.inbox / "bad.mp4"
        bad.write_bytes(b"not media")
        p = self.ok("/api/ingest", {"path": str(bad)})
        self.assertEqual((p["status"], p["quarantined"], p["essence"]), ("quarantine", True, None))
        self.assertEqual([j["status"] for j in p["jobs"]], ["failed"])

    def test_a_full_disk_is_disk_low_not_a_missing_file(self):
        from volt import disk
        card = self.inbox / "big.mp4"
        card.write_bytes(b"x")
        with mock.patch("volt.ingest.disk.require_space", side_effect=disk.DiskLowError("only 1 GB free, need 4 GB")):
            p = self.refused(507, "disk_low", "/api/ingest", {"path": str(card)})
        self.assertIn("1 GB", p["error"]["reason"])
        self.assertEqual(self.assets(), 0)

    def test_state_lists_the_unfinished_jobs_of_an_asset_and_drops_them_when_done(self):
        a = self.store.mint_asset(owner="desk", camera="iso-9")
        self.store.enqueue(a.id, "proxy", "k1")
        job = self.store.enqueue(a.id, "asr", "k2")
        self.store.complete_job(job.id)
        s, state = self.get("/api/state")
        mine = next(x for x in state["assets"] if x["id"] == a.id)
        self.assertEqual([(j["type"], j["status"]) for j in mine["jobs"]], [("proxy", "queued")])

    def test_only_the_allowed_folders_are_read(self):
        secret = self.outside / "secret.mp4"
        secret.write_bytes(b"x")
        inside = self.inbox / "ok.mp4"
        inside.write_bytes(b"x")
        for path in (secret, self.outside / "missing.mp4", self.inbox / ".." / "elsewhere" / "secret.mp4",
                     self.inbox.parent):
            p = self.refused(403, "path_not_allowed", "/api/ingest", {"path": str(path)})
            self.assertNotIn(str(path), p["error"]["reason"])             # existence and name are not echoed
        self.refused(422, "bad_input", "/api/ingest", {"path": "inbox/ok.mp4"})
        self.refused(422, "bad_input", "/api/ingest", {"path": str(inside) + "\x00.mp4"})
        self.refused(404, "not_found", "/api/ingest", {"path": str(self.inbox / "missing.mp4")})
        self.refused(404, "not_found", "/api/ingest", {"path": str(self.inbox)})        # a folder is not a file
        self.assertEqual(self.assets(), 0)

    def test_a_link_out_of_the_folder_is_refused(self):
        secret = self.outside / "secret.mp4"
        secret.write_bytes(b"x")
        link = self.inbox / "link.mp4"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.refused(403, "path_not_allowed", "/api/ingest", {"path": str(link)})
        self.assertEqual(self.assets(), 0)

    def test_prefix_siblings_are_not_inside(self):
        sibling = self.dir / "inbox-evil"
        sibling.mkdir()
        (sibling / "x.mp4").write_bytes(b"x")
        self.refused(403, "path_not_allowed", "/api/ingest", {"path": str(sibling / "x.mp4")})

    def test_no_configured_folder_means_no_ingest(self):
        with mock.patch.dict(os.environ, {"VOLT_INGEST_ROOTS": ""}):
            self.refused(503, "ingest_disabled", "/api/ingest", {"path": str(self.inbox / "x.mp4")})

    def test_bad_bodies(self):
        for body in ({}, {"path": ""}, {"path": 5}, {"path": "x" * 4097}, {"path": "/a", "other": 1}):
            self.refused(422, "bad_input", "/api/ingest", body)


class AsRun(Behaviour):
    def test_go_records_the_millisecond_and_keeps_the_second(self):
        show = self.new_show()
        a, b = self.new_items(show, "a", "b")
        for item in (a, b):
            attach(self.store, item, self.span())
        self.ok(f"/api/shows/{show}/go")
        p = self.ok(f"/api/shows/{show}/go")
        first, second = p["rundown"]["items"]
        for stamp in (first["asRunInPrecise"], first["asRunOutPrecise"], second["asRunInPrecise"]):
            self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(first["asRunOutPrecise"], second["asRunInPrecise"])             # one read of the clock
        self.assertEqual(first["asRunIn"], first["asRunInPrecise"][:19] + "Z")           # the old field is its prefix
        self.assertEqual(first["asRunOut"], first["asRunOutPrecise"][:19] + "Z")
        self.assertLessEqual(first["asRunInPrecise"], first["asRunOutPrecise"])
        self.assertIsNone(second["asRunOut"])
        row = self.store.conn.execute("SELECT as_run_in_precise, as_run_out_precise FROM item WHERE id = ?", (b,)).fetchone()
        self.assertEqual((row[0], row[1]), (second["asRunInPrecise"], None))

    def test_an_injected_clock_keeps_its_seconds_and_precise_falls_back_to_them(self):
        show = self.new_show()
        (a,) = self.new_items(show, "a")
        attach(self.store, a, self.span())
        go(self.store, show, Clock())
        item = self.get(f"/rundown?show={show}")[1]["items"][0]
        self.assertEqual((item["asRunIn"], item["asRunInPrecise"]), ("2026-10-10T20:00:01Z",) * 2)
        self.assertIsNone(self.store.conn.execute("SELECT as_run_in_precise FROM item").fetchone()[0])
        adv = advance(self.store, show, Clock(), precise_clock=lambda: "2026-10-10T20:00:07.250Z")
        row = self.store.conn.execute("SELECT as_run_out, as_run_out_precise FROM item WHERE id = ?", (a,)).fetchone()
        self.assertEqual((row[0], row[1]), ("2026-10-10T20:00:01Z", "2026-10-10T20:00:07.250Z"))

    def test_a_rewind_clears_the_millisecond_too(self):
        show = self.new_show()
        a, b = self.new_items(show, "a", "b")
        for item in (a, b):
            attach(self.store, item, self.span())
        advance(self.store, show)
        adv = advance(self.store, show)
        rewind(self.store, adv)
        rows = {r["id"]: r for r in self.store.conn.execute(
            "SELECT id, as_run_in_precise, as_run_out_precise FROM item")}
        self.assertIsNone(rows[b]["as_run_in_precise"])
        self.assertIsNone(rows[a]["as_run_out_precise"])
        self.assertIsNotNone(rows[a]["as_run_in_precise"])


ALL = (Shows, Items, Reorder, Cues, Ingest, AsRun)


class _Sqlite:
    def open_store(self):
        self.target = str(self.dir / "v.db")
        return Store(self.target)


class _Pg:
    def open_store(self):
        self.target = self.dsn
        return self.store()


def _build():
    g = globals()
    for base in ALL:
        g[f"Sqlite{base.__name__}"] = type(f"Sqlite{base.__name__}", (_Sqlite, base, unittest.TestCase), {})
        g[f"Postgres{base.__name__}"] = type(f"Postgres{base.__name__}", (_Pg, base, PgCase), {})


_build()
del Behaviour, Shows, Items, Reorder, Cues, Ingest, AsRun

if __name__ == "__main__":
    unittest.main()
