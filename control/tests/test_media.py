"""Read API and media (slice 2 of docs/specs/shot-spans.md): span listing, transcript, proxy ranges, thumbnails.

Run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set. Tests that need a real encode skip without ffmpeg."""

import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from volt import media, serve, shots
from volt.rundown import add_item, attach, open_show
from volt.store import Store

from tests.pgutil import PgCase

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
TOKEN = "t" * 24
BLOB = bytes(range(256)) * 4          # 1024 bytes, every offset distinguishable


def _clip(path: Path, seconds=3, rate=25) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=640x360:r={rate}:d={seconds}", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Behaviour:
    """Provides self.open_store() and self.target (what serve.DB_PATH points at)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, media.RUN, media.THUMB_TIMEOUT)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self.target, None, self.dir / "out"
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)   # port 0: the OS picks a free one
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, media.RUN, media.THUMB_TIMEOUT = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers
    def get(self, path, headers=None, token=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("GET", path, headers=h)
            r = conn.getresponse()
            body = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body
        finally:
            conn.close()

    def jget(self, path, **kw):
        status, _, body = self.get(path, **kw)
        return status, json.loads(body)

    def refused(self, status, code, path, **kw):
        s, p = self.jget(path, **kw)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        return p

    def asset(self, proxy_bytes=BLOB, proxy=True, rate="25", start="01:00:00:00", duration=75):
        a = self.store.mint_asset(owner="desk", camera="iso-3")
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=duration)
        if proxy:
            self.proxy_path = self.dir / f"{a.id}.mp4"
            if proxy_bytes is not None:
                self.proxy_path.write_bytes(proxy_bytes)
            self.store.add_essence(a.id, role="proxy", location=str(self.proxy_path))
        return a

    def span(self, a, kind, tc_in, tc_out, **kw):
        return shots.create(self.store, a.id, kind, tc_in, tc_out, **kw)["id"]

    def word(self, a, text, fin, fout):
        f = shots.facts(self.store, a.id)
        return self.store.add_span(a.id, "transcript", f.label(fin), f.label(fout), text=text,
                                   origin="transcript", frame_in=fin, frame_out=fout)


class Spans(Behaviour):
    def setUp(self):
        super().setUp()
        self.a = self.asset()
        self.s1 = self.span(self.a, "shot", "01:00:00:00", "01:00:01:00", label="Wide")
        self.s2 = self.span(self.a, "shot", "01:00:01:00", "01:00:02:00")
        self.c1 = self.span(self.a, "clip", "01:00:00:12", "01:00:01:12", label="Reaction")
        base = shots.facts(self.store, self.a.id).start
        self.w = [self.word(self.a, t, base + 5 * i, base + 5 * i + 4) for i, t in enumerate("one two three".split())]

    def ids(self, p, key="spans"):
        return [s["id"] for s in p[key]]

    def test_default_lists_shots_and_clips_in_frame_order_with_header(self):
        s, p = self.jget(f"/api/assets/{self.a.id}/spans")
        self.assertEqual(s, 200)
        self.assertTrue(p["ok"])
        # shot 1 (frame 0), clip (frame 12), shot 2 (frame 25): ordered by frame_in, never by kind
        self.assertEqual(self.ids(p), [self.s1, self.c1, self.s2])
        self.assertIsNone(p["next"])
        self.assertEqual((p["rate"], p["start"], p["durationFrames"], p["open"]), ("25", "01:00:00:00", 75, False))
        self.assertEqual((p["cleared"], p["legalHold"], p["hasProxy"], p["lastRun"]), (False, False, True, None))
        first = p["spans"][0]
        self.assertEqual((first["label"], first["kind"], first["origin"], first["tcIn"], first["tcOut"],
                          first["frameIn"], first["frameOut"], first["blockedBy"], first["usedBy"]),
                         ("Wide", "shot", "manual", "01:00:00:00", "01:00:01:00",
                          90000, 90025, "not cleared", {"items": 0, "versions": 0}))

    def test_kind_filter(self):
        _, p = self.jget(f"/api/assets/{self.a.id}/spans?kind=clip")
        self.assertEqual(self.ids(p), [self.c1])
        _, p = self.jget(f"/api/assets/{self.a.id}/spans?kind=transcript")
        self.assertEqual(self.ids(p), self.w)
        _, p = self.jget(f"/api/assets/{self.a.id}/spans?kind=shot,transcript")
        self.assertEqual({s["kind"] for s in p["spans"]}, {"shot", "transcript"})
        self.assertEqual(len(p["spans"]), 5)
        for bad in ("kind=marker", "kind=", "kind=shot,bogus"):
            self.refused(422, "bad_input", f"/api/assets/{self.a.id}/spans?{bad}")

    def test_window(self):
        # from/to select spans that overlap [from, to): half open, so a span ending exactly at `from` is out
        _, p = self.jget(f"/api/assets/{self.a.id}/spans?from=01:00:01:00&to=01:00:02:00")
        self.assertEqual(self.ids(p), [self.c1, self.s2])
        _, p = self.jget(f"/api/assets/{self.a.id}/spans?to=01:00:00:12")
        self.assertEqual(self.ids(p), [self.s1])
        self.refused(422, "bad_timecode", f"/api/assets/{self.a.id}/spans?from=01:00:99:00")
        self.refused(422, "bad_input", f"/api/assets/{self.a.id}/spans?from=01:00:02:00&to=01:00:01:00")

    def test_paging_follows_the_cursor_without_gaps_or_repeats(self):
        for i in range(2, 9):
            self.span(self.a, "clip", f"01:00:0{i // 4}:{(i % 4) * 5:02d}", f"01:00:0{i // 4}:{(i % 4) * 5 + 4:02d}")
        _, whole = self.jget(f"/api/assets/{self.a.id}/spans?limit=500")
        seen, after, pages = [], None, 0
        while True:
            q = "limit=3" + (f"&after={after}" if after else "")
            _, p = self.jget(f"/api/assets/{self.a.id}/spans?{q}")
            seen += self.ids(p)
            pages += 1
            after = p["next"]
            if not after:
                break
        self.assertEqual(seen, self.ids(whole))
        self.assertEqual(len(seen), 10)
        self.assertEqual(pages, 4)

    def test_equal_frame_in_pages_stably(self):
        # three clips with the same in: the id breaks the tie and the cursor must not skip or repeat any
        same = [self.span(self.a, "clip", "01:00:02:00", f"01:00:02:0{n}") for n in (2, 3, 4)]
        seen, after = [], None
        while True:
            _, p = self.jget(f"/api/assets/{self.a.id}/spans?kind=clip&from=01:00:02:00&limit=1"
                             + (f"&after={after}" if after else ""))
            seen += self.ids(p)
            after = p["next"]
            if not after:
                break
        self.assertEqual(sorted(seen), sorted(same))
        self.assertEqual(len(seen), 3)

    def test_bad_query_refused(self):
        base = f"/api/assets/{self.a.id}/spans"
        for q in ("limit=0", "limit=501", "limit=abc", "limit=-1", "after=not-a-cursor", "after=", "bogus=1",
                  "limit=1&limit=2"):
            self.refused(422, "bad_input", f"{base}?{q}")

    def test_unknown_asset_is_404_on_every_read(self):
        for tail in ("spans", "transcript", "proxy"):
            self.refused(404, "not_found", f"/api/assets/nope/{tail}")
        self.refused(404, "not_found", "/api/assets/../spans")

    def test_in_use_and_rights_words(self):
        show = open_show(self.store, "S")
        item = add_item(self.store, show, "x")
        attach(self.store, item, self.s1)
        self.store.set_cleared(self.a.id, True)
        _, p = self.jget(f"/api/assets/{self.a.id}/spans")
        by = {s["id"]: s for s in p["spans"]}
        self.assertEqual(by[self.s1]["usedBy"], {"items": 1, "versions": 0})
        self.assertEqual(by[self.s2]["usedBy"], {"items": 0, "versions": 0})
        self.assertIsNone(by[self.s1]["blockedBy"])
        self.store.set_hold(self.a.id, True)
        _, p = self.jget(f"/api/assets/{self.a.id}/spans")
        self.assertEqual({s["blockedBy"] for s in p["spans"]}, {"legal hold"})
        self.assertTrue(p["legalHold"])

    def test_unplaced_spans_are_left_out_and_counted(self):
        self.store.add_span(self.a.id, "clip", "01:00:02:00", "01:00:02:10", label="old")   # no frame columns
        _, p = self.jget(f"/api/assets/{self.a.id}/spans")
        self.assertEqual(len(p["spans"]), 3)
        self.assertEqual(p["pendingFrames"], 1)

    def test_last_run_summary(self):
        job = self.store.enqueue(self.a.id, "shots", "shots-v1:x")
        ess = self.store.conn.execute("SELECT id FROM essence WHERE role='hi-res' AND asset_id=?",
                                      (self.a.id,)).fetchone()["id"]
        self.store.conn.execute(
            "INSERT INTO shot_run (job_id, asset_id, essence_id, engine, engine_version, threshold, min_frames, "
            "shot_count, created_at, settings) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.id, self.a.id, ess, "ffmpeg-scene", "7.1", 0.3, 12, 4, "2026-10-12T10:00:00Z", "{}"))
        self.store.conn.commit()
        _, p = self.jget(f"/api/assets/{self.a.id}/spans")
        run = p["lastRun"]
        self.assertEqual((run["job"], run["engine"], run["threshold"], run["minFrames"], run["shotCount"]),
                         (job.id, "ffmpeg-scene", 0.3, 12, 4))

    def test_asset_without_hi_res_lists_empty_with_null_facts(self):
        bare = self.store.mint_asset(owner="desk")
        _, p = self.jget(f"/api/assets/{bare.id}/spans")
        self.assertEqual((p["spans"], p["rate"], p["start"], p["hasProxy"]), ([], None, None, False))
        self.refused(422, "bad_input", f"/api/assets/{bare.id}/spans?from=01:00:00:00")

    def test_transcript_pages_and_windows(self):
        _, p = self.jget(f"/api/assets/{self.a.id}/transcript")
        self.assertEqual([(w["text"], w["kind"]) for w in p["words"]],
                         [(t, "transcript") for t in ("one", "two", "three")])
        self.assertIsNone(p["next"])
        _, p = self.jget(f"/api/assets/{self.a.id}/transcript?limit=2")
        self.assertEqual(len(p["words"]), 2)
        _, p2 = self.jget(f"/api/assets/{self.a.id}/transcript?limit=2&after={p['next']}")
        self.assertEqual([w["text"] for w in p["words"] + p2["words"]], ["one", "two", "three"])
        # words 5 frames apart; [from, to) = frames 6..9 overlaps only "two" (5..9)
        _, p = self.jget(f"/api/assets/{self.a.id}/transcript?from=01:00:00:06&to=01:00:00:09")
        self.assertEqual([w["text"] for w in p["words"]], ["two"])
        self.refused(422, "bad_input", f"/api/assets/{self.a.id}/transcript?kind=shot")
        self.refused(422, "bad_input", f"/api/assets/{self.a.id}/transcript?limit=1001")

    def test_drop_frame_window(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", duration=3000)
        s = self.span(a, "clip", "01:00:59;28", "01:01:00;04")       # spans the minute where ;00 and ;01 do not exist
        _, p = self.jget(f"/api/assets/{a.id}/spans?from=01:01:00;02&to=01:01:00;04")
        self.assertEqual(self.ids(p), [s])
        self.assertEqual(p["rate"], "30000/1001")
        self.refused(422, "bad_timecode", f"/api/assets/{a.id}/spans?from=01:01:00;00")


class Proxy(Behaviour):
    def setUp(self):
        super().setUp()
        self.a = self.asset()
        self.url = f"/api/assets/{self.a.id}/proxy"

    def test_full_body(self):
        s, h, body = self.get(self.url)
        self.assertEqual((s, body, h["accept-ranges"], h["content-length"], h["content-type"]),
                         (200, BLOB, "bytes", "1024", "video/mp4"))

    def test_ranges(self):
        cases = [("bytes=0-9", 0, 9), ("bytes=100-", 100, 1023), ("bytes=-16", 1008, 1023),
                 ("bytes=1000-5000", 1000, 1023), ("bytes=-5000", 0, 1023), ("bytes=5-5", 5, 5),
                 ("BYTES=2-3", 2, 3)]
        for header, a, b in cases:
            s, h, body = self.get(self.url, headers={"Range": header})
            self.assertEqual((s, body), (206, BLOB[a:b + 1]), header)
            self.assertEqual((h["content-range"], h["content-length"]), (f"bytes {a}-{b}/1024", str(b - a + 1)), header)

    def test_bad_ranges_are_416_with_the_size(self):
        for header in ("bytes=1024-", "bytes=5000-6000", "bytes=abc", "bytes=5-2", "bytes=-0", "bytes=-", "bytes=",
                       "bytes=--5", "bytes=1-2-3", "bytes=0x10-20", "bytes=+1-2"):
            s, h, body = self.get(self.url, headers={"Range": header})
            self.assertEqual(s, 416, header)
            self.assertEqual(h["content-range"], "bytes */1024", header)
            self.assertEqual(json.loads(body)["error"]["code"], "bad_range", header)

    def test_ranges_that_may_be_ignored_get_the_whole_file(self):
        for headers in ({"Range": "bytes=0-1,5-6"}, {"Range": "items=0-1"},
                        {"Range": "bytes=0-1", "If-Range": '"etag"'}):
            s, h, body = self.get(self.url, headers=headers)
            self.assertEqual((s, body), (200, BLOB), headers)
            self.assertNotIn("content-range", h)

    def test_empty_file_range_is_416(self):
        a = self.asset(proxy_bytes=b"")
        s, h, _ = self.get(f"/api/assets/{a.id}/proxy", headers={"Range": "bytes=0-0"})
        self.assertEqual((s, h["content-range"]), (416, "bytes */0"))
        self.assertEqual(self.get(f"/api/assets/{a.id}/proxy")[0], 200)

    def test_no_proxy_row_and_missing_file(self):
        bare = self.asset(proxy=False)
        self.refused(404, "no_proxy", f"/api/assets/{bare.id}/proxy")
        gone = self.asset(proxy_bytes=None)           # registered, never written
        self.refused(404, "no_proxy", f"/api/assets/{gone.id}/proxy")

    def test_query_on_the_route_is_refused(self):
        self.refused(422, "bad_input", self.url + "?path=/etc/passwd")

    def test_no_client_path_reaches_the_disk(self):
        secret = self.dir / "secret.txt"
        secret.write_bytes(b"do not serve")
        for path in ("/api/assets/..%2Fsecret.txt/proxy", "/api/assets/%2e%2e/proxy", "/api/assets/../proxy",
                     f"/api/assets/{secret}/proxy", "/api/assets/....//proxy", "/api/assets/a%00b/proxy"):
            s, _, body = self.get(path)
            self.assertEqual(s, 404, path)
            self.assertNotIn(b"do not serve", body)

    def test_media_route_serves_only_registered_proxies(self):
        s, _, body = self.get("/media?path=" + quote(str(self.proxy_path)))
        self.assertEqual((s, body), (200, BLOB))
        s, _, body = self.get("/media?path=" + quote(str(self.proxy_path)), headers={"Range": "bytes=1-2"})
        self.assertEqual((s, body), (206, BLOB[1:3]))
        secret = self.dir / "secret.txt"
        secret.write_bytes(b"do not serve")
        hi = self.dir / "hi.mxf"
        hi.write_bytes(b"hi-res bytes")
        for target in (str(secret), str(hi), str(self.proxy_path) + "/../secret.txt", "../secret.txt",
                       str(self.dir / ".." / self.dir.name / "secret.txt"), "", str(self.proxy_path) + "x"):
            s, _, body = self.get("/media?path=" + quote(target))
            self.assertIn(s, (404, 422), target)
            self.assertNotIn(b"do not serve", body)
            self.assertNotIn(b"hi-res bytes", body)
        self.refused(422, "bad_input", "/media")
        self.refused(422, "bad_input", "/media?path=a&path=b")
        self.refused(422, "bad_input", "/media?path=a&x=1")

    def test_auth_and_host_rules(self):
        serve.TOKEN = TOKEN
        urls = [self.url, f"/api/assets/{self.a.id}/spans", f"/api/assets/{self.a.id}/transcript",
                "/api/spans/x/thumb", "/media?path=x"]
        for u in urls:
            self.refused(401, "unauthorized", u)
            self.refused(401, "unauthorized", u, token="wrong" * 6)
        self.assertEqual(self.get(self.url, token=TOKEN)[0], 200)
        self.assertEqual(self.get(f"/api/assets/{self.a.id}/spans", token=TOKEN)[0], 200)
        serve.TOKEN = None
        for u in urls:
            s, _, _ = self.get(u, headers={"Host": "evil.example"})
            self.assertEqual(s, 403, u)


class Thumbs(Behaviour):
    def setUp(self):
        super().setUp()
        self.a = self.asset(proxy_bytes=None)
        self.s1 = self.span(self.a, "shot", "01:00:00:00", "01:00:01:00")        # frames 0-25 of the proxy
        self.s2 = self.span(self.a, "shot", "01:00:01:00", "01:00:02:00")
        self.calls = []
        self.thumbs = self.dir / "out" / "thumbs"

    def real_clip(self):
        if not HAVE_FFMPEG:
            self.skipTest("ffmpeg not installed")
        _clip(self.proxy_path)

    def spy(self):
        real = subprocess.run

        def run(cmd, **kw):
            self.calls.append(list(cmd))
            return real(cmd, **kw)
        media.RUN = run

    def leftovers(self):
        return sorted(p.name for p in self.thumbs.rglob("*") if p.is_file())

    def test_frame_rule(self):
        self.assertEqual(media.thumb_frame(100, 200), 110)       # 10% of 100
        self.assertEqual(media.thumb_frame(100, 140), 104)       # 10% = 4
        self.assertEqual(media.thumb_frame(100, 120), 102)       # at least 2 frames past the cut
        self.assertEqual(media.thumb_frame(100, 102), 101)       # capped inside the span
        self.assertEqual(media.thumb_frame(100, 101), 100)       # a one-frame span has one frame

    def test_generates_caches_and_serves_a_320_wide_jpeg(self):
        self.real_clip()
        self.spy()
        s, h, body = self.get(f"/api/spans/{self.s2}/thumb")
        self.assertEqual((s, h["content-type"], h["cache-control"]), (200, "image/jpeg", "private, max-age=3600"))
        self.assertEqual(body[:3], b"\xff\xd8\xff")
        frame = 90025 + 2                               # shot 2 starts 25 frames in; 10% of 25 is 2
        self.assertEqual(self.leftovers(), [f"{self.s2}-{frame}.jpg"])
        cmd = self.calls[0]
        self.assertAlmostEqual(float(cmd[cmd.index("-ss") + 1]), 27 / 25, places=3)   # 1.08 s into the proxy
        self.assertIn("scale=320:-2", cmd)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0",
                                str(next(self.thumbs.rglob("*.jpg")))], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "320")
        # second request: from the cache, no ffmpeg
        s, _, again = self.get(f"/api/spans/{self.s2}/thumb")
        self.assertEqual((s, again, len(self.calls)), (200, body, 1))

    def test_a_trim_makes_a_new_key(self):
        self.real_clip()
        c = self.span(self.a, "clip", "01:00:00:00", "01:00:02:00")
        self.get(f"/api/spans/{c}/thumb")
        shots.trim(self.store, c, tc_in="01:00:00:20")
        self.get(f"/api/spans/{c}/thumb")
        mine = [n for n in self.leftovers() if n.startswith(c)]
        self.assertEqual(len(mine), 2, mine)

    def test_unknown_span_no_proxy_and_missing_file(self):
        self.refused(404, "not_found", "/api/spans/nope/thumb")
        bare = self.asset(proxy=False)
        sp = self.span(bare, "clip", "01:00:00:00", "01:00:01:00")
        self.refused(404, "no_proxy", f"/api/spans/{sp}/thumb")
        gone = self.asset(proxy_bytes=None)                        # registered, file never written
        sp = self.span(gone, "clip", "01:00:00:00", "01:00:01:00")
        self.refused(404, "no_proxy", f"/api/spans/{sp}/thumb")
        self.assertEqual(self.leftovers(), [])
        self.refused(422, "bad_input", f"/api/spans/{self.s1}/thumb?w=9999")

    def test_ffmpeg_failure_surfaces_and_leaves_no_file(self):
        self.proxy_path.write_bytes(b"not a video")

        def failing(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"")                       # ffmpeg often leaves an empty output behind
            return subprocess.CompletedProcess(cmd, 1, "", "moov atom not found")
        media.RUN = failing
        p = self.refused(502, "thumb_failed", f"/api/spans/{self.s1}/thumb")
        self.assertIn("moov atom", p["error"]["reason"])
        self.assertEqual(self.leftovers(), [])

    def test_exit_zero_without_an_image_is_a_failure(self):
        self.proxy_path.write_bytes(b"x")
        media.RUN = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
        self.refused(502, "thumb_failed", f"/api/spans/{self.s1}/thumb")
        self.assertEqual(self.leftovers(), [])

    def test_ffmpeg_missing_is_an_error_not_a_crash(self):
        self.proxy_path.write_bytes(b"x")

        def missing(cmd, **kw):
            raise FileNotFoundError("ffmpeg")
        media.RUN = missing
        self.refused(502, "thumb_failed", f"/api/spans/{self.s1}/thumb")
        self.assertEqual(self.leftovers(), [])

    def test_timeout_surfaces_and_removes_the_partial_file(self):
        self.proxy_path.write_bytes(b"x")

        def hang(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"\xff\xd8 half an image")
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        media.RUN = hang
        self.refused(504, "thumb_timeout", f"/api/spans/{self.s1}/thumb")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.get(f"/api/spans/{self.s1}/thumb")[0], 504)    # nothing cached to serve next time

    def test_a_real_hang_is_killed_at_the_limit(self):
        self.proxy_path.write_bytes(b"x")
        real = subprocess.run
        media.THUMB_TIMEOUT = 1

        def sleeper(cmd, **kw):
            self.assertEqual(kw["timeout"], 1)
            return real([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        media.RUN = sleeper
        self.refused(504, "thumb_timeout", f"/api/spans/{self.s1}/thumb")
        self.assertEqual(self.leftovers(), [])

    def test_limits_match_the_spec(self):
        self.assertEqual((media.THUMB_TIMEOUT, media.THUMB_WIDTH), (15, 320))


ALL = (Spans, Proxy, Thumbs)


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
del Behaviour, Spans, Proxy, Thumbs

if __name__ == "__main__":
    unittest.main()
