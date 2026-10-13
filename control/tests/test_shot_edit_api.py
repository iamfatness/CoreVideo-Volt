"""Detect, split, merge and the span-frames backfill over HTTP (slice 4b of docs/specs/shot-spans.md).

The rules are tested in test_shot_detect.py and test_shots.py; this file tests what the API adds: the body
contract, the stable codes and statuses, that a refusal writes nothing, that rights do not block, the job state
the page polls, and races. Run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set. The one test that
runs a real detection needs ffmpeg."""

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from volt import media, serve, shots
from volt.rundown import add_item, attach
from volt.runner import Config, Runner, default_handlers
from volt.shots import ShotError
from volt.store import Store

from tests.pgutil import PgCase
from tests.test_shot_detect import make_clip

TOKEN = "t" * 24


class Behaviour:
    """Provides self.open_store() and self.target (what serve.DB_PATH points at). Same shape as test_shot_writes."""

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

    def asset(self, rate="25", start="01:00:00:00", duration=2500, cleared=True, open_file=False):
        a = self.store.mint_asset(owner="desk", camera="iso-3")
        if cleared:
            self.store.set_cleared(a.id, True)
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=duration, open_file=open_file)
        return a

    def _call(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=headers or {})
            r = conn.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
        finally:
            conn.close()

    def post(self, path, body=None, token=None, headers=None):
        h = dict(headers or {})
        h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = f"Bearer {token}"
        return self._call("POST", path, json.dumps({} if body is None else body).encode(), h)

    def get(self, path):
        return self._call("GET", path)

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
            "SELECT id, kind, tc_in, tc_out, frame_in, frame_out, label, origin FROM span WHERE asset_id = ? ORDER BY frame_in, id",
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

    def jobs(self, asset=None, kind="shots"):
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT id, status, attempt, idempotency_key FROM job WHERE asset_id = ? AND type = ? ORDER BY id",
            ((asset or self.a).id, kind))]

    def drain(self):
        return Runner(self.store, default_handlers(Config(out_dir=self.dir / "out")), name="r1").drain()

    def header(self, asset=None):
        s, p = self.get(f"/api/assets/{(asset or self.a).id}/spans")
        self.assertEqual(s, 200)
        return p

    def detect(self, body=None, asset=None, **kw):
        return self.post(f"/api/assets/{(asset or self.a).id}/shots/detect", body, **kw)

    def shot(self, tc_in, tc_out, asset=None, **kw):
        return self.create("shot", tc_in, tc_out, asset=asset, **kw)


class Detect(Behaviour):
    def test_it_queues_one_job_and_the_page_can_read_its_state(self):
        s, p = self.detect()
        self.assertEqual((s, p["ok"], p["alreadyDone"]), (200, True, False))
        job = p["job"]
        self.assertEqual((job["status"], job["attempt"], job["threshold"], job["minFrames"]), ("queued", 1, 0.13, 12))
        self.assertIsNone(job["error"])
        self.assertEqual(self.jobs(), [(job["id"], "queued", 1, self.jobs()[0][3])])
        seen = self.header()["detect"]
        self.assertEqual((seen["id"], seen["status"]), (job["id"], "queued"))
        self.assertIsNone(self.header()["indexing"])

    def test_settings_ride_in_the_key_and_a_repeat_is_the_same_job(self):
        _, a = self.detect({"threshold": 0.2, "min_frames": 8})
        _, b = self.detect({"threshold": 0.2, "min_frames": 8})
        self.assertEqual(a["job"]["id"], b["job"]["id"])
        self.assertEqual((a["job"]["threshold"], a["job"]["minFrames"]), (0.2, 8))
        self.assertEqual(len(self.jobs()), 1)

    def test_a_refusal_queues_nothing(self):
        path = f"/api/assets/{self.a.id}/shots/detect"
        for body in ({"threshold": 0}, {"threshold": 1}, {"threshold": "high"}, {"threshold": True}, {"threshold": None, "min_frames": 0},
                     {"min_frames": 1.5}, {"min_frames": 10001}, {"min_frames": True}):
            self.refused(422, "bad_input", path, body)
        self.refused(422, "bad_input", path, {"force": True})
        self.refused(404, "not_found", "/api/assets/nope/shots/detect")
        bare = self.store.mint_asset(owner="desk", camera="iso-9")           # no hi-res essence at all
        self.refused(422, "bad_input", f"/api/assets/{bare.id}/shots/detect")
        live = self.asset(duration=None, open_file=True)
        self.refused(409, "essence_open", f"/api/assets/{live.id}/shots/detect")
        self.assertEqual((self.jobs(), self.jobs(bare), self.jobs(live)), ([], [], []))

    def test_a_missing_ffmpeg_is_a_503_with_its_own_code(self):
        with mock.patch.object(shots, "engine_version", side_effect=ShotError("engine_missing", "ffmpeg is not available")):
            self.refused(503, "engine_missing", f"/api/assets/{self.a.id}/shots/detect")
        self.assertEqual(self.jobs(), [])

    def test_legal_hold_and_not_cleared_do_not_block_it(self):
        a = self.asset(cleared=False)
        self.store.set_hold(a.id, True)
        s, p = self.detect(asset=a)
        self.assertEqual((s, p["job"]["status"]), (200, "queued"))

    def test_different_settings_while_one_is_active_are_refused_and_change_nothing(self):
        _, first = self.detect({"threshold": 0.2})
        p = self.refused(409, "detect_busy", f"/api/assets/{self.a.id}/shots/detect", {"threshold": 0.3})
        self.assertEqual(self.jobs(), [(first["job"]["id"], "queued", 1, self.jobs()[0][3])])
        self.assertIn("wait", p["error"]["reason"])
        # the same settings are not a conflict: it is that job
        self.assertEqual(self.detect({"threshold": 0.2})[1]["job"]["id"], first["job"]["id"])

    def test_once_a_run_is_over_other_settings_are_allowed_and_a_done_repeat_is_a_no_op(self):
        _, first = self.detect({"threshold": 0.2})
        self.store.complete_job(first["job"]["id"])
        s, again = self.detect({"threshold": 0.2})
        self.assertEqual((s, again["alreadyDone"], again["job"]["status"]), (200, True, "done"))
        s, other = self.detect({"threshold": 0.3})
        self.assertEqual((s, other["alreadyDone"]), (200, False))
        self.assertEqual(len(self.jobs()), 2)

    def test_a_failed_run_is_queued_again_and_the_page_can_read_why_it_failed(self):
        _, first = self.detect()
        self.store.fail_job(first["job"]["id"], "the hi-res file is not on disk")
        seen = self.header()["detect"]
        self.assertEqual((seen["status"], seen["error"]), ("failed", "the hi-res file is not on disk"))
        _, again = self.detect()
        self.assertEqual((again["job"]["id"], again["job"]["status"], again["job"]["attempt"]), (first["job"]["id"], "queued", 2))
        self.assertIsNone(again["job"]["error"])

    def test_a_runner_failure_shows_on_the_header(self):
        self.detect()                                    # the asset's hi-res file does not exist: the job fails for good
        self.drain()
        seen = self.header()["detect"]
        self.assertEqual(seen["status"], "failed")
        self.assertIn("hi-res file is not on disk", seen["error"])

    def test_six_clicks_with_the_same_settings_are_one_job(self):
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: self.detect({"threshold": 0.25}), range(6)))
        self.assertEqual({s for s, _ in results}, {200})
        self.assertEqual(len({p["job"]["id"] for _, p in results}), 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_six_clicks_with_different_settings_let_exactly_one_in(self):
        barrier = threading.Barrier(6)

        def go(n):
            barrier.wait()
            return self.detect({"threshold": round(0.1 + n * 0.05, 2)})
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(go, range(6)))
        self.assertEqual(sorted(s for s, _ in results), [200, 409, 409, 409, 409, 409])
        self.assertEqual({p["error"]["code"] for s, p in results if s == 409}, {"detect_busy"})
        self.assertEqual(len(self.jobs()), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg to build and read a clip")
    def test_a_runner_picks_it_up_and_the_page_sees_shots_and_a_finished_job(self):
        a = self.store.mint_asset(owner="desk", camera="iso-5")
        path = self.dir / "cuts.mp4"
        make_clip(path, "25", (60, 50, 70))
        self.store.add_essence(a.id, role="hi-res", location=str(path), frame_rate="25", timecode_start="01:00:00:00",
                               duration_frames=180)
        manual = self.shot("01:00:00:00", "01:00:02:00", asset=a)
        _, p = self.detect(asset=a)
        self.assertEqual(p["job"]["status"], "queued")
        self.assertEqual(self.drain(), {"done": 1})
        h = self.header(a)
        self.assertEqual((h["detect"]["status"], h["lastRun"]["job"]), ("done", p["job"]["id"]))
        self.assertEqual(h["lastRun"]["shotCount"], len([s for s in h["spans"] if s["origin"] == "detect"]))
        self.assertIn(manual["id"], [s["id"] for s in h["spans"]])      # the hand-made shot survives
        self.assertEqual(self.detect(asset=a)[1]["alreadyDone"], True)


class Split(Behaviour):
    def test_it_cuts_one_shot_into_two_manual_halves(self):
        a = self.shot("01:00:00:00", "01:00:10:00", label="Wide")
        s, p = self.post(f"/api/spans/{a['id']}/split", {"at": "01:00:04:00"})
        self.assertEqual(s, 200, p)
        left, right = p["spans"]
        self.assertEqual((left["id"], left["label"], left["tcIn"], left["tcOut"]), (a["id"], "Wide", "01:00:00:00", "01:00:04:00"))
        self.assertEqual((right["tcIn"], right["tcOut"], right["label"], right["origin"]), ("01:00:04:00", "01:00:10:00", None, "manual"))
        self.assertEqual(left["origin"], "manual")
        self.assertEqual(left["frameOut"], right["frameIn"])
        self.assertEqual(len(self.rows()), 2)

    def test_refusals_change_nothing(self):
        shot = self.shot("01:00:00:00", "01:00:10:00")
        clip = self.create("clip", "01:00:20:00", "01:00:22:00")
        before = self.rows()
        path = f"/api/spans/{shot['id']}/split"
        self.refused(422, "bad_input", path)                                          # at is required
        self.refused(422, "bad_input", path, {"at": ""})
        self.refused(422, "bad_input", path, {"at": 100})
        self.refused(422, "bad_input", path, {"at": "01:00:04:00", "force": True})
        self.refused(422, "bad_timecode", path, {"at": "nonsense"})
        self.refused(422, "bad_input", path, {"at": "01:00:00:00"})                   # an end is not inside
        self.refused(422, "bad_input", path, {"at": "01:00:10:00"})
        self.refused(422, "bad_input", path, {"at": "01:00:12:00"})                   # outside the shot
        self.refused(409, "not_editable", f"/api/spans/{clip['id']}/split", {"at": "01:00:21:00"})
        self.refused(404, "not_found", "/api/spans/nope/split", {"at": "01:00:04:00"})
        self.assertEqual(self.rows(), before)

    def test_a_shot_in_use_is_refused(self):
        shot = self.shot("01:00:00:00", "01:00:10:00")
        self.use_on_item(shot["id"])
        before = self.rows()
        self.refused(409, "span_in_use", f"/api/spans/{shot['id']}/split", {"at": "01:00:04:00"})
        self.assertEqual(self.rows(), before)

    def test_a_shot_whose_frames_are_not_indexed_yet_says_so(self):
        old = self.store.add_span(self.a.id, "shot", "01:00:00:00", "01:00:10:00")
        before = self.rows()
        self.refused(409, "indexing", f"/api/spans/{old}/split", {"at": "01:00:04:00"})
        self.assertEqual(self.rows(), before)

    def test_legal_hold_and_not_cleared_do_not_block_it(self):
        a = self.asset(cleared=False)
        self.store.set_hold(a.id, True)
        shot = self.shot("01:00:00:00", "01:00:10:00", asset=a)
        s, p = self.post(f"/api/spans/{shot['id']}/split", {"at": "01:00:04:00"})
        self.assertEqual(s, 200)
        self.assertEqual({x["blockedBy"] for x in p["spans"]}, {"legal hold"})

    def test_drop_frame_labels_are_kept_in_the_essences_style(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", duration=1800)
        shot = self.shot("01:00:00;00", "01:00:30;00", asset=a)
        s, p = self.post(f"/api/spans/{shot['id']}/split", {"at": "01:00:10:00"})     # a colon is accepted
        self.assertEqual(s, 200, p)
        self.assertEqual(p["spans"][0]["tcOut"], "01:00:10;00")

    def test_two_operators_splitting_one_shot_at_the_same_frame_cannot_both_win(self):
        shot = self.shot("01:00:00:00", "01:00:10:00")
        barrier = threading.Barrier(6)

        def go(_):
            barrier.wait()
            return self.post(f"/api/spans/{shot['id']}/split", {"at": "01:00:04:00"})
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(go, range(6)))
        self.assertEqual([r[0] for r in results].count(200), 1, results)
        self.assertTrue(all(r[0] in (409, 422) for r in results if r[0] != 200), results)
        rows = self.rows()
        self.assertEqual((len(rows), rows[0][3], rows[1][2]), (2, "01:00:04:00", "01:00:04:00"))   # the halves tile the shot


class Merge(Behaviour):
    def test_it_joins_two_neighbours_and_removes_the_second(self):
        a = self.shot("01:00:00:00", "01:00:04:00", label="Wide")
        b = self.shot("01:00:04:00", "01:00:10:00")
        s, p = self.post(f"/api/spans/{a['id']}/merge", {"with": b["id"]})
        self.assertEqual(s, 200, p)
        self.assertEqual((p["span"]["id"], p["span"]["label"], p["span"]["tcOut"], p["removed"]), (a["id"], "Wide", "01:00:10:00", b["id"]))
        self.assertEqual([r[0] for r in self.rows()], [a["id"]])

    def test_refusals_change_nothing(self):
        a = self.shot("01:00:00:00", "01:00:04:00")
        gap = self.shot("01:00:05:00", "01:00:08:00")
        clip = self.create("clip", "01:00:04:00", "01:00:05:00")
        elsewhere = self.asset()
        other = self.shot("01:00:04:00", "01:00:05:00", asset=elsewhere)
        before, other_before = self.rows(), self.rows(elsewhere)
        path = f"/api/spans/{a['id']}/merge"
        self.refused(422, "bad_input", path)
        self.refused(422, "bad_input", path, {"with": 5})
        self.refused(422, "bad_input", path, {"with": gap["id"], "force": True})
        self.refused(409, "not_adjacent", path, {"with": gap["id"]})                   # a gap is not adjacent
        self.refused(409, "not_adjacent", path, {"with": a["id"]})
        self.refused(409, "not_adjacent", path, {"with": other["id"]})                 # another asset's shot
        self.refused(409, "not_adjacent", f"/api/spans/{gap['id']}/merge", {"with": a["id"]})  # the wrong way round
        self.refused(409, "not_editable", path, {"with": clip["id"]})
        self.refused(404, "not_found", path, {"with": "nope"})
        self.refused(404, "not_found", "/api/spans/nope/merge", {"with": a["id"]})
        self.assertEqual((self.rows(), self.rows(elsewhere)), (before, other_before))

    def test_either_shot_in_use_is_refused_and_nothing_half_happens(self):
        a = self.shot("01:00:00:00", "01:00:04:00")
        b = self.shot("01:00:04:00", "01:00:10:00")
        c = self.shot("01:00:10:00", "01:00:12:00")
        self.use_on_item(b["id"])
        self.use_on_version(self.a, c["id"])
        before = self.rows()
        self.refused(409, "span_in_use", f"/api/spans/{a['id']}/merge", {"with": b["id"]})
        self.refused(409, "span_in_use", f"/api/spans/{b['id']}/merge", {"with": c["id"]})
        self.assertEqual(self.rows(), before)

    def test_legal_hold_and_not_cleared_do_not_block_it(self):
        x = self.asset(cleared=False)
        self.store.set_hold(x.id, True)
        a = self.shot("01:00:00:00", "01:00:04:00", asset=x)
        b = self.shot("01:00:04:00", "01:00:10:00", asset=x)
        s, p = self.post(f"/api/spans/{a['id']}/merge", {"with": b["id"]})
        self.assertEqual((s, p["span"]["blockedBy"]), (200, "legal hold"))

    def test_a_split_then_a_merge_gets_the_original_range_back(self):
        a = self.shot("01:00:00:00", "01:00:10:00", label="Wide")
        _, p = self.post(f"/api/spans/{a['id']}/split", {"at": "01:00:04:00"})
        s, m = self.post(f"/api/spans/{a['id']}/merge", {"with": p["spans"][1]["id"]})
        self.assertEqual((s, m["span"]["tcIn"], m["span"]["tcOut"], m["span"]["label"]), (200, "01:00:00:00", "01:00:10:00", "Wide"))
        self.assertEqual(len(self.rows()), 1)

    def test_two_merges_of_the_same_pair_cannot_both_win(self):
        a = self.shot("01:00:00:00", "01:00:04:00")
        b = self.shot("01:00:04:00", "01:00:10:00")
        barrier = threading.Barrier(4)

        def go(_):
            barrier.wait()
            return self.post(f"/api/spans/{a['id']}/merge", {"with": b["id"]})
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(go, range(4)))
        self.assertEqual(sorted(r[0] for r in results), [200, 404, 404, 404], results)
        self.assertEqual(len(self.rows()), 1)


class Indexing(Behaviour):
    def test_it_queues_the_backfill_once_and_the_page_can_watch_it(self):
        old = self.store.add_span(self.a.id, "shot", "01:00:00:00", "01:00:02:00")        # no frame columns
        h = self.header()
        self.assertEqual((h["pendingFrames"], h["spans"]), (1, []))                      # left out of the list until indexed
        self.assertIsNone(h["indexing"])
        s, p = self.post(f"/api/assets/{self.a.id}/span-frames")
        self.assertEqual((s, p["queued"], p["pendingFrames"], p["job"]["status"]), (200, True, 1, "queued"))
        s, again = self.post(f"/api/assets/{self.a.id}/span-frames")
        self.assertEqual(again["job"]["id"], p["job"]["id"])
        self.assertEqual(len(self.jobs(kind="span-frames")), 1)
        self.assertEqual(self.header()["indexing"]["status"], "queued")
        self.assertEqual(self.drain(), {"done": 1})
        h = self.header()
        self.assertEqual((h["pendingFrames"], h["indexing"]["status"], [x["id"] for x in h["spans"]]), (0, "done", [old]))

    def test_nothing_to_do_queues_nothing(self):
        self.shot("01:00:00:00", "01:00:02:00")
        s, p = self.post(f"/api/assets/{self.a.id}/span-frames")
        self.assertEqual((s, p["queued"], p["pendingFrames"], p["job"]), (200, False, 0, None))
        self.assertEqual(self.jobs(kind="span-frames"), [])

    def test_a_label_that_cannot_be_placed_ends_with_the_job_done_and_a_count_left(self):
        self.store.add_span(self.a.id, "shot", "01:00:00:00", "01:00:02:00")
        self.store.add_span(self.a.id, "shot", "01:00:03:30", "01:00:04:00")              # frame 30 does not exist at 25 fps
        self.post(f"/api/assets/{self.a.id}/span-frames")
        self.drain()
        h = self.header()
        self.assertEqual((h["pendingFrames"], h["indexing"]["status"]), (1, "done"))
        s, p = self.post(f"/api/assets/{self.a.id}/span-frames")                          # not queued again, no loop
        self.assertEqual((s, p["queued"], p["job"]["status"]), (200, True, "done"))
        self.assertEqual(len(self.jobs(kind="span-frames")), 1)

    def test_refusals(self):
        self.refused(404, "not_found", "/api/assets/nope/span-frames")
        self.refused(422, "bad_input", f"/api/assets/{self.a.id}/span-frames", {"x": 1})


class Gate(Behaviour):
    def test_a_token_is_required_on_every_new_route(self):
        serve.TOKEN = TOKEN
        a = self.store.add_span(self.a.id, "shot", "01:00:00:00", "01:00:10:00", frame_in=90000, frame_out=90250)
        for path, body in ((f"/api/assets/{self.a.id}/shots/detect", {}), (f"/api/assets/{self.a.id}/span-frames", {}),
                           (f"/api/spans/{a}/split", {"at": "01:00:04:00"}), (f"/api/spans/{a}/merge", {"with": "x"})):
            self.assertEqual(self.post(path, body)[0], 401, path)
            self.assertEqual(self.post(path, body, token="wrong" * 5)[0], 401, path)
        self.assertEqual((self.jobs(), self.jobs(kind="span-frames"), len(self.rows())), ([], [], 1))
        self.assertEqual(self.post(f"/api/assets/{self.a.id}/shots/detect", {}, token=TOKEN)[0], 200)

    def test_a_foreign_host_is_refused_without_a_token(self):
        s, p = self.detect(headers={"Host": "evil.example"})
        self.assertEqual((s, p["error"]["code"]), (403, "bad_host"))
        self.assertEqual(self.jobs(), [])


class Thumbnails(Behaviour):
    """media.py shares the frame rule, the path and the ffmpeg call with the detection job (volt.shots)."""

    def test_the_route_and_the_job_use_one_implementation(self):
        self.assertIs(media.thumb_frame, shots.thumb_frame)
        self.assertIs(media.thumb_path, shots.thumb_path)
        self.assertEqual((media.THUMB_WIDTH, media.THUMB_TIMEOUT), (shots.THUMB_WIDTH, shots.THUMB_TIMEOUT))
        with open(media.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("-frames:v", src)           # no second copy of the ffmpeg command
        self.assertNotIn("def thumb_frame", src)
        self.assertNotIn("def thumb_path", src)


ALL = (Detect, Split, Merge, Indexing, Gate, Thumbnails)


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
del Behaviour, Detect, Split, Merge, Indexing, Gate, Thumbnails

if __name__ == "__main__":
    unittest.main()
