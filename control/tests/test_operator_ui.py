import json
import os
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

    def test_the_detect_split_and_merge_hooks_are_wired_not_disabled(self):
        js, html = FILES["operator.js"], FILES["index.html"]
        for token in ("spanSplit:", "spanMerge:", "detect:", "spanFrames:", "function paintDetect", "function splitMergeBlock",
                      "function libBackground", "armedAction('detect:'"):
            self.assertIn(token, js)
        self.assertIn('id="lib-detectbar"', html)
        self.assertNotIn("Detection is not on this screen yet", js)
        self.assertNotIn("come with detection", js)
        self.assertNotIn("The hook for slice 3", js)
    def test_the_transcript_tab_sends_word_ids_and_never_a_time_and_owns_its_keys(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        for needle in ('id="lib-tab-words"', 'id="lib-words"', 'id="lib-tr"', "Library, Mark from transcript tab"):
            self.assertIn(needle, html)
        body = js[js.index("function addFromWords"):js.index("// ---- dialogs, keys, screens")]
        self.assertIn("from_words: [W.words[r.i].id, W.words[r.j].id]", body)
        self.assertNotIn("tc_in", body)
        self.assertNotIn("tc_out", body)
        keys = js[js.index("function trKey"):js.index("function clearWordSel")]
        for show_only in ("takeNow", "toggleHold", "skipItem", "killItem", "location.hash"):
            self.assertNotIn(show_only, keys)
        # the shot list's keys stand down on this tab, so Enter and Space cannot do two things
        self.assertIn("W.tab === 'words') return;", js[js.index("function libKey"):js.index("function libKey") + 200])

    def test_the_export_tab_is_wired_warns_and_has_no_keys(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        for needle in ('id="lib-tab-export"', 'id="lib-export"', 'id="lib-x-table"', 'id="lib-x-prev"', 'id="lib-x-opts"',
                       'id="lib-x-act"', 'id="lib-x-done"', "Library, Selection to export tab"):
            self.assertIn(needle, html)
        self.assertIn("exportNle:   ['POST', '/api/exports/nle']", js)
        body = js[js.index("// ---- Library: export to an NLE"):js.index("// ---- dialogs, keys, screens")]
        # the format is unverified until the owner records the import check; the page says so and the switch is one constant
        self.assertIn("const FORMAT_VERIFIED = false;", body)
        self.assertIn("Not checked in an NLE yet.", body)
        self.assertIn("Manual import check", body)
        self.assertIn("Result: not yet recorded.", (Path(serve.ROOT).parents[1] / "docs" / "specs" / "shot-spans.md").read_text(encoding="utf-8"))
        # no key handler of its own, nothing of the Show keys, and the shot list's keys stand down on the tab
        for forbidden in ("keydown", "takeNow", "toggleHold", "skipItem", "killItem", "location.hash", "e.key"):
            self.assertNotIn(forbidden, body)
        libkey = js[js.index("function libKey"):js.index("function libKey") + 400]
        self.assertIn("W.tab === 'export') return;", libkey)
        # the file is saved from a Blob after a fetch (so the bearer token travels), never from a URL with a token in it
        self.assertIn("new Blob([done.content]", body)
        self.assertIn("URL.createObjectURL", body)
        self.assertIn("await call('exportNle'", body)
        self.assertNotIn("?token", body)
        self.assertNotIn("access_token", body)
        # the tray's button now opens the tab instead of being disabled
        tray = js[js.index("function paintTray"):js.index("function paintSpan")]
        self.assertIn("onclick: xOpen", tray)
        self.assertNotIn("disabled: true", tray)
        # every refusal code the server can send has words
        server_codes = set(re.findall(r'NleError\(\d+,\s*"(\w+)"', (Path(serve.ROOT).parent / "volt" / "nle.py").read_text(encoding="utf-8")))
        server_codes |= {"bad_input", "not_found"}
        for code in sorted(server_codes):
            self.assertRegex(body, rf"\b{code}: \[", code)

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


    def drain(self):
        from volt.runner import Config, Runner, default_handlers
        return Runner(self.store, default_handlers(Config(out_dir=Path(self.tmp.name) / "out")), name="t1").drain()

    def test_detect_on_the_cuts_asset_finds_its_shots_and_the_page_reads_the_job(self):
        a = self.ids["iso-5"]
        _, h, _ = self.get(f"/api/assets/{a}/spans")
        self.assertEqual((h["spans"], h["detect"], h["indexing"], h["pendingFrames"]), ([], None, None, 0))
        status, p = self.post(f"/api/assets/{a}/shots/detect", {})
        self.assertEqual((status, p["job"]["status"]), (200, "queued"))
        _, h, _ = self.get(f"/api/assets/{a}/spans")
        self.assertEqual(h["detect"]["status"], "queued")
        self.assertEqual(self.drain(), {"done": 1})
        _, h, _ = self.get(f"/api/assets/{a}/spans?kind=shot")
        self.assertEqual([(x["frameIn"] - 90000, x["frameOut"] - 90000) for x in h["spans"]], [(0, 60), (60, 110), (110, 180), (180, 250)])
        self.assertEqual((h["detect"]["status"], h["lastRun"]["shotCount"]), ("done", 4))
        self.assertEqual(self.post(f"/api/assets/{a}/shots/detect", {})[1]["alreadyDone"], True)
        # a thumbnail the job wrote is the file the route serves (one path rule), so the route makes no new one
        thumbs = list((Path(self.tmp.name) / "out" / "thumbs" / a).glob("*.jpg"))
        self.assertEqual(len(thumbs), 4)

    def test_the_old_spans_asset_is_indexed_by_the_backfill_and_then_listed(self):
        a = self.ids["iso-8"]
        _, h, _ = self.get(f"/api/assets/{a}/spans")
        self.assertEqual((h["pendingFrames"], h["spans"]), (2, []))
        status, p = self.post(f"/api/assets/{a}/span-frames", {})
        self.assertEqual((status, p["queued"], p["job"]["status"]), (200, True, "queued"))
        self.assertEqual(self.drain(), {"done": 1})
        _, h, _ = self.get(f"/api/assets/{a}/spans")
        self.assertEqual((h["pendingFrames"], len(h["spans"]), h["indexing"]["status"]), (0, 2, "done"))

    def test_split_and_merge_work_on_the_drop_frame_asset_that_is_not_cleared(self):
        _, p, _ = self.get(f"/api/assets/{self.ids['iso-6']}/spans?kind=shot")
        first = p["spans"][0]
        status, out = self.post(f"/api/spans/{first['id']}/split", {"at": "01:00:06;00"})
        self.assertEqual((status, out["spans"][1]["tcIn"], out["spans"][0]["blockedBy"]), (200, "01:00:06;00", "not cleared"))
        status, out2 = self.post(f"/api/spans/{first['id']}/merge", {"with": out["spans"][1]["id"]})
        self.assertEqual((status, out2["span"]["tcOut"]), (200, first["tcOut"]))

    def test_detect_waits_for_the_asset_that_is_still_recording(self):
        status, p = self.post(f"/api/assets/{self.ids['iso-7']}/shots/detect", {})
        self.assertEqual((status, p["error"]["code"]), (409, "essence_open"))


class ExportScenario(Api):
    """The harness's `export` scenario: each refusal the export tab words is reachable, through the same route the page calls."""

    def setUp(self):
        super().setUp()
        serve.OUT_DIR = Path(self.tmp.name) / "out"
        serve.ROOM, self.show, self.spans = seed(self.store, "export", media_dir=Path(self.tmp.name))
        self.ids = {k[6:]: v for k, v in self.spans.items() if k.startswith("asset:")}
        self._env = os.environ.get("VOLT_EXPORT_PATH_MAP")
        self.addCleanup(lambda: os.environ.pop("VOLT_EXPORT_PATH_MAP", None) if self._env is None else os.environ.__setitem__("VOLT_EXPORT_PATH_MAP", self._env))
        os.environ.pop("VOLT_EXPORT_PATH_MAP", None)

    def span(self, camera, label):
        _, p, _ = self.call("GET", f"/api/assets/{self.ids[camera]}/spans?limit=500")
        return next(s["id"] for s in p["spans"] if s["label"] == label)

    def dry(self, spans, **kw):
        return self.post("/api/exports/nle", {"format": "xmeml", "spans": spans, "dry_run": True, **kw})

    def test_a_clean_selection_previews_and_exports_with_the_file_name(self):
        a, b = self.span("iso-4", "Bench reaction"), self.span("iso-4", "Steal and drive")
        s, p = self.dry([a, b], name="Final Four Watch selects")
        self.assertEqual((s, p["ok"], p["filename"], p["rate"]), (200, True, "Final_Four_Watch_selects.xml", "25"))
        self.assertEqual([v["status"] for v in p["spans"]], ["ready", "ready"])
        self.assertTrue(p["preview"][0].startswith("<?xml"))
        s, p = self.post("/api/exports/nle", {"format": "xmeml", "spans": [a, b], "name": "Final Four Watch selects"})
        self.assertEqual((s, p["filename"]), (200, "Final_Four_Watch_selects.xml"))
        self.assertIn("<xmeml", p["content"])

    def test_rights_are_verdicts_in_a_dry_run_and_a_refusal_in_a_real_one(self):
        ok, hold, nc = self.span("iso-4", "Bench reaction"), self.span("iso-9", "Post-game handshake"), self.span("iso-6", "Tip")
        s, p = self.dry([ok, hold, nc])
        self.assertEqual(s, 200)
        self.assertEqual([(v["status"], v.get("blockedBy")) for v in p["spans"]], [("ready", None), ("blocked", "legal hold"), ("blocked", "not cleared")])
        body = {"format": "xmeml", "spans": [ok, hold, nc]}
        e = self.refused(409, "blocked", "/api/exports/nle", body)
        self.assertEqual(len(e["spans"]), 3)
        s, p = self.post("/api/exports/nle", {**body, "skip_blocked": True})
        self.assertEqual(s, 200)
        self.assertIn("VOLT SKIPPED", p["content"])
        self.assertTrue(any("left out" in w for w in p["warnings"]))

    def test_every_other_refusal_has_a_reachable_case(self):
        wide, arch = self.span("iso-4", "Bench reaction"), self.span("iso-10", "Archive wide")
        live, tip = self.span("iso-7", "Live moment"), self.span("iso-11", "Tip-in, 29.97")
        self.refused(409, "not_local", "/api/exports/nle", {"format": "xmeml", "spans": [wide, arch], "dry_run": True})
        s, p = self.dry([wide, arch], media="proxy")                                       # the page's "Use the proxy"
        self.assertEqual(s, 200, p)
        self.refused(409, "essence_open", "/api/exports/nle", {"format": "xmeml", "spans": [live], "dry_run": True})
        self.refused(409, "no_proxy", "/api/exports/nle", {"format": "xmeml", "spans": [live], "media": "proxy", "dry_run": True})
        e = self.refused(422, "mixed_rates", "/api/exports/nle", {"format": "xmeml", "spans": [wide, tip], "dry_run": True})
        self.assertEqual(sorted(e["rates"]), ["25", "30000/1001"])                         # what the page's "Keep only ..." buttons use
        self.refused(404, "not_found", "/api/exports/nle", {"format": "xmeml", "spans": [wide, "no-such-span"], "dry_run": True})
        self.refused(422, "bad_input", "/api/exports/nle", {"format": "xmeml", "spans": [wide], "start_tc": "99", "dry_run": True})

    def test_the_path_map_shows_in_the_dry_run(self):
        a = self.span("iso-4", "Bench reaction")
        s, p = self.dry([a])
        self.assertFalse(p["pathMapSet"])
        os.environ["VOLT_EXPORT_PATH_MAP"] = self.tmp.name + "=\\\\nas\\iso"
        s, p = self.dry([a])
        self.assertTrue(p["pathMapSet"])
        self.assertTrue(p["paths"][0]["mapped"])
        self.assertIn("nas", "\n".join(p["preview"]) + p["paths"][0]["path"])


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
