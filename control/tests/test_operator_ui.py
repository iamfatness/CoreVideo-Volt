import json
import re
import shutil
import subprocess
import unittest
from fractions import Fraction
from pathlib import Path

from tests.operator_harness import seed
from tests.test_operator_api import TOKEN, Api
from volt import serve, timecode

SESSION = Path(serve.ROOT) / "operator"
FILES = {name: (SESSION / name).read_text(encoding="utf-8") for name in ("index.html", "operator.css", "operator.js")}


class Static(Api):
    def get(self, path, **kw):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            conn.request("GET", path, headers=kw.get("headers", {}))
            resp = conn.getresponse()
            return resp.status, resp.read(), resp
        finally:
            conn.close()

    def test_page_and_assets_are_served_with_a_locked_down_policy(self):
        for path, ctype in (("/operator", "text/html"), ("/operator/operator.css", "text/css"),
                            ("/operator/operator.js", "text/javascript")):
            status, body, resp = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype))
            self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
            self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_shell_is_open_with_a_token_but_state_is_not(self):
        serve.TOKEN = TOKEN
        self.assertEqual(self.get("/operator")[0], 200)
        self.assertEqual(self.get("/operator/operator.js")[0], 200)
        self.assertEqual(self.get("/api/state")[0], 401)
        status, _, _ = self.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)

    def test_foreign_host_is_refused_without_a_token(self):
        status, _, _ = self.get("/operator", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_references_only_same_origin_assets(self):
        html = FILES["index.html"]
        refs = re.findall(r'(?:src|href)="([^"]*)"', html)
        for ref in refs:
            self.assertTrue(ref.startswith(("/operator/", "#", "data:")), ref)
        for name, text in FILES.items():
            self.assertNotRegex(text, r"https?://", name)
            self.assertNotRegex(text, r"@import|url\(\s*['\"]?(?!data:)", name)
        self.assertNotIn(" style=", html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")  # no inline script
        self.assertNotIn("<style", html)
        for ref in refs:
            if ref.startswith("/operator/"):
                self.assertEqual(self.get(ref)[0], 200, ref)

    def test_token_lives_in_session_storage_only(self):
        js = FILES["operator.js"]
        self.assertIn("sessionStorage", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("document.cookie", js)


class Endpoints(Api):
    def test_every_endpoint_the_page_calls_exists(self):
        pairs = re.findall(r"\['(GET|POST)',\s*'(/api/[^']*)'\]", FILES["operator.js"])
        self.assertGreaterEqual(len(pairs), 22)
        for method, tpl in pairs:
            path = tpl.replace("{id}", "x1")
            if method == "POST":
                self.assertTrue(any(p.match(path) for p, _ in serve.ROUTES), tpl)
            elif tpl == "/api/state":
                status, payload, _ = self.call("GET", path)
                self.assertEqual(status, 200, tpl)
                self.assertIn("rundown", payload)
            else:  # the library's reads: a route that exists answers an unknown id with a stable 404, not "no such route"
                status, payload, _ = self.call("GET", path)
                self.assertEqual((status, payload["error"]["code"]), (404, "not_found"), tpl)
                self.assertNotEqual(payload["error"]["reason"], "no such route", tpl)

    def test_every_post_the_page_uses_is_answered_not_404(self):
        # A wrong id is a stable refusal (404 not_found or 422), never an unknown route.
        for method, tpl in re.findall(r"\['(POST)',\s*'(/api/[^']*)'\]", FILES["operator.js"]):
            status, payload = self.post(tpl.replace("{id}", "nope"), {})
            self.assertFalse(payload["ok"], tpl)
            self.assertIn(status, (404, 422, 503), tpl)
            self.assertTrue(payload["error"]["reason"], tpl)


class Library(Api):
    """The Library screen's static contract. The browser behaviour is checked by hand in a real Chrome (axe, keys)."""

    get = Static.get

    def test_the_fifth_screen_is_in_the_nav_and_the_page(self):
        html = FILES["index.html"]
        self.assertIn('<a href="#library" data-screen="library">Library</a>', html)
        self.assertIn('id="screen-library"', html)
        self.assertIn("'show', 'record', 'rights', 'publish', 'library'", FILES["operator.js"])

    def test_the_csp_lets_the_page_show_its_own_blobs_and_nothing_else(self):
        _, _, resp = self.get("/operator")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("img-src 'self' data: blob:", csp)
        self.assertIn("media-src 'self' blob:", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)

    def test_no_inline_style_attributes_are_set_by_the_script(self):
        # style-src 'self' blocks style="..." and setAttribute('style'); only CSSOM (el.style.x) is allowed.
        self.assertNotRegex(FILES["operator.js"], r"setAttribute\(\s*['\"]style|['\"]style['\"]\s*:")

    def test_every_library_key_is_listed_and_none_reaches_the_show_keys(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        for key in ("<kbd>K</kbd>", "<kbd>,</kbd> <kbd>.</kbd>", "<kbd>[</kbd> <kbd>]</kbd>", "<kbd>I</kbd> <kbd>O</kbd>",
                    "<kbd>Enter</kbd>", "<kbd>1</kbd>..<kbd>5</kbd>"):
            self.assertIn(key, html)
        body = js[js.index("function libKey"):js.index("// ---- rendering", js.index("function libKey"))]
        for show_only in ("takeNow", "toggleHold", "skipItem", "killItem", "location.hash"):
            self.assertNotIn(show_only, body)
        # the dispatcher hands the Library its own keys before the Show keys can see them
        self.assertLess(js.index("if (S.screen === 'library') { libKey(e); return; }"), js.index("if (k === 't')"))

    def test_the_old_keys_are_unchanged(self):
        js = FILES["operator.js"]
        for line in ("if (k === 't') { if (!e.repeat) takeNow(true); return; }", "if (k === 'h') { if (!e.repeat) toggleHold(); return; }",
                     "if (e.key === ' ' || e.code === 'Space') {"):
            self.assertIn(line, js)


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is needed to run the page's timecode functions")
class TimecodeParity(unittest.TestCase):
    """The page counts timecode itself (it is sent labels the server then validates). Hold it to volt/timecode.py."""

    RATES = ["25", "30000/1001", "24000/1001", "60000/1001", "29.97", "30", "50", "23.976"]

    def run_js(self, calls):
        script = ("const m = require(process.argv[1]); const calls = JSON.parse(process.argv[2]);"
                  "const rate = (v) => (v && typeof v === 'object' && 'rate' in v ? m.parseRate(v.rate) : v);"
                  "console.log(JSON.stringify(calls.map(([fn, ...a]) => m[fn](...a.map(rate)))));")
        out = subprocess.run([NODE, "-e", script, str(SESSION / "operator.js"), json.dumps(calls)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_frames_to_label_matches_python_on_every_rate_and_boundary(self):
        for rate in self.RATES:
            r = timecode.parse_rate(rate)
            drops = [False] + ([True] if r in (Fraction(30000, 1001), Fraction(60000, 1001)) else [])
            nom = timecode.nominal_fps(r)
            frames = [0, 1, nom - 1, nom, nom * 60 - 1, nom * 60, nom * 60 + 1, nom * 600 - 1, nom * 600, nom * 3600 * 25 + 7,
                      17981, 17982, 17983, 17999, 18000, 107891, 107892, 107893, 215783, 215784]
            for drop in drops:
                got = self.run_js([["framesToTc", f, {"rate": rate}, drop] for f in frames])
                self.assertEqual(got, [timecode.frames_to_tc(f, r, drop) for f in frames], (rate, drop))

    def test_label_to_frames_matches_python_including_labels_that_do_not_exist(self):
        cases = [("01:00:00:00", "25"), ("01:00:00;00", "30000/1001"), ("00:00:59;29", "30000/1001"), ("00:01:00;02", "30000/1001"),
                 ("00:01:00;00", "30000/1001"), ("00:01:00;01", "30000/1001"), ("00:10:00;00", "30000/1001"), ("00:01:00;03", "60000/1001"),
                 ("00:01:00:00", "30000/1001"), ("01:00:00:25", "25"), ("01:60:00:00", "25"), ("nonsense", "25"), ("01:00:00:24", "25"),
                 ("100:00:00:00", "25")]
        got = self.run_js([["tcToFrames", tc, {"rate": rate}] for tc, rate in cases])
        want = []
        for tc, rate in cases:
            try:
                want.append(timecode.tc_to_frames(tc, rate))
            except timecode.TimecodeError:
                want.append(None)
        self.assertEqual(got, want)

    def test_rates_and_drop_labels(self):
        got = self.run_js([["isDropLabel", "01:00:00;00"], ["isDropLabel", "01:00:00:00"], ["isDropLabel", None],
                           ["fmtRate", {"n": 30000, "d": 1001}], ["fmtRate", {"n": 25, "d": 1}], ["fmtRate", {"n": 24000, "d": 1001}],
                           ["fmtRate", {"n": 60000, "d": 1001}], ["lengthTc", 163, {"n": 25, "d": 1}], ["parseRate", "abc"], ["parseRate", "29.5"]])
        self.assertEqual(got, [True, False, False, "29.97 fps", "25 fps", "23.976 fps", "59.94 fps", "00:00:06:13", None, None])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg makes the harness's proxy files")
class LibraryScenario(Api):
    """The harness's `library` scenario read back through the real routes the page calls."""

    def setUp(self):
        super().setUp()
        serve.OUT_DIR = Path(self.tmp.name) / "out"
        serve.ROOM, self.show, self.spans = seed(self.store, "library", media_dir=Path(self.tmp.name))
        self.ids = {k[6:]: v for k, v in self.spans.items() if k.startswith("asset:")}

    def get(self, path):
        status, payload, resp = self.call("GET", path)
        return status, payload, resp

    def test_state_says_which_assets_have_a_proxy(self):
        _, state, _ = self.get("/api/state")
        by = {a["id"]: a for a in state["assets"]}
        self.assertTrue(by[self.ids["iso-4"]]["hasProxy"])
        self.assertTrue(by[self.ids["iso-6"]]["hasProxy"])
        self.assertFalse(by[self.ids["iso-7"]]["hasProxy"])
        self.assertTrue(by[self.ids["iso-7"]]["open"])

    def test_the_main_asset_lists_the_mockups_shots_a_clip_and_a_detection_run(self):
        _, p, _ = self.get(f"/api/assets/{self.ids['iso-4']}/spans?limit=500")
        self.assertEqual([x["kind"] for x in p["spans"]].count("shot"), 6)
        self.assertEqual([x["label"] for x in p["spans"] if x["kind"] == "clip"], ["Okonkwo steal and layup"])
        self.assertEqual((p["rate"], p["start"], p["durationFrames"], p["hasProxy"], p["open"]), ("25", "01:00:00:00", 915, True, False))
        self.assertEqual((p["lastRun"]["shotCount"], p["lastRun"]["threshold"]), (5, 0.3))
        self.assertEqual({x["origin"] for x in p["spans"]}, {"detect", "manual", "transcript"})
        _, w, _ = self.get(f"/api/assets/{self.ids['iso-4']}/transcript?limit=1000")
        self.assertGreater(len(w["words"]), 50)

    def test_the_drop_frame_asset_uses_semicolons_and_says_it_is_not_cleared(self):
        _, p, _ = self.get(f"/api/assets/{self.ids['iso-6']}/spans")
        self.assertTrue(all(";" in x["tcIn"] and ";" in x["tcOut"] for x in p["spans"]))
        self.assertEqual({x["blockedBy"] for x in p["spans"]}, {"not cleared"})

    def test_empty_and_recording_assets(self):
        _, p, _ = self.get(f"/api/assets/{self.ids['iso-5']}/spans")
        self.assertEqual((p["spans"], p["hasProxy"]), ([], True))
        _, p, _ = self.get(f"/api/assets/{self.ids['iso-7']}/spans")
        self.assertEqual((p["spans"], p["hasProxy"], p["open"], p["durationFrames"]), ([], False, True, None))

    def test_the_proxy_streams_by_range_and_a_thumbnail_is_cut(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("GET", f"/api/assets/{self.ids['iso-4']}/proxy", headers={"Range": "bytes=0-99"})
            r = conn.getresponse()
            self.assertEqual((r.status, len(r.read())), (206, 100))
            _, p, _ = self.get(f"/api/assets/{self.ids['iso-4']}/spans?kind=shot")
            conn.request("GET", f"/api/spans/{p['spans'][1]['id']}/thumb")
            r = conn.getresponse()
            body = r.read()
            self.assertEqual((r.status, r.getheader("Content-Type"), body[:2]), (200, "image/jpeg", b"\xff\xd8"))
        finally:
            conn.close()


class State(Api):
    def setUp(self):
        super().setUp()
        serve.ROOM, self.show, self.spans = seed(self.store, "held")

    def state(self, show=""):
        status, payload, _ = self.call("GET", "/api/state" + (f"?show={show}" if show else ""))
        self.assertEqual(status, 200)
        return payload

    def test_state_keeps_the_rundown_shape_and_adds_what_the_screens_need(self):
        s = self.state()
        self.assertEqual(set(s["rundown"]), {"hold", "items"})
        self.assertEqual(s["rundown"]["hold"]["reason"], "Waiting on the producer for the replay cue")
        self.assertEqual(s["show"]["id"], self.show)
        statuses = [i["status"] for i in s["rundown"]["items"]]
        self.assertEqual(statuses, ["aired", "on air", "ready", "ready", "killed", "ready", "skipped"])
        game = s["rundown"]["items"][2]["id"]
        self.assertEqual([c["command"] for c in s["cues"][game]], ["overlay.take", "stream.start", "show-input.take"])
        self.assertIsNone(s["media"][game]["blockedBy"])
        self.assertEqual(s["room"]["state"], "idle")

    def test_assets_say_why_they_cannot_air(self):
        by_cam = {a["camera"]: a for a in self.state()["assets"]}
        self.assertIsNone(by_cam["iso-3"]["blockedBy"])
        self.assertEqual(by_cam["iso-1"]["blockedBy"], "not cleared")
        self.assertEqual(by_cam["iso-2"]["blockedBy"], "legal hold")

    def test_unknown_show_falls_back_to_the_newest(self):
        self.assertEqual(self.state("gone")["show"]["id"], self.show)

    def test_no_room_is_a_state_not_an_error(self):
        serve.ROOM = None
        s = self.state()
        self.assertFalse(s["room"]["configured"])

    def test_spans_carry_versions_and_blocks(self):
        spans = {s["text"]: s for s in self.state()["spans"]}
        self.assertEqual(spans["open"]["versions"], [])
        self.assertEqual(spans["buzzer"]["blockedBy"], "not cleared")
        self.post(f"/api/spans/{self.spans['open']}/publish")
        spans = {s["text"]: s for s in self.state()["spans"]}
        self.assertEqual([v["kind"] for v in spans["open"]["versions"]], ["house", "ott", "social"])
        self.assertTrue(all(v["status"] == "done" for v in spans["open"]["versions"]))

    def test_held_take_is_refused_with_the_stable_code_the_page_switches_on(self):
        p = self.refused(409, "held", f"/api/shows/{self.show}/take")
        self.assertTrue(p["error"]["reason"])


if __name__ == "__main__":
    unittest.main()
