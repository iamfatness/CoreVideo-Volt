"""Write API for shot and clip spans (slice 4 of docs/specs/shot-spans.md): create, trim, label, delete over HTTP.

The rules themselves are tested in test_shots.py; this file tests what the API adds: the body contract, the stable
codes and statuses, that a refusal writes nothing, auth, rights, and a race. Run on SQLite always and on Postgres
when VOLT_TEST_PG_DSN is set."""

import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import serve, shots
from volt.rundown import add_item, attach
from volt.store import Store

from tests.pgutil import PgCase

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
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self.target, None, self.dir / "out"
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)   # port 0: the OS picks a free one
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        self.a = self.asset()

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers
    def asset(self, rate="25", start="01:00:00:00", duration=250, cleared=True):
        a = self.store.mint_asset(owner="desk", camera="iso-3")
        if cleared:
            self.store.set_cleared(a.id, True)
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=duration)
        return a

    def post(self, path, body=None, token=None, headers=None, raw=None, ctype="application/json"):
        h = dict(headers or {})
        data = raw if raw is not None else json.dumps({} if body is None else body).encode()
        h["Content-Type"] = ctype
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("POST", path, body=data, headers=h)
            r = conn.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
        finally:
            conn.close()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def refused(self, status, code, path, body=None, **kw):
        s, p = self.post(path, body, **kw)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        self.assertTrue(p["error"]["reason"])
        return p

    def create(self, kind="shot", tc_in="01:00:00:00", tc_out="01:00:02:00", asset=None, **extra):
        s, p = self.post(f"/api/assets/{(asset or self.a).id}/spans", {"kind": kind, "tc_in": tc_in, "tc_out": tc_out, **extra})
        self.assertEqual(s, 200, p)
        return p["span"]

    def rows(self, asset=None):
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT id, kind, tc_in, tc_out, frame_in, frame_out, label FROM span WHERE asset_id = ? ORDER BY id",
            ((asset or self.a).id,))]

    def use_on_item(self, span_id):
        self.store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Show', '2026-01-01T00:00:00Z')")
        self.store.conn.commit()
        attach(self.store, add_item(self.store, "s1", "slug"), span_id)

    def use_on_version(self, asset, span_id):
        self.store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at) "
            "VALUES (?, ?, ?, 'house', 'done', '2026-01-01T00:00:00Z')", (f"v-{span_id}", asset.id, span_id))
        self.store.conn.commit()


class Create(Behaviour):
    def test_a_clip_and_a_shot_come_back_with_frames_origin_and_rights_words(self):
        clip = self.create("clip", "01:00:01:00", "01:00:03:12", label="  Reaction ")
        self.assertEqual((clip["kind"], clip["origin"], clip["label"]), ("clip", "manual", "Reaction"))
        self.assertEqual((clip["tcIn"], clip["tcOut"]), ("01:00:01:00", "01:00:03:12"))
        self.assertEqual((clip["frameIn"], clip["frameOut"]), (3600 * 25 + 25, 3600 * 25 + 87))
        self.assertEqual(clip["usedBy"], {"items": 0, "versions": 0})
        self.assertIsNone(clip["blockedBy"])
        shot = self.create("shot", "01:00:00:00", "01:00:01:00")
        self.assertIsNone(shot["label"])
        # and the read API sees both, in frame order
        s, listing = self.get(f"/api/assets/{self.a.id}/spans")
        self.assertEqual([x["id"] for x in listing["spans"]], [shot["id"], clip["id"]])

    def test_marking_is_allowed_on_an_uncleared_asset_and_on_legal_hold_and_says_so(self):
        raw = self.asset(cleared=False)
        span = self.create("shot", asset=raw)
        self.assertEqual(span["blockedBy"], "not cleared")
        held = self.asset(cleared=True)
        self.store.set_hold(held.id, True)
        span = self.create("clip", asset=held)
        self.assertEqual(span["blockedBy"], "legal hold")

    def test_drop_frame_labels_are_kept_in_the_essence_style(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", duration=3600)
        span = self.create("clip", "01:00:59;28", "01:01:00;04", asset=a)
        self.assertEqual((span["tcIn"], span["tcOut"]), ("01:00:59;28", "01:01:00;04"))
        self.assertEqual(span["frameOut"] - span["frameIn"], 4)         # labels ;00 and ;01 do not exist, so 59;28 to 00;04 is 4 frames
        self.refused(422, "bad_timecode", f"/api/assets/{a.id}/spans",
                     {"kind": "clip", "tc_in": "01:01:00;00", "tc_out": "01:01:05;00"})

    def test_refusals_have_stable_codes_and_write_nothing(self):
        path = f"/api/assets/{self.a.id}/spans"
        ok = {"kind": "shot", "tc_in": "01:00:00:00", "tc_out": "01:00:02:00"}
        self.create("shot", "01:00:02:00", "01:00:04:00")
        before = self.rows()
        self.refused(404, "not_found", "/api/assets/nope/spans", ok)
        self.refused(422, "bad_input", path, {**ok, "kind": "marker"})
        self.refused(422, "bad_input", path, {**ok, "kind": "transcript"})
        self.refused(422, "bad_input", path, {"tc_in": ok["tc_in"], "tc_out": ok["tc_out"]})            # kind missing
        self.refused(422, "bad_input", path, {"kind": "shot", "tc_out": ok["tc_out"]})                 # tc_in missing
        self.refused(422, "bad_input", path, {**ok, "tc_in": 100})                                      # not a string
        self.refused(422, "bad_input", path, {**ok, "tc_in": "01:00:03:00", "tc_out": "01:00:02:00"})   # out before in
        self.refused(422, "bad_input", path, {**ok, "tc_out": ok["tc_in"]})                             # empty range
        self.refused(422, "bad_input", path, {**ok, "label": "x" * 201})
        self.refused(422, "bad_input", path, {**ok, "label": 7})
        self.refused(422, "bad_input", path, {**ok, "from_words": ["a", "b"]})                          # from_words needs no tc_in/tc_out
        self.refused(422, "bad_input", path, {**ok, "origin": "detect"})                                # the server decides
        self.refused(422, "bad_timecode", path, {**ok, "tc_in": "01:00:00:25"})
        self.refused(422, "bad_timecode", path, {**ok, "tc_in": "nonsense"})
        self.refused(422, "outside_source", path, {**ok, "tc_in": "00:59:59:24"})
        self.refused(422, "outside_source", path, {**ok, "tc_out": "01:00:10:01"})                       # past 250 frames
        self.refused(409, "overlaps_shot", path, {**ok, "tc_out": "01:00:03:00"})
        self.assertEqual(self.rows(), before)

    def test_an_overlap_is_refused_for_a_shot_but_fine_for_a_clip(self):
        self.create("shot", "01:00:02:00", "01:00:04:00")
        self.refused(409, "overlaps_shot", f"/api/assets/{self.a.id}/spans",
                     {"kind": "shot", "tc_in": "01:00:03:00", "tc_out": "01:00:05:00"})
        self.create("clip", "01:00:03:00", "01:00:05:00")
        self.create("shot", "01:00:04:00", "01:00:05:00")         # adjacent is not overlapping

    def test_an_asset_with_no_usable_timebase_is_refused(self):
        bare = self.store.mint_asset(owner="desk")
        self.refused(422, "bad_input", f"/api/assets/{bare.id}/spans",
                     {"kind": "clip", "tc_in": "00:00:00:00", "tc_out": "00:00:01:00"})

    def test_two_operators_marking_overlapping_shots_leave_exactly_one(self):
        path = f"/api/assets/{self.a.id}/spans"
        bodies = [{"kind": "shot", "tc_in": "01:00:00:00", "tc_out": f"01:00:0{n}:00"} for n in range(2, 8)]
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda b: self.post(path, b), bodies))
        won = [r for r in results if r[0] == 200]
        self.assertEqual(len(won), 1, results)
        self.assertEqual({r[1]["error"]["code"] for r in results if r[0] != 200}, {"overlaps_shot"})
        self.assertEqual(len(self.rows()), 1)


class Trim(Behaviour):
    def test_trim_moves_one_or_both_ends(self):
        span = self.create("shot", "01:00:01:00", "01:00:03:00")
        s, p = self.post(f"/api/spans/{span['id']}/trim", {"tc_out": "01:00:04:00"})
        self.assertEqual((s, p["span"]["tcIn"], p["span"]["tcOut"], p["span"]["frameOut"] - p["span"]["frameIn"]),
                         (200, "01:00:01:00", "01:00:04:00", 75))
        s, p = self.post(f"/api/spans/{span['id']}/trim", {"tc_in": "01:00:00:10", "tc_out": "01:00:01:00"})
        self.assertEqual((p["span"]["tcIn"], p["span"]["tcOut"]), ("01:00:00:10", "01:00:01:00"))
        self.assertEqual(p["span"]["id"], span["id"])

    def test_trim_refusals_change_nothing(self):
        a = self.create("shot", "01:00:01:00", "01:00:03:00")
        b = self.create("shot", "01:00:04:00", "01:00:06:00")
        before = self.rows()
        path = f"/api/spans/{a['id']}/trim"
        self.refused(404, "not_found", "/api/spans/nope/trim", {"tc_in": "01:00:01:00"})
        self.refused(422, "bad_input", path, {})                                           # nothing to change
        self.refused(422, "bad_input", path, {"tc_in": 5})
        self.refused(422, "bad_input", path, {"label": "x"})                              # unknown field
        self.refused(422, "bad_input", path, {"tc_in": "01:00:05:00"})                    # in past out
        self.refused(422, "bad_timecode", path, {"tc_in": "01:00:00:99"})
        self.refused(422, "outside_source", path, {"tc_in": "00:59:00:00"})
        self.refused(422, "outside_source", path, {"tc_out": "01:00:11:00"})
        self.refused(409, "overlaps_shot", path, {"tc_out": "01:00:04:12"})
        self.assertEqual(self.rows(), before)
        # a clip may be trimmed over anything
        clip = self.create("clip", "01:00:01:00", "01:00:02:00")
        s, p = self.post(f"/api/spans/{clip['id']}/trim", {"tc_out": "01:00:05:00"})
        self.assertEqual((s, p["span"]["tcOut"]), (200, "01:00:05:00"))
        self.assertTrue(b)

    def test_a_span_in_use_cannot_be_trimmed(self):
        on_item = self.create("shot", "01:00:01:00", "01:00:03:00")
        on_version = self.create("clip", "01:00:04:00", "01:00:05:00")
        self.use_on_item(on_item["id"])
        self.use_on_version(self.a, on_version["id"])
        before = self.rows()
        p = self.refused(409, "span_in_use", f"/api/spans/{on_item['id']}/trim", {"tc_out": "01:00:04:00"})
        self.assertIn("1 rundown item", p["error"]["reason"])
        self.refused(409, "span_in_use", f"/api/spans/{on_version['id']}/trim", {"tc_in": "01:00:04:10"})
        self.assertEqual(self.rows(), before)

    def test_a_transcript_span_is_not_editable(self):
        f = shots.facts(self.store, self.a.id)
        word = self.store.add_span(self.a.id, "transcript", f.label(f.start), f.label(f.start + 4), text="hi",
                                   origin="transcript", frame_in=f.start, frame_out=f.start + 4)
        self.refused(409, "not_editable", f"/api/spans/{word}/trim", {"tc_out": "01:00:01:00"})


class Label(Behaviour):
    def test_label_sets_changes_and_clears(self):
        span = self.create("clip")
        for sent, got in (("Bench", "Bench"), ("  Wide  ", "Wide"), (None, None), ("   ", None)):
            s, p = self.post(f"/api/spans/{span['id']}/label", {"label": sent})
            self.assertEqual((s, p["span"]["label"]), (200, got))
        self.assertEqual(self.rows()[0][-1], None)

    def test_label_refusals(self):
        span = self.create("clip", label="Keep")
        path = f"/api/spans/{span['id']}/label"
        self.refused(404, "not_found", "/api/spans/nope/label", {"label": "x"})
        self.refused(422, "bad_input", path, {})                           # missing key is not "clear"
        self.refused(422, "bad_input", path, {"label": 5})
        self.refused(422, "bad_input", path, {"label": "x" * 201})
        self.refused(422, "bad_input", path, {"label": "ok", "tc_in": "01:00:00:00"})
        self.assertEqual(self.rows()[0][-1], "Keep")

    def test_a_span_in_use_can_still_be_renamed(self):
        span = self.create("shot")
        self.use_on_item(span["id"])
        s, p = self.post(f"/api/spans/{span['id']}/label", {"label": "Renamed"})
        self.assertEqual((s, p["span"]["label"], p["span"]["usedBy"]["items"]), (200, "Renamed", 1))

    def test_label_works_on_a_transcript_span_too(self):
        f = shots.facts(self.store, self.a.id)
        word = self.store.add_span(self.a.id, "transcript", f.label(f.start), f.label(f.start + 4), text="hi",
                                   origin="transcript", frame_in=f.start, frame_out=f.start + 4)
        s, p = self.post(f"/api/spans/{word}/label", {"label": "Greeting"})
        self.assertEqual((s, p["span"]["label"]), (200, "Greeting"))


class Delete(Behaviour):
    def test_delete_removes_a_shot_and_a_clip(self):
        shot, clip = self.create("shot"), self.create("clip")
        for span in (shot, clip):
            s, p = self.post(f"/api/spans/{span['id']}/delete")
            self.assertEqual((s, p), (200, {"ok": True, "deleted": span["id"]}))
        self.assertEqual(self.rows(), [])
        self.refused(404, "not_found", f"/api/spans/{shot['id']}/delete")      # already gone

    def test_delete_refusals_change_nothing(self):
        on_item = self.create("shot", "01:00:00:00", "01:00:01:00")
        on_version = self.create("clip", "01:00:02:00", "01:00:03:00")
        self.use_on_item(on_item["id"])
        self.use_on_version(self.a, on_version["id"])
        f = shots.facts(self.store, self.a.id)
        word = self.store.add_span(self.a.id, "transcript", f.label(f.start), f.label(f.start + 4), text="hi",
                                   origin="transcript", frame_in=f.start, frame_out=f.start + 4)
        before = self.rows()
        self.refused(404, "not_found", "/api/spans/nope/delete")
        self.refused(409, "span_in_use", f"/api/spans/{on_item['id']}/delete")
        self.refused(409, "span_in_use", f"/api/spans/{on_version['id']}/delete")
        self.refused(409, "not_deletable", f"/api/spans/{word}/delete")
        self.refused(422, "bad_input", f"/api/spans/{on_item['id']}/delete", {"force": True})
        self.assertEqual(self.rows(), before)

    def test_delete_works_on_legal_hold_and_uncleared_assets(self):
        a = self.asset(cleared=False)
        self.store.set_hold(a.id, True)
        span = self.create("shot", asset=a)
        s, _ = self.post(f"/api/spans/{span['id']}/delete")
        self.assertEqual(s, 200)


class Gate(Behaviour):
    """The same auth and request rules as every other POST."""

    def test_a_token_is_required_when_one_is_set(self):
        serve.TOKEN = TOKEN
        path = f"/api/assets/{self.a.id}/spans"
        body = {"kind": "shot", "tc_in": "01:00:00:00", "tc_out": "01:00:01:00"}
        self.assertEqual(self.post(path, body)[0], 401)
        self.assertEqual(self.post(path, body, token="wrong" * 5)[0], 401)
        self.assertEqual(self.rows(), [])
        s, p = self.post(path, body, token=TOKEN)
        self.assertEqual(s, 200)
        for suffix, b in (("trim", {"tc_out": "01:00:02:00"}), ("label", {"label": "x"}), ("delete", {})):
            self.assertEqual(self.post(f"/api/spans/{p['span']['id']}/{suffix}", b)[0], 401, suffix)
        self.assertEqual(len(self.rows()), 1)

    def test_a_foreign_host_is_refused_without_a_token(self):
        s, p = self.post(f"/api/assets/{self.a.id}/spans", {"kind": "shot", "tc_in": "01:00:00:00",
                         "tc_out": "01:00:01:00"}, headers={"Host": "evil.example"})
        self.assertEqual((s, p["error"]["code"]), (403, "bad_host"))
        self.assertEqual(self.rows(), [])

    def test_json_only_and_no_garbage(self):
        path = f"/api/assets/{self.a.id}/spans"
        s, p = self.post(path, raw=b"kind=shot", ctype="application/x-www-form-urlencoded")
        self.assertEqual((s, p["error"]["code"]), (415, "unsupported_media_type"))
        s, p = self.post(path, raw=b"{not json")
        self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))
        s, p = self.post(path, raw=b"[1]")
        self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))
        self.assertEqual(self.rows(), [])


ALL = (Create, Trim, Label, Delete, Gate)


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
del Behaviour, Create, Trim, Label, Delete, Gate

if __name__ == "__main__":
    unittest.main()
