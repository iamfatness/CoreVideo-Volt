"""Clips from transcript words (slice 5 of docs/specs/shot-spans.md): `from_words`, handles and snap.

The page sends the first and last word span IDs and never a time; this file tests that the SERVER computes the
range from the stored words and enforces the rule: words of one asset, kind transcript, in order, placed on frames,
a range inside the source. Each refusal also checks that nothing was written.

Run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set."""

import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import serve, shots
from volt.rundown import add_item, attach
from volt.speech import FakeEngine, Word, transcribe
from volt.store import Store

from tests.pgutil import PgCase

TOKEN = "t" * 24
WORDS = "And we are underway at the Dome and the home bench is loud".split()   # 13 words
STEP, LEN = 12, 10        # a word every 12 frames, each 10 frames long: word n is [start + 12n, start + 12n + 10)


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
        self.w = self.words(self.a)          # word spans of the asset, in order

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers
    def asset(self, rate="25", start="01:00:00:00", duration=1000, cleared=True):
        a = self.store.mint_asset(owner="desk", camera="iso-3")
        if cleared:
            self.store.set_cleared(a.id, True)
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=duration)
        return a

    def words(self, asset, texts=WORDS, first=None, placed=True):
        f = shots.facts(self.store, asset.id)
        base = f.start if first is None else first
        out = []
        for n, text in enumerate(texts):
            fi = base + STEP * n
            out.append(self.store.add_span(asset.id, "transcript", f.label(fi), f.label(fi + LEN), text=text, origin="transcript",
                                           frame_in=fi if placed else None, frame_out=fi + LEN if placed else None))
        return out

    def post(self, path, body=None, token=None, raw=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("POST", path, body=raw if raw is not None else json.dumps(body or {}).encode(), headers=h)
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

    def make(self, first, last, asset=None, expect=200, **extra):
        s, p = self.post(f"/api/assets/{(asset or self.a).id}/spans",
                         {"kind": "clip", "from_words": [first, last], **extra})
        self.assertEqual(s, expect, p)
        return p["span"] if expect == 200 else p

    def rows(self):
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT id, kind, tc_in, tc_out, frame_in, frame_out, text FROM span ORDER BY id")]

    def refused(self, status, code, body, asset=None):
        before = self.rows()
        s, p = self.post(f"/api/assets/{(asset or self.a).id}/spans", body)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        self.assertTrue(p["error"]["reason"])
        self.assertEqual(self.rows(), before, "a refusal must change nothing")
        return p

    def shot(self, tc_in, tc_out, asset=None):
        return shots.create(self.store, (asset or self.a).id, "shot", tc_in, tc_out)["id"]

    def start(self):
        return shots.facts(self.store, self.a.id).start


class FromWords(Behaviour):
    def test_the_range_is_the_first_words_in_to_the_last_words_out_and_the_text_is_the_words_between(self):
        c = self.make(self.w[3], self.w[7], label="  Underway  ")
        s = self.start()
        self.assertEqual((c["kind"], c["origin"], c["label"]), ("clip", "transcript", "Underway"))
        self.assertEqual((c["frameIn"], c["frameOut"]), (s + 36, s + 84 + LEN))
        self.assertEqual((c["tcIn"], c["tcOut"]), ("01:00:01:11", "01:00:03:19"))
        self.assertEqual(c["text"], "underway at the Dome and")
        self.assertEqual(c["usedBy"], {"items": 0, "versions": 0})
        self.assertIsNone(c["blockedBy"])

    def test_one_word_is_a_clip_of_that_word(self):
        c = self.make(self.w[6], self.w[6])
        self.assertEqual((c["frameIn"], c["frameOut"], c["text"]), (self.start() + 72, self.start() + 82, "Dome"))

    def test_handles_extend_the_range_in_frames(self):
        c = self.make(self.w[3], self.w[7], handle_in=5, handle_out=18)
        s = self.start()
        self.assertEqual((c["frameIn"], c["frameOut"]), (s + 31, s + 84 + LEN + 18))
        c = self.make(self.w[3], self.w[7], handle_in=0, handle_out=0)
        self.assertEqual((c["frameIn"], c["frameOut"]), (s + 36, s + 94))

    def test_the_client_cannot_send_a_time_with_words(self):
        for extra in ({"tc_in": "01:00:00:00"}, {"tc_out": "01:00:09:00"}):
            self.refused(422, "bad_input", {"kind": "clip", "from_words": [self.w[0], self.w[1]], **extra})

    def test_the_text_uses_the_stored_words_so_a_stale_page_cannot_misquote(self):
        c = self.make(self.w[0], self.w[2])
        self.assertEqual(c["text"], "And we are")
        # what the page showed does not matter: the row holds what the transcript held
        self.store.conn.execute("UPDATE span SET text = 'We' WHERE id = ?", (self.w[1],))
        self.store.conn.commit()
        self.assertEqual(self.make(self.w[0], self.w[2])["text"], "And We are")

    def test_words_of_another_asset_do_not_leak_into_the_text(self):
        other = self.asset()
        self.words(other, ["intruder"] * 13)
        self.assertNotIn("intruder", self.make(self.w[0], self.w[12])["text"])

    def test_the_clip_appears_in_the_spans_list_and_the_transcript_is_unchanged(self):
        c = self.make(self.w[1], self.w[2])
        _, listing = self.get(f"/api/assets/{self.a.id}/spans")
        self.assertEqual([(x["id"], x["origin"], x["text"]) for x in listing["spans"]], [(c["id"], "transcript", "we are")])
        _, tr = self.get(f"/api/assets/{self.a.id}/transcript?limit=100")
        self.assertEqual([x["id"] for x in tr["words"]], self.w)

    def test_a_clip_from_words_overlaps_shots_freely_and_a_second_clip_may_overlap_the_first(self):
        s = self.start()
        self.shot(shots.facts(self.store, self.a.id).label(s), shots.facts(self.store, self.a.id).label(s + 200))
        a = self.make(self.w[2], self.w[6])
        b = self.make(self.w[4], self.w[8])
        self.assertLess(b["frameIn"], a["frameOut"])

    def test_marking_is_allowed_on_an_uncleared_asset_and_on_legal_hold_and_says_so(self):
        raw = self.asset(cleared=False)
        w = self.words(raw)
        self.assertEqual(self.make(w[0], w[1], asset=raw)["blockedBy"], "not cleared")
        held = self.asset()
        self.store.set_hold(held.id, True)
        w = self.words(held)
        self.assertEqual(self.make(w[0], w[1], asset=held)["blockedBy"], "legal hold")

    def test_a_drop_frame_asset_keeps_its_labels(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", duration=4000)
        f = shots.facts(self.store, a.id)
        w = self.words(a, ["steal", "and", "go"], first=f.start + 1790)           # the minute boundary is at +1798
        c = self.make(w[0], w[2], asset=a)
        self.assertEqual((c["frameIn"], c["frameOut"]), (f.start + 1790, f.start + 1790 + 24 + LEN))
        self.assertEqual((c["tcIn"], c["tcOut"]), (f.label(f.start + 1790), f.label(f.start + 1790 + 24 + LEN)))
        self.assertIn(";", c["tcIn"] + c["tcOut"])

    def test_the_clip_is_a_real_span_it_can_be_attached_trimmed_and_deleted_like_any_clip(self):
        c = self.make(self.w[1], self.w[3])
        s, p = self.post(f"/api/spans/{c['id']}/trim", {"tc_out": "01:00:05:00"})
        self.assertEqual((s, p["span"]["tcOut"], p["span"]["origin"]), (200, "01:00:05:00", "transcript"))
        self.store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Show', '2026-01-01T00:00:00Z')")
        self.store.conn.commit()
        attach(self.store, add_item(self.store, "s1", "slug"), c["id"])
        self.assertEqual(self.post(f"/api/spans/{c['id']}/delete")[1]["error"]["code"], "span_in_use")


class Snap(Behaviour):
    def setUp(self):
        super().setUp()
        self.f = shots.facts(self.store, self.a.id)
        s = self.f.start
        # shot 1: [0, 100)  shot 2: [100, 300)  gap [300, 400)  shot 3: [400, 600)
        for lo, hi in ((0, 100), (100, 300), (400, 600)):
            self.shot(self.f.label(s + lo), self.f.label(s + hi))

    def at(self, frame, n=2):
        """Words at an exact frame (the 13 seeded words are at 0..144)."""
        return self.words(self.a, ["x"] * n, first=self.f.start + frame)

    def test_snap_widens_the_in_and_the_out_to_the_shots_that_hold_them(self):
        a = self.at(130, 3)                                  # words at 130, 142, 154 .. 164: wholly inside shot 2
        c = self.make(a[0], a[2], snap="shot")
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (100, 300))

    def test_snap_applies_after_handles_and_never_shrinks(self):
        a = self.at(90, 1)                                   # [90, 100) in shot 1
        c = self.make(a[0], a[0], snap="shot", handle_out=20)    # handle carries the out to 110: now in shot 2
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (0, 300))
        a = self.at(0, 1)                                    # [0, 10): in and out both in shot 1
        c = self.make(a[0], a[0], snap="shot")
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (0, 100))

    def test_an_out_exactly_on_a_cut_does_not_move(self):
        a = self.at(70, 1)                                   # [70, 80) + 20 = 100 = the cut
        c = self.make(a[0], a[0], snap="shot", handle_out=20)
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (0, 100))

    def test_an_end_in_a_gap_stays_where_it_is(self):
        a = self.at(310, 1)                                  # [310, 320) lies in the gap
        c = self.make(a[0], a[0], snap="shot")
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (310, 320))
        a = self.at(280, 1)                                  # in shot 2, out into the gap: only the in moves
        c = self.make(a[0], a[0], snap="shot", handle_out=50)
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (100, 340))

    def test_a_range_across_shots_takes_the_outer_ends(self):
        a = self.at(80, 1)
        b = self.at(450, 1)
        c = self.make(a[0], b[0], snap="shot")
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (0, 600))

    def test_snap_null_and_absent_are_the_same(self):
        a = self.at(130, 1)
        for extra in ({}, {"snap": None}):
            c = self.make(a[0], a[0], **extra)
            self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (130, 140))

    def test_with_no_shots_snap_changes_nothing(self):
        self.store.conn.execute("DELETE FROM span WHERE kind = 'shot'")
        self.store.conn.commit()
        c = self.make(self.w[2], self.w[3], snap="shot")
        self.assertEqual((c["frameIn"] - self.f.start, c["frameOut"] - self.f.start), (24, 46))

    def test_snap_cannot_carry_a_clip_past_the_end_of_the_source(self):
        a = self.asset()
        fa = shots.facts(self.store, a.id)
        for lo, hi in ((0, 100), (400, 600)):
            self.shot(fa.label(fa.start + lo), fa.label(fa.start + hi), asset=a)
        self.store.conn.execute("UPDATE essence SET duration_frames = 450 WHERE asset_id = ?", (a.id,))   # shot 3 now overruns it
        self.store.conn.commit()
        w = self.words(a, ["x"], first=fa.start + 420)
        self.refused(422, "outside_source", {"kind": "clip", "from_words": [w[0], w[0]], "snap": "shot"}, asset=a)


class Refusals(Behaviour):
    def test_words_from_different_assets(self):
        other = self.asset()
        ow = self.words(other)
        p = self.refused(409, "not_transcript", {"kind": "clip", "from_words": [self.w[0], ow[1]]})
        self.assertIn("another asset", p["error"]["reason"])
        self.refused(409, "not_transcript", {"kind": "clip", "from_words": [ow[0], self.w[1]]})
        self.refused(409, "not_transcript", {"kind": "clip", "from_words": [ow[0], ow[1]]})   # both foreign

    def test_reversed_order(self):
        p = self.refused(422, "bad_input", {"kind": "clip", "from_words": [self.w[5], self.w[2]]})
        self.assertIn("out of order", p["error"]["reason"])

    def test_a_missing_word(self):
        self.refused(404, "not_found", {"kind": "clip", "from_words": [self.w[0], "no-such-word"]})
        self.refused(404, "not_found", {"kind": "clip", "from_words": ["no-such-word", self.w[0]]})
        self.refused(404, "not_found", {"kind": "clip", "from_words": ["a", "b"]}, asset=self.a)
        s, p = self.post("/api/assets/nope/spans", {"kind": "clip", "from_words": [self.w[0], self.w[1]]})
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))

    def test_words_whose_frames_are_not_placed_yet(self):
        late = self.words(self.a, ["late", "words"], first=self.start() + 500, placed=False)
        p = self.refused(409, "frames_pending", {"kind": "clip", "from_words": [late[0], late[1]]})
        self.assertIn("backfill", p["error"]["reason"])
        self.refused(409, "frames_pending", {"kind": "clip", "from_words": [self.w[0], late[1]]})
        self.refused(409, "frames_pending", {"kind": "clip", "from_words": [late[0], self.w[1]]})

    def test_spans_that_are_not_transcript_words(self):
        clip = shots.create(self.store, self.a.id, "clip", "01:00:00:00", "01:00:01:00")["id"]
        shot = self.shot("01:00:02:00", "01:00:03:00")
        for bad in (clip, shot):
            p = self.refused(409, "not_transcript", {"kind": "clip", "from_words": [bad, self.w[5]]})
            self.assertIn("not a transcript word", p["error"]["reason"])
            self.refused(409, "not_transcript", {"kind": "clip", "from_words": [self.w[0], bad]})

    def test_the_body_shape(self):
        w = self.w
        for words in (None, "ab", [], [w[0]], [w[0], w[1], w[2]], [w[0], 5], [5, 6], [w[0], ""], [w[0], "x" * 129], {"a": 1}, [[w[0]], [w[1]]]):
            self.refused(422, "bad_input", {"kind": "clip", "from_words": words})
        self.refused(422, "bad_input", {"kind": "shot", "from_words": [w[0], w[1]]})        # words make clips
        self.refused(422, "bad_input", {"kind": "marker", "from_words": [w[0], w[1]]})
        self.refused(422, "bad_input", {"from_words": [w[0], w[1]]})                         # kind missing
        self.refused(422, "bad_input", {"kind": "clip", "from_words": [w[0], w[1]], "origin": "detect"})
        self.refused(422, "bad_input", {"kind": "clip", "from_words": [w[0], w[1]], "label": "x" * 201})
        self.refused(422, "bad_input", {"kind": "clip", "from_words": [w[0], w[1]], "label": 3})

    def test_handles_and_snap_values(self):
        pair = [self.w[0], self.w[1]]
        for key in ("handle_in", "handle_out"):
            for bad in (-1, 1.5, "5", True, [], {}, 10001):
                self.refused(422, "bad_input", {"kind": "clip", "from_words": pair, key: bad})
        for bad in ("none", "SHOT", "word", 1, True, ["shot"]):
            self.refused(422, "bad_input", {"kind": "clip", "from_words": pair, "snap": bad})

    def test_handles_that_leave_the_source_are_refused_not_clamped(self):
        # the first word is at the very start of the essence, the last is near its end
        self.refused(422, "outside_source", {"kind": "clip", "from_words": [self.w[0], self.w[1]], "handle_in": 1})
        end = self.words(self.a, ["tail"], first=self.start() + 985)        # [985, 995) of 1000
        self.make(end[0], end[0], handle_out=5)                              # out at exactly 1000 is fine
        self.refused(422, "outside_source", {"kind": "clip", "from_words": [end[0], end[0]], "handle_out": 6})

    def test_handles_and_snap_belong_to_from_words(self):
        for extra in ({"handle_in": 3}, {"handle_out": 3}, {"snap": "shot"}):
            self.refused(422, "bad_input", {"kind": "clip", "tc_in": "01:00:00:00", "tc_out": "01:00:01:00", **extra})

    def test_neither_words_nor_times(self):
        self.refused(422, "bad_input", {"kind": "clip"})

    def test_unknown_fields_are_still_refused(self):
        self.refused(422, "bad_input", {"kind": "clip", "from_words": [self.w[0], self.w[1]], "words": 3})

    def test_an_asset_with_no_usable_timebase(self):
        bare = self.store.mint_asset(owner="desk")
        self.refused(422, "bad_input", {"kind": "clip", "from_words": [self.w[0], self.w[1]]}, asset=bare)


class RekindedWords(Behaviour):
    """A speech re-run keeps a word an item uses and re-kinds it `clip` (slice 1). It is no longer a word."""

    def setUp(self):
        super().setUp()
        clip = self.dir / "iso.mp4"
        subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
                               "-c:a", "aac", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sp = self.store.mint_asset(owner="desk")
        self.store.set_cleared(self.sp.id, True)
        self.store.add_essence(self.sp.id, role="hi-res", location=str(clip), frame_rate="25", timecode_start="01:00:00:00")

    def run_engine(self, model, *texts):
        transcribe(self.store, self.sp.id, FakeEngine([Word(t, 0.2 + i * 0.5, 0.6 + i * 0.5) for i, t in enumerate(texts)], model=model))

    def ids(self):
        return {r["text"]: r["id"] for r in self.store.conn.execute(
            "SELECT id, text FROM span WHERE asset_id = ? AND kind = 'transcript'", (self.sp.id,))}

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg makes the clip the speech run reads")
    def test_a_word_a_rerun_re_kinded_to_clip_cannot_be_selected_as_a_word(self):
        self.run_engine("tiny.en", "they", "steal", "home")
        before = self.ids()
        self.store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Show', '2026-01-01T00:00:00Z')")
        self.store.conn.commit()
        attach(self.store, add_item(self.store, "s1", "slug"), before["steal"])
        self.run_engine("large-v3", "they", "steal", "the", "ball")
        now = self.ids()
        self.assertNotIn(before["steal"], now.values())                       # it is a clip now
        _, tr = self.get(f"/api/assets/{self.sp.id}/transcript?limit=100")
        self.assertNotIn(before["steal"], [x["id"] for x in tr["words"]])     # and the transcript read agrees
        rows = len(self.rows())
        for pair in ([before["steal"], now["ball"]], [now["they"], before["steal"]], [before["steal"], before["steal"]]):
            s, p = self.post(f"/api/assets/{self.sp.id}/spans", {"kind": "clip", "from_words": pair})
            self.assertEqual((s, p["error"]["code"]), (409, "not_transcript"), pair)
        self.assertEqual(len(self.rows()), rows)
        # the new words from the same run are selectable, and the re-kinded span's text is not in their text
        c = self.make(now["they"], now["ball"], asset=self.sp)
        self.assertEqual(c["text"], "they steal the ball")
        self.assertEqual((c["origin"], c["kind"]), ("transcript", "clip"))


class Gate(Behaviour):
    def test_a_token_is_required_when_one_is_set(self):
        serve.TOKEN = TOKEN
        path = f"/api/assets/{self.a.id}/spans"
        body = {"kind": "clip", "from_words": [self.w[0], self.w[1]]}
        self.assertEqual(self.post(path, body)[0], 401)
        self.assertEqual(self.post(path, body, token="wrong" * 5)[0], 401)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM span WHERE kind = 'clip'").fetchone()[0], 0)
        self.assertEqual(self.post(path, body, token=TOKEN)[0], 200)


ALL = (FromWords, Snap, Refusals, RekindedWords, Gate)


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
del Behaviour, FromWords, Snap, Refusals, RekindedWords, Gate

if __name__ == "__main__":
    unittest.main()
