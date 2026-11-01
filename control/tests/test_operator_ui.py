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
from tests.test_playout import Clock, _show
from volt import serve, shots, timecode
from volt.playout import advance, go

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

    def test_root_serves_the_same_operator_html_as_operator(self):
        status_root, body_root, resp_root = self.get("/")
        status_op, body_op, resp_op = self.get("/operator")
        self.assertEqual(status_root, 200)
        self.assertEqual(status_op, 200)
        self.assertEqual(body_root, body_op)
        self.assertTrue(resp_root.getheader("Content-Type").startswith("text/html"))
        self.assertIn("script-src 'self'", resp_root.getheader("Content-Security-Policy"))

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
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")  # no inline script
        self.assertNotIn("<style", html)
        for ref in refs:
            if ref.startswith("/operator/"):
                self.assertEqual(self.get(ref)[0], 200, ref)

    def test_token_lives_in_session_storage_only(self):
        js = FILES["operator.js"]
        self.assertIn("sessionStorage", js)
        # The one per-browser convenience is the chosen workspace; the token never goes in localStorage.
        self.assertEqual(set(re.findall(r"localStorage\.\w+Item\('([^']+)'", js)), {"volt.workspace", "volt.theme"})
        self.assertEqual(js.count("localStorage"), len(re.findall(r"localStorage\.\w+Item\('volt\.(workspace|theme)'", js)))
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
            elif tpl == "/api/health":  # the footer reads the server version from it
                status, payload, _ = self.call("GET", path)
                self.assertIn(status, (200, 503), tpl)
                self.assertIn("version", payload)
            elif tpl == "/api/cue-commands":  # a list, not a lookup: it has no id to get wrong
                status, payload, _ = self.call("GET", path)
                self.assertEqual((status, payload["ok"]), (200, True), tpl)
            elif tpl == "/api/media-ticket":
                status, payload, _ = self.call("GET", path)
                self.assertIn(status, (401, 422, 503), tpl)
                self.assertFalse(payload["ok"], tpl)
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
        self.assertIn('href="#library" data-screen="library"', html)
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
        self.assertIn("Result: not yet recorded.", (Path(__file__).resolve().parents[2] / "docs" / "specs" / "shot-spans.md").read_text(encoding="utf-8"))
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
        server_codes = set(re.findall(r'NleError\(\d+,\s*"(\w+)"', (Path(serve.ROOT).parent / "nle.py").read_text(encoding="utf-8")))
        server_codes |= {"bad_input", "not_found"}
        for code in sorted(server_codes):
            self.assertRegex(body, rf"\b{code}: \[", code)

    def test_ingest_panel_is_wired_says_every_refusal_in_words_and_owns_no_keys(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        self.assertIn('id="lib-ingest"', html)
        self.assertIn("ingest:      ['POST', '/api/ingest']", js)
        body = js[js.index("// ---- ingest: one file"):js.index("  function renderLibrary()")]
        why = js[js.index("const INGEST_WHY"):js.index("const CUE_LABEL")]
        for code in ("path_not_allowed", "ingest_disabled", "not_found", "bad_input", "engine_missing", "disk_low", "unauthorized", "network"):
            self.assertIn(code + ": ", why)
        for forbidden in ("keydown", "takeNow", "toggleHold", "e.key", "location.hash"):
            self.assertNotIn(forbidden, body)
        # progress comes from the poll (assets[].jobs), not a second request loop
        self.assertIn("a.jobs", body)
        self.assertNotIn("setInterval", body)
        self.assertIn("role: 'alert'", body)    # a refusal is announced

    def test_the_attach_and_stale_order_paths_have_words_and_a_hint(self):
        js = FILES["operator.js"]
        self.assertIn("stale_order: 'The list changed'", js)
        move = js[js.index("function move(id, dir)"):js.index("// ---- rendering")]
        self.assertIn("version: S.data.orderVersion", move)
        self.assertIn("Nothing moved.", move)
        self.assertIn("act('attach', it.id, { span: e.id }", js)
        self.assertIn("act('itemDetach'", js)

    def test_no_complementary_landmark_sits_inside_main(self):
        # axe landmark-complementary-is-top-level: an aside inside <main> is a violation.
        self.assertNotIn("<aside", FILES["index.html"])

    def test_the_old_keys_are_unchanged(self):
        js = FILES["operator.js"]
        for line in ("if (k === 't') { if (!e.repeat) takeNow(true); return; }", "if (k === 'h') { if (!e.repeat) toggleHold(); return; }",
                     "if (e.key === ' ' || e.code === 'Space') {"):
            self.assertIn(line, js)

    def test_a_token_uses_media_tickets_and_blobs_only_on_404(self):
        js = FILES["operator.js"]
        self.assertIn("mediaTicket:", js)
        self.assertIn("'/api/media-ticket'", js)
        self.assertIn("status === 404", js)
        load = js[js.index("async function proxyLoad"):js.index("function onVideoReady")]
        self.assertIn("fetchMediaTicket", load)
        self.assertIn("v.src = ", load)
        self.assertIn("t.missing", load)
        self.assertIn("fetchBlob", load)
        thumbs = js[js.index("function pumpThumbs"):js.index("function thumbsPrune")]
        self.assertIn("fetchMediaTicket", thumbs)
        self.assertIn("n.src", thumbs)
        self.assertIn("t.missing", thumbs)


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

    def test_legal_hold_and_not_cleared_both_export(self):
        ok, hold, nc = self.span("iso-4", "Bench reaction"), self.span("iso-9", "Post-game handshake"), self.span("iso-6", "Tip")
        tip = self.span("iso-11", "Tip-in, 29.97")
        s, p = self.dry([ok, hold])
        self.assertEqual(s, 200)
        self.assertEqual([(v["status"], v.get("notCleared"), v.get("legalHold")) for v in p["spans"]],
                         [("ready", None, None), ("ready", None, True)])
        self.assertIn("leaves the original source untouched", p["spans"][1]["note"])
        s, p = self.dry([nc, tip])                                                          # not cleared, same rate as iso-11
        self.assertEqual(s, 200, p)
        self.assertEqual([(v["status"], v.get("notCleared")) for v in p["spans"]], [("ready", True), ("ready", None)])
        self.assertIn("publish and air stay blocked", p["spans"][0]["note"])
        self.assertTrue(any("not cleared" in w for w in p["warnings"]))
        s, p = self.post("/api/exports/nle", {"format": "xmeml", "spans": [nc, tip], "skip_blocked": True})
        self.assertEqual(s, 200, p)                                                         # accepted, ignored, a real export
        self.assertNotIn("SKIPPED", p["content"])
        s, p = self.post("/api/exports/nle", {"format": "xmeml", "spans": [ok, hold]})
        self.assertEqual(s, 200, p)

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
        self.assertEqual(set(s["rundown"]), {"hold", "items", "recurrence", "recurrenceOf", "slotSeconds"})
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
        self.assertNotIn("buzzer", spans)          # a transcript word nothing uses is not on the Publish list
        self.assertEqual({s["kind"] for s in self.state()["spans"]}, {"transcript"})   # only the words rundown items use
        iso1 = next(a["id"] for a in self.state()["assets"] if a["camera"] == "iso-1")
        self.store.add_essence(iso1, role="hi-res", location="media/iso-1.mxf", frame_rate="25",
                               timecode_start="01:00:00:00", duration_frames=500)
        clip = shots.create(self.store, iso1, "clip", "01:00:01:00", "01:00:03:00", label="Buzzer beater")
        row = next(s for s in self.state()["spans"] if s["id"] == clip["id"])
        self.assertEqual((row["kind"], row["label"], row["blockedBy"]), ("clip", "Buzzer beater", "not cleared"))
        self.assertEqual(len([s for s in self.state()["spans"] if s["kind"] == "transcript"]), len(spans))
        self.post(f"/api/spans/{self.spans['open']}/publish")
        spans = {s["text"]: s for s in self.state()["spans"]}
        self.assertEqual([v["kind"] for v in spans["open"]["versions"]], ["house", "ott", "social"])
        self.assertTrue(all(v["status"] == "done" for v in spans["open"]["versions"]))

    def test_held_take_is_refused_with_the_stable_code_the_page_switches_on(self):
        p = self.refused(409, "held", f"/api/shows/{self.show}/take")
        self.assertTrue(p["error"]["reason"])

    def test_state_surfaces_taking_as_the_current_item(self):
        # Production change that would fail this: omitting taking from /api/state, or leaving
        # the taking row as a generic ready/on-air status so the poll looks idle.
        show, ids = _show(self.store)  # this store already has the harness spans; take the new show's first three
        a, b = ids[0], ids[1]
        go(self.store, show, Clock())
        advance(self.store, show, Clock(), pending=True)
        s = self.state(show)
        self.assertEqual(s["taking"], b)
        self.assertEqual([i["id"] for i in s["rundown"]["items"] if i["status"] == "taking"], [b])
        self.assertNotIn("on air", {i["status"] for i in s["rundown"]["items"]})


@unittest.skipUnless(NODE, "node is needed to run the page's showInfo twin")
class TakingDesk(unittest.TestCase):
    """The poll must treat a taking row as the current item, not a blank on-air and not the next ready."""

    def derive(self, rundown):
        script = ("const m = require(process.argv[1]);"
                  "console.log(JSON.stringify(m.deriveShow(JSON.parse(process.argv[2]))));")
        out = subprocess.run(
            [NODE, "-e", script, str(SESSION / "operator.js"), json.dumps(rundown)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_taking_item_is_current_and_next_is_not_the_following_ready(self):
        # Production change that would fail this: showInfo only matching status === 'on air'.
        got = self.derive({
            "hold": None,
            "items": [
                {"id": "a", "slug": "Open", "status": "aired"},
                {"id": "b", "slug": "Game", "status": "taking"},
                {"id": "c", "slug": "Break", "status": "ready"},
            ],
        })
        self.assertEqual(got["taking"]["id"], "b")
        self.assertEqual(got["air"]["id"], "b")
        self.assertIsNone(got["next"])

    def test_on_air_still_picks_the_following_ready_as_next(self):
        got = self.derive({
            "hold": None,
            "items": [
                {"id": "a", "slug": "Open", "status": "aired"},
                {"id": "b", "slug": "Game", "status": "on air"},
                {"id": "c", "slug": "Break", "status": "ready"},
            ],
        })
        self.assertIsNone(got["taking"])
        self.assertEqual(got["air"]["id"], "b")
        self.assertEqual(got["next"]["id"], "c")


class CuePicker(unittest.TestCase):
    def test_show_input_take_is_labelled_not_live_proven(self):
        js = FILES["operator.js"]
        self.assertIn("(not live-proven)", js)
        self.assertIn("no live Program take is recorded yet", js)
        self.assertIn("(no verified adapter)", js)


class TakingCopy(unittest.TestCase):
    def test_desk_words_taking_and_blocks_take(self):
        js = FILES["operator.js"]
        self.assertRegex(js, r"taking:\s+\{ icon: 'pending', word: 'Taking'")
        self.assertIn("taking: 'Taking'", js)
        block = js[js.index("function takeBlock"):js.index("function takeNow")]
        self.assertIn("taking", block)




def _lum(hexcolour):
    c = [int(hexcolour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Tokens(unittest.TestCase):
    ROOT = re.search(r":root\{(.*?)\n\}", FILES["operator.css"], re.S).group(1)

    def token(self, name):
        return re.search(r"--%s:\s*([^;]+);" % re.escape(name), self.ROOT).group(1).strip()

    def test_colour_pairs_meet_contrast(self):
        t = self.token
        for fg, bg, need in [("text", "ground", 4.5), ("text", "panel", 4.5), ("muted", "panel", 4.5),
                             ("signal", "panel", 3.0), ("air", "panel", 3.0), ("ok", "panel", 3.0), ("blue-hi", "panel", 3.0)]:
            self.assertGreaterEqual(_ratio(t(fg), t(bg)), need, (fg, bg, _ratio(t(fg), t(bg))))

    def test_scales_exist(self):
        want = {**{"space-%d" % i: px for i, px in enumerate((4, 8, 12, 16, 24, 32), 1)}, "radius-sm": "3px", "radius": "6px",
                "fs-xs": ".75rem", "fs-sm": ".875rem", "fs-md": "1rem", "fs-lg": "1.25rem", "fs-xl": "1.75rem",
                "rail-w": "64px", "head-h": "48px"}
        for name, value in want.items():
            self.assertEqual(self.token(name), "%spx" % value if isinstance(value, int) else value, name)


ICON_NAMES = ("rundown record rights publish library take hold skip stop cut wave transcript marker onair ready pending alert "
              "sliders user key search filter download link trash lock room cue export shot plus close chevron more mic eye "
              "lower3 mark").split()


class Icons(unittest.TestCase):
    SPRITE_IDS = set(re.findall(r'<symbol id="i-([a-z0-9]+)"', FILES["index.html"]))

    def test_every_named_icon_has_a_symbol(self):
        self.assertEqual(set(ICON_NAMES) - self.SPRITE_IDS, set())

    def test_every_icon_the_page_asks_for_exists(self):
        asked = set(re.findall(r"icon\('([a-z0-9]+)'", FILES["operator.js"]))
        asked |= set(re.findall(r'href="#i-([a-z0-9]+)"', FILES["index.html"] + FILES["operator.js"]))
        self.assertEqual(asked - self.SPRITE_IDS, set())

    def test_sprite_obeys_the_csp_rules(self):
        self.assertNotIn("xmlns", FILES["index.html"])
        self.assertNotIn("style=", FILES["index.html"])
        self.assertIn("function icon(", FILES["operator.js"])


def _node(*calls):
    """Call exported pure functions of operator.js under node: each call is [name, *args]."""
    script = ("const m = require(process.argv[1]); const calls = JSON.parse(process.argv[2]);"
              "console.log(JSON.stringify(calls.map(([fn, ...a]) => m[fn] && m[fn].apply ? m[fn](...a) : m[fn])));")
    out = subprocess.run([NODE, "-e", script, str(SESSION / "operator.js"), json.dumps(list(calls))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class StateLanguage(unittest.TestCase):
    def test_each_state_maps_to_an_existing_icon_and_a_class(self):
        (state,) = _node(["STATE"])
        self.assertEqual(sorted(state), ["aired", "blocked", "canair", "cleared", "done", "failed", "fresh", "hold", "killed", "locked", "next", "onair", "pending", "queued", "ready", "recording", "running", "skipped", "stale", "taking", "uncleared", "withdrawn", "wrapped"])
        for kind, spec in state.items():
            self.assertIn(spec["icon"], Icons.SPRITE_IDS, kind)
            self.assertTrue(spec["cls"].startswith("st "), kind)
        self.assertEqual(state["onair"], {"icon": "onair", "word": "On air", "cls": "st air"})
        self.assertEqual(state["locked"]["word"], "Held")

    def test_row_states(self):
        next_, aired, killed, taking = _node(["stateChipSpec", "next"], ["stateChipSpec", "aired"], ["stateChipSpec", "killed"], ["stateChipSpec", "taking"])
        self.assertEqual(next_, {"icon": "take", "word": "Next", "cls": "st next"})
        self.assertEqual(aired, {"icon": "ready", "word": "Aired", "cls": "st done"})
        self.assertEqual(killed, {"icon": "close", "word": "Killed", "cls": "st done"})
        self.assertEqual(taking, {"icon": "pending", "word": "Taking", "cls": "st pend"})
        self.assertEqual(_node(["itemStateKind", "on air", False], ["itemStateKind", "ready", True], ["itemStateKind", "skipped", False],
                               ["cueStateKind", "armed"], ["cueStateKind", "fired"], ["cueStateKind", "unsupported"]),
                         ["onair", "next", "skipped", ["pending", "Armed"], ["ready", "Fired"], ["locked", "Unsupported"]])

    def test_chip_spec_never_throws_on_an_unknown_kind(self):
        known, unknown, bare = _node(["stateChipSpec", "ready"], ["stateChipSpec", "future-state", "Sealed"], ["stateChipSpec", "nonsense"])
        self.assertEqual(known, {"icon": "ready", "word": "Ready", "cls": "st ok"})
        self.assertEqual(unknown, {"icon": "pending", "word": "Sealed", "cls": "st"})
        self.assertEqual(bare["word"], "nonsense")


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class Workspaces(unittest.TestCase):
    def test_names_fall_back_to_operator(self):
        self.assertEqual(_node(["workspaceName", "director"], ["workspaceName", None], ["workspaceName", "editor"],
                               ["workspaceName", "constructor"], ["workspaceName", 5]),
                         ["operator", "operator", "editor", "operator", "operator"])

    def test_rail_has_the_workspaces_screens_and_one_extra_for_an_off_rail_hash(self):
        got = _node(["railFor", "operator", "rights"], ["railFor", "editor", "library"], ["railFor", "operator", "nonsense"],
                    ["railFor", "producer", "show"])
        self.assertEqual(got[0], {"screens": ["show", "record"], "extra": "rights"})
        self.assertIsNone(got[1]["extra"])
        self.assertIsNone(got[2]["extra"])
        self.assertEqual(got[3]["screens"], ["show", "rights", "publish", "library"])

    def test_default_screen_is_the_first_on_the_rail(self):
        self.assertEqual(_node(["defaultScreen", "editor"], ["defaultScreen", "operator"], ["defaultScreen", "producer"]),
                         ["library", "show", "show"])


class Shell(unittest.TestCase):
    HTML = FILES['index.html']

    def test_ids_the_script_depends_on_survive(self):
        for i in ('show-select', 'screens', 'workspaces', 'who', 'conn', 'token-btn', 'keys-btn', 'clock', 'main', 'banners',
                  'foot-show', 'foot-count', 'foot-version'):
            self.assertIn('id="%s"' % i, self.HTML, i)

    def test_workspace_buttons_exist_for_all_three(self):
        for w in ('producer', 'editor', 'operator'):
            self.assertRegex(self.HTML, r'<button[^>]*data-ws="%s"[^>]*aria-pressed' % w)

    def test_rail_links_keep_their_hash_and_data_screen_and_carry_an_icon(self):
        for scr in ('show', 'record', 'rights', 'publish', 'library'):
            self.assertRegex(self.HTML, r'<a href="#%s" data-screen="%s"[^>]*>\s*<svg class="ic"' % (scr, scr))
        self.assertIn('<nav aria-label="Screens" id="screens" class="rail">', self.HTML)

    def test_the_page_script_wires_the_workspace_and_survives_a_bad_store(self):
        js = FILES['operator.js']
        self.assertIn('function setWorkspace(', js)
        self.assertIn('volt.workspace', js)
        self.assertRegex(js, r'try \{[^}]*localStorage')




class ItemTiming(Api):
    """Each item's media carries its length and rate, so the page can total durations."""

    def setUp(self):
        super().setUp()
        serve.OUT_DIR = Path(self.tmp.name) / "out"
        serve.ROOM, self.show, self.spans = seed(self.store, "normal", media_dir=Path(self.tmp.name))
        # The harness attaches clips to assets with no essence; give each a hi-res file at 25 fps so lengths can be counted.
        for r in self.store.conn.execute("SELECT DISTINCT s.asset_id FROM item i JOIN span s ON s.id = i.span_id").fetchall():
            self.store.add_essence(r["asset_id"], "hi-res", str(Path(self.tmp.name) / "x.mxf"), frame_rate="25", timecode_start="01:00:00:00")

    def media(self):
        _, state, _ = self.call("GET", "/api/state")
        return state["media"]

    def test_media_carries_frames_and_rate(self):
        with_span = [m for m in self.media().values() if m.get("span")]
        self.assertTrue(with_span)
        for m in with_span:
            self.assertIsInstance(m["frames"], int)
            self.assertEqual(m["frames"], 50)  # the harness clips are two seconds at 25 fps
            self.assertIsNotNone(timecode.parse_rate(m["rate"]))

    def test_item_without_a_span_is_unchanged(self):
        no_span = [m for m in self.media().values() if not m.get("span")]
        for m in no_span:
            self.assertEqual(m, {"span": None, "blockedBy": "no media"})

    def test_unknown_rate_gives_nulls_and_state_still_answers(self):
        self.store.conn.execute("UPDATE essence SET frame_rate = NULL")
        self.store.conn.commit()
        with_span = [m for m in self.media().values() if m.get("span")]
        self.assertTrue(with_span)
        for m in with_span:
            self.assertEqual((m["frames"], m["rate"]), (None, None))


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class RundownTiming(unittest.TestCase):
    T0 = 1_800_000_000_000

    def iso(self, ms):
        import datetime
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    def items(self, *rows):
        return [{"id": "i%d" % n, "status": st, "asRunIn": None if at is None else self.iso(self.T0 + at * 1000)}
                for n, (st, at) in enumerate(rows)]

    def test_item_seconds_uses_the_exact_rate(self):
        got = _node(["itemSeconds", {"frames": 250, "rate": "25"}], ["itemSeconds", {"frames": 30000, "rate": "30000/1001"}],
                    ["itemSeconds", {"frames": 10}], ["itemSeconds", None], ["itemSeconds", {"frames": 10, "rate": "abc"}])
        self.assertEqual(got, [10, 1001, None, None, None])

    def test_nothing_aired_gives_offsets_and_no_clock(self):
        items = self.items(("ready", None), ("ready", None), ("ready", None))
        (r,) = _node(["frontTimes", items, {"i0": 60, "i1": None, "i2": 30}, self.T0])
        self.assertEqual([x["offset"] for x in r["rows"]], [0, 60, 60])
        self.assertEqual([x["startMs"] for x in r["rows"]], [None, None, None])
        self.assertEqual((r["totalSec"], r["driftSec"]), (90, None))

    def test_drift_is_actual_start_minus_planned_front(self):
        for late, want in ((75, 15), (50, -10), (60, 0)):
            items = self.items(("aired", 0), ("on air", late), ("ready", None))
            (r,) = _node(["frontTimes", items, {"i0": 60, "i1": 30, "i2": 30}, self.T0 + 99_000])
            self.assertEqual(r["rows"][1]["startMs"], self.T0 + 60_000)
            self.assertEqual(r["driftSec"], want, late)
            self.assertEqual(r["rows"][2]["startMs"], self.T0 + 90_000)

    def test_killed_and_skipped_take_no_time(self):
        items = self.items(("aired", 0), ("killed", None), ("skipped", None), ("ready", None))
        (r,) = _node(["frontTimes", items, {"i0": 60, "i1": 500, "i2": 500, "i3": 20}, self.T0])
        self.assertEqual([x["offset"] for x in r["rows"]], [0, None, None, 60])
        self.assertEqual(r["totalSec"], 80)

    def test_first_item_skipped_still_anchors_on_the_first_that_aired(self):
        items = self.items(("skipped", None), ("on air", 5), ("ready", None))
        (r,) = _node(["frontTimes", items, {"i0": 40, "i1": 60, "i2": 10}, self.T0])
        self.assertEqual((r["rows"][1]["offset"], r["driftSec"], r["rows"][2]["startMs"]), (0, 0, self.T0 + 65_000))

    def test_formats(self):
        self.assertEqual(_node(["fmtOffset", 3725], ["fmtOffset", 65], ["fmtOffset", None], ["fmtOffset", 0],
                               ["fmtSigned", 15], ["fmtSigned", -10], ["fmtSigned", 0], ["fmtSigned", None]),
                         ["1:02:05", "01:05", "--:--", "00:00", "+00:15", "-00:10", "00:00", ""])


class RundownMarkup(unittest.TestCase):
    def test_summary_and_columns_exist(self):
        js, html = FILES["operator.js"], FILES["index.html"]
        self.assertIn('id="rundown-sum"', html)
        self.assertIn("frontTimes(", js[js.index("function renderShow"):])
        for cls in ("dur", "front", "drift", "rhead"):
            self.assertIn("class: '%s" % cls, js, cls)

    def test_kill_is_set_apart(self):
        rule = re.search(r"\.rrow \.sep\{([^}]*)\}", FILES["operator.css"])
        self.assertIsNotNone(rule)
        self.assertIn("var(--space-4)", rule.group(1))
        self.assertIn("'sep'", FILES["operator.js"])

    def test_current_row_has_a_bar_in_the_right_colour(self):
        css = FILES["operator.css"]
        self.assertRegex(css, r"\.rrow\.is-air\{[^}]*border-left-color:var\(--air\)")
        self.assertRegex(css, r"\.rrow\.is-next\{[^}]*border-left-color:var\(--signal\)")

    def test_no_inline_style_added(self):
        self.assertNotIn("style=", FILES["index.html"])


class ShowLayouts(unittest.TestCase):
    def test_take_is_blue_and_has_the_take_icon(self):
        js, css = FILES["operator.js"], FILES["operator.css"]
        self.assertIn("icon('take'", js[js.index("function renderShow"):js.index("function renderShow") + 3000])
        rule = re.search(r"\.btn\.take\{[^}]*\}|\.take\{[^}]*\}", css)
        self.assertIsNotNone(rule)
        self.assertNotIn("--ok", css[css.index(".take{"):css.index(".take{") + 400])

    def test_operator_layout_hides_the_build_forms_not_the_dom(self):
        css, html = FILES["operator.css"], FILES["index.html"]
        self.assertRegex(css, r"\.ws-operator #build[^{]*\{[^}]*display:none")
        self.assertRegex(css, r"\.ws-operator #cue-add[^{]*\{[^}]*display:none")
        self.assertIn('id="build"', html)
        self.assertIn('id="cue-add"', html)
        self.assertRegex(css, r"\.ws-operator \.cols\{[^}]*grid-template-columns:\s*2fr 1fr")
        self.assertRegex(css, r"\.ws-producer \.cols[^{]*\{[^}]*grid-template-columns:\s*3fr 2fr")

    def test_panel_headings_have_icons(self):
        html = FILES["index.html"]
        for name in ("rundown", "cue", "room"):
            self.assertIn('href="#i-%s"' % name, html[html.index('id="screen-show"'):html.index('id="screen-record"')], name)

    def test_take_is_spaced_from_hold_and_advance(self):
        self.assertRegex(FILES["operator.css"], r"\.transport \.acts\{[^}]*margin-left:\s*var\(--space-5\)")


class RundownColumns(unittest.TestCase):
    def test_rows_and_header_share_fixed_tracks(self):
        css, js = FILES['operator.css'], FILES['operator.js']
        track = re.search(r'\.rrow,\.rhead\{display:grid;grid-template-columns:([^;]+);', css).group(1)
        self.assertNotIn('auto', track)   # an auto track is sized row by row and the columns drift apart
        self.assertNotIn('em ', track.replace('rem', ''))   # em would shrink with the header's smaller font
        head = js[js.index("class: 'rhead'"):]
        self.assertEqual(head[:head.index("items.map")].count("el('span'"), 7)   # six, plus Back when a slot is set


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class LibraryState(unittest.TestCase):
    def test_cleared_and_hold_specs(self):
        got = _node(["stateChipSpec", "cleared"], ["stateChipSpec", "uncleared"], ["stateChipSpec", "hold"],
                    ["stateChipSpec", "running"], ["stateChipSpec", "done"], ["stateChipSpec", "queued"])
        self.assertEqual(got, [{"icon": "ready", "word": "Cleared", "cls": "st ok"},
                               {"icon": "alert", "word": "Not cleared", "cls": "st fail"},
                               {"icon": "lock", "word": "Legal hold", "cls": "st fail"},
                               {"icon": "pending", "word": "Running", "cls": "st pend"},
                               {"icon": "ready", "word": "Done", "cls": "st ok"},
                               {"icon": "pending", "word": "Queued", "cls": "st pend"}])
        for spec in got:
            self.assertIn(spec["icon"], Icons.SPRITE_IDS)

    def test_headers_use_the_chips(self):
        js = FILES["operator.js"]
        self.assertNotIn("'⚑ '", js)
        self.assertEqual(len(re.findall(r"d\.cleared \? '✓ ' : '✕ '", js)), 0)
        self.assertGreaterEqual(js.count("assetChips("), 3)   # the definition and both headers
        self.assertNotIn("chip(JOB_CHIP", js)
        self.assertNotIn("chip(VER_CHIP", js)


class LibraryMarkup(unittest.TestCase):
    HTML = FILES["index.html"]

    def piece(self, start, end):
        i = self.HTML.index(start)
        return self.HTML[i:self.HTML.index(end, i)]

    def test_headings_and_tabs_have_icons(self):
        for hid, icon_name in (("h-ingest", "download"), ("h-player", "eye"), ("h-shots", "shot"), ("h-tray", "library"), ("h-span", "sliders")):
            self.assertIn('href="#i-%s"' % icon_name, self.piece('id="%s"' % hid, "</h2>"), hid)
        for tid, icon_name in (("lib-tab-shots", "shot"), ("lib-tab-words", "transcript"), ("lib-tab-export", "export")):
            self.assertIn('href="#i-%s"' % icon_name, self.piece('id="%s"' % tid, "</button>"), tid)

    def test_tab_attributes_survive(self):
        for tid, ctl in (("lib-tab-shots", "lib-body"), ("lib-tab-words", "lib-words"), ("lib-tab-export", "lib-export")):
            self.assertIn('aria-controls="%s"' % ctl, self.piece('id="%s"' % tid, "</button>"))
        for tid in ("lib-tab-words", "lib-tab-export"):
            self.assertIn('tabindex="-1"', self.piece('id="%s"' % tid, "</button>"))

    def test_no_script_overwrites_a_heading_with_an_icon(self):
        js = FILES["operator.js"]
        for hid in ("h-ingest", "h-player", "h-shots", "h-tray", "h-span"):
            self.assertNotIn("$('%s').textContent" % hid, js)
            self.assertNotIn("getElementById('%s').textContent" % hid, js)


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class Shuttle(unittest.TestCase):
    def run_keys(self, keys, state=None):
        script = ("const m = require(process.argv[1]); let s = JSON.parse(process.argv[2]); const out = [];"
                  "for (const k of JSON.parse(process.argv[3])) { s = m.shuttleNext(s, k); out.push([s.dir, s.speed]); }"
                  "console.log(JSON.stringify(out));")
        out = subprocess.run([NODE, "-e", script, str(SESSION / "operator.js"), json.dumps(state or {"dir": 0, "speed": 0}), json.dumps(keys)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_speed_ladder(self):
        self.assertEqual(self.run_keys(list("lllll")), [[1, 1], [1, 2], [1, 4], [1, 8], [1, 8]])
        self.assertEqual(self.run_keys(list("jjj")), [[-1, 1], [-1, 2], [-1, 4]])
        self.assertEqual(self.run_keys(list("lj")), [[1, 1], [-1, 1]])
        self.assertEqual(self.run_keys(list("jl")), [[-1, 1], [1, 1]])
        self.assertEqual(self.run_keys(list("llk")), [[1, 1], [1, 2], [0, 0]])
        self.assertEqual(self.run_keys(["k"]), [[0, 0]])

    def test_keys_are_wired_and_ignore_repeat_and_fields(self):
        js = FILES["operator.js"]
        start = js.index("function libKey")
        body = js[start:js.index("function stateGlyph", start)]
        self.assertRegex(body, r"low === 'j'[^\n]*!e\.repeat")
        self.assertRegex(body, r"low === 'l'[^\n]*!e\.repeat")
        on_key = js[js.index("function onKey"):js.index("function onKey") + 600]
        self.assertIn("input,select,textarea", on_key)   # typing jkl into a field never reaches libKey

    def test_every_exit_stops_the_shuttle(self):
        js = FILES["operator.js"]
        for fn in ("function libLeave", "function stepFrames", "function jumpShot", "function togglePlay", "function setTab", "function proxyRelease"):
            i = js.index(fn)
            self.assertIn("shuttleStop(", js[i:i + 700], fn)

    def test_documented(self):
        md = (Path(serve.ROOT).parents[2] / "docs" / "specs" / "operator-ui.md").read_text(encoding="utf-8")
        table = md[md.index("## Keys (Library screen)"):md.index("The Export tab has no keys")]
        for key in ("`J`", "`K`", "`L`"):
            self.assertIn(key, table)


class LibraryLayouts(unittest.TestCase):
    def test_editor_gives_the_main_column_the_width(self):
        css = FILES["operator.css"]
        m = re.search(r"@media\(min-width:1001px\)\{[^@]*\.ws-editor \.cols\.lib\{[^}]*minmax\(0,1fr\) 300px", css)
        self.assertIsNotNone(m)

    def test_narrow_rule_still_collapses_and_comes_last(self):
        css = FILES["operator.css"]
        narrow = css.index("@media(max-width:1000px){.cols.lib{grid-template-columns:1fr}}")
        self.assertGreater(narrow, css.index(".ws-editor .cols.lib"))

    def test_play_has_an_icon_and_keeps_its_key_and_label_updates(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        i = html.index('id="lib-play"')
        btn = html[html.rindex("<button", 0, i):html.index("</button>", i)]
        self.assertIn('href="#i-take"', btn)
        self.assertIn('aria-keyshortcuts="K"', btn)
        self.assertIn('id="lib-play-text"', btn)
        self.assertNotIn("$('lib-play').firstChild", js)   # the icon is the first child now


class ShuttleEdges(unittest.TestCase):
    def test_the_natural_end_and_a_video_error_clear_the_shuttle(self):
        js = FILES["operator.js"]
        self.assertRegex(js, r"addEventListener\('ended'[^\n]*SH\.dir")
        i = js.index("function onVideoError")
        self.assertIn("shuttleStop(", js[i:i + 300])

    def test_reverse_step_is_capped_so_a_hidden_tab_cannot_jump(self):
        js = FILES["operator.js"]
        self.assertRegex(js, r"Math\.min\(\s*\(t - SH\.last\) / 1000,\s*0\.1\s*\)")

    def test_withdrawn_versions_do_not_look_like_work_in_progress(self):
        self.assertEqual(_node(["stateChipSpec", "withdrawn"])[0], {"icon": "close", "word": "Withdrawn", "cls": "st done"})


def _fn_source(js, name, span=6000):
    i = js.index("function " + name)
    j = js.find("\n  function ", i + 10)
    return js[i:j if j > 0 else i + span]


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class ScreensState(unittest.TestCase):
    def test_specs(self):
        got = _node(*[["stateChipSpec", k] for k in ("recording", "wrapped", "fresh", "stale", "canair", "blocked")])
        self.assertEqual(got, [{"icon": "record", "word": "Recording", "cls": "st air"},
                               {"icon": "ready", "word": "Wrapped", "cls": "st ok"},
                               {"icon": "ready", "word": "Fresh", "cls": "st ok"},
                               {"icon": "alert", "word": "Stale", "cls": "st pend"},
                               {"icon": "ready", "word": "Yes", "cls": "st ok"},
                               {"icon": "alert", "word": "No", "cls": "st fail"}])
        for spec in got:
            self.assertIn(spec["icon"], Icons.SPRITE_IDS)

    def test_no_loose_glyphs_left_on_these_screens(self):
        js = FILES["operator.js"]
        for fn in ("renderRecord", "assetsTable", "renderRights", "renderPublish"):
            src = _fn_source(js, fn)
            for ch in "✓✕⚑⚠▶":
                self.assertNotIn(ch, src, (fn, ch))


class ScreensMarkup(unittest.TestCase):
    def test_headings_carry_icons(self):
        js = FILES["operator.js"]
        for hid in ("h-recstart", "h-lifecycle", "h-assets", "h-assets2", "h-clips"):
            i = js.index("el('h2', { id: '%s'" % hid)
            self.assertIn("icon(", js[i:i + 260], hid)


def _tokens(block):
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block))


class ThemeContrast(unittest.TestCase):
    CSS = FILES["operator.css"]
    DARK = _tokens(re.search(r":root\{(.*?)\n\}", CSS, re.S).group(1))
    LIGHT = _tokens(re.search(r':root\[data-theme="light"\]\{(.*?)\n\}', CSS, re.S).group(1)) if ':root[data-theme="light"]{' in CSS else {}
    AUTO = _tokens(re.search(r'@media\(prefers-color-scheme:light\)\{:root:not\(\[data-theme\]\)\{(.*?)\n\s*\}\}', CSS, re.S).group(1)) if "prefers-color-scheme:light" in CSS else {}

    # (foreground, background, minimum): text 4.5, icons and control borders 3
    PAIRS = [("text", "ground", 4.5), ("text", "panel", 4.5), ("text", "raise", 4.5), ("muted", "panel", 4.5), ("muted", "ground", 4.5),
             ("blue-ink", "panel", 4.5), ("signal-ink", "panel", 4.5), ("air-ink", "panel", 4.5), ("ok-ink", "panel", 4.5),
             ("signal-ink", "signal-bg", 4.5), ("air-ink", "air-bg", 4.5), ("ok-ink", "ok-bg", 4.5),
             ("on-signal", "signal", 4.5), ("on-air", "air", 4.5), ("on-blue", "blue", 4.5),
             ("off-fg", "off-bg", 4.5), ("text", "chip-bg", 4.5), ("text", "row-air", 4.5), ("text", "row-sel", 4.5),
             ("ctl", "panel", 3.0)]

    def check(self, tokens, label):
        for fg, bg, need in self.PAIRS:
            got = _ratio(tokens[fg], tokens[bg])
            self.assertGreaterEqual(got, need, (label, fg, bg, round(got, 2)))

    def test_dark_pairs(self):
        self.check(self.DARK, "dark")

    def test_light_pairs(self):
        self.assertTrue(self.LIGHT, "no light token block")
        self.check({**self.DARK, **self.LIGHT}, "light")

    def test_every_dark_colour_token_has_a_light_one(self):
        colours = {k for k, v in self.DARK.items() if v.strip().startswith("#")}
        self.assertEqual(colours - set(self.LIGHT), set())

    def test_the_os_following_set_is_the_same_as_the_chosen_set(self):
        self.assertEqual(self.AUTO, self.LIGHT)


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class ThemeChoice(Api):
    get = Static.get

    def test_names_and_cycle(self):
        got = _node(["themeName", "dark"], ["themeName", "light"], ["themeName", "auto"], ["themeName", "sepia"], ["themeName", None],
                    ["nextTheme", "auto"], ["nextTheme", "dark"], ["nextTheme", "light"])
        self.assertEqual(got, ["dark", "light", "auto", "auto", "auto", "dark", "light", "auto"])

    def test_the_early_script_and_the_button(self):
        html, js = FILES["index.html"], FILES["operator.js"]
        self.assertRegex(html, r'(?s)<head>.*<script src="/operator/theme\.js"></script>.*</head>')
        self.assertIn('id="theme-btn"', html)
        self.assertEqual(self.get("/operator/theme.js")[0], 200)
        early = (SESSION / "theme.js").read_text(encoding="utf-8")
        self.assertIn("try", early)
        self.assertNotRegex(early, r"https?://")
        self.assertRegex(js, r"try \{[^}]*localStorage[^}]*volt\.theme")


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class BackTimes(unittest.TestCase):
    T0 = 1_800_000_000_000

    def iso(self, ms):
        import datetime
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    def items(self, *rows):
        out = []
        for n, row in enumerate(rows):
            st, at = row[0], row[1]
            it = {"id": "i%d" % n, "status": st, "asRunIn": None if at is None else self.iso(self.T0 + at * 1000), "asRunOut": None}
            if len(row) > 2 and row[2] is not None:
                it["asRunOut"] = self.iso(self.T0 + row[2] * 1000)
            if len(row) > 3:
                it["scheduledAt"] = self.iso(self.T0 + row[3] * 1000)
            out.append(it)
        return out

    def run_back(self, items, seconds, slot):
        return _node(["backTimes", items, seconds, slot])[0]

    def test_back_time_counts_back_from_the_slot_end(self):
        items = self.items(("aired", 0), ("ready", None), ("ready", None))
        r = self.run_back(items, {"i0": 60, "i1": 60, "i2": 60}, 300)
        self.assertEqual(r["slotEndMs"], self.T0 + 300_000)
        self.assertEqual([x["backMs"] for x in r["rows"]], [None, self.T0 + 180_000, self.T0 + 240_000])
        self.assertEqual(r["overSec"], -120)   # 180 s planned against a 300 s slot: 120 s under

    def test_over_is_positive(self):
        items = self.items(("aired", 0), ("ready", None))
        self.assertEqual(self.run_back(items, {"i0": 200, "i1": 200}, 300)["overSec"], 100)

    def test_unknown_length_makes_the_rows_up_to_it_unknown(self):
        items = self.items(("aired", 0), ("ready", None), ("ready", None), ("ready", None))
        r = self.run_back(items, {"i0": 60, "i1": 60, "i2": None, "i3": 60}, 600)
        self.assertEqual([x["backMs"] for x in r["rows"]], [None, None, None, self.T0 + 540_000])

    def test_killed_and_skipped_take_no_time(self):
        items = self.items(("aired", 0), ("killed", None), ("ready", None))
        r = self.run_back(items, {"i0": 60, "i1": 500, "i2": 60}, 300)
        self.assertEqual([x["backMs"] for x in r["rows"]], [None, None, self.T0 + 240_000])

    def test_no_slot_gives_nothing(self):
        items = self.items(("aired", 0), ("ready", None))
        r = self.run_back(items, {"i0": 60, "i1": 60}, None)
        self.assertEqual((r["slotEndMs"], r["overSec"], [x["backMs"] for x in r["rows"]]), (None, None, [None, None]))

    def test_before_anything_airs_the_first_scheduled_time_anchors_it(self):
        items = self.items(("ready", None, None, 1000), ("ready", None))
        r = self.run_back(items, {"i0": 60, "i1": 60}, 300)
        self.assertEqual(r["slotEndMs"], self.T0 + 1_000_000 + 300_000)
        none = self.run_back(self.items(("ready", None), ("ready", None)), {"i0": 60, "i1": 60}, 300)
        self.assertEqual((none["slotEndMs"], none["overSec"]), (None, -180))

    def test_aired_items_count_by_their_actual_length(self):
        items = self.items(("aired", 0, 70), ("ready", None))
        self.assertEqual(self.run_back(items, {"i0": 60, "i1": 60}, 300)["overSec"], -170)   # 70 + 60 = 130 of 300

    def test_parse_slot(self):
        self.assertEqual(_node(["parseSlot", "15:00"], ["parseSlot", "1:30:00"], ["parseSlot", "90"], ["parseSlot", ""], ["parseSlot", "abc"],
                               ["parseSlot", "0:00"], ["parseSlot", "24:00:00"], ["parseSlot", "12:75"]),
                         [900, 5400, "bad", None, "bad", "bad", "bad", "bad"])   # a bare number is ambiguous, so it is refused


class SlotUi(unittest.TestCase):
    def test_the_narrow_layout_overrides_the_slot_columns(self):
        css = FILES["operator.css"]
        self.assertRegex(css, r"\.rrow,\.rrow\.hb\{grid-template-columns:1\.8rem 7\.5rem minmax\(0,1fr\) auto\}")

    def test_the_page_wires_the_slot(self):
        js, css = FILES["operator.js"], FILES["operator.css"]
        self.assertIn("showSlot:", js)
        self.assertIn("backTimes(", js[js.index("function renderShow"):])
        self.assertIn("slotSeconds", js[js.index("function parseRundown"):js.index("function parseRundown") + 700])
        for needle in ("'Over '", "'Under '", "'On slot'", "'Back'"):
            self.assertIn(needle, js)
        self.assertRegex(css, r"\.rrow\.hb,\.rhead\.hb\{grid-template-columns:[^}]*6\.5rem 6\.5rem 16rem\}")
        self.assertNotIn("auto", re.search(r"\.rrow\.hb,\.rhead\.hb\{grid-template-columns:([^}]*)\}", css).group(1))

    def test_the_slot_control_lives_in_the_build_panel_and_is_hidden_in_operator(self):
        js, css = FILES["operator.js"], FILES["operator.css"]
        build = js[js.index("function renderBuild"):js.index("function renderBuild") + 2500]
        self.assertIn("build-slot", build)
        self.assertRegex(css, r"\.ws-operator #build[^{]*\{[^}]*display:none")   # read-only in the Operator layout


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class ScheduleUi(unittest.TestCase):
    def test_chip_kinds(self):
        got = _node(["scheduleChip", None, False], ["scheduleChip", None, True], ["scheduleChip", "claimed", True], ["scheduleChip", "fired", True],
                    ["scheduleChip", "failed", True], ["scheduleChip", "missed", True], ["scheduleChip", "held", True],
                    ["scheduleChip", "blocked", True], ["scheduleChip", "some-future-state", True])
        self.assertEqual(got, [None, ["pending", "Scheduled"], ["pending", "Claimed"], ["ready", "Fired"], ["failed", "Failed"],
                               ["failed", "Missed"], ["locked", "Held"], ["failed", "Blocked"], ["pending", "some-future-state"]])

    def test_local_time_to_utc_text(self):
        got = _node(["localToUtc", "2026-11-01T20:00"], ["localToUtc", ""], ["localToUtc", "nonsense"])
        self.assertRegex(got[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(got[1:], [None, "bad"])

    def test_wiring(self):
        js = FILES["operator.js"]
        self.assertIn("itemSchedule:", js)
        self.assertIn("build-sched", js[js.index("function renderBuild"):js.index("function renderBuild") + 4500])
        self.assertIn("scheduleState", js[js.index("function parseRundown"):js.index("function parseRundown") + 800])
        self.assertIn("scheduleChip(", js[js.index("function rundownRow"):js.index("function rundownRow") + 3500])


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class RepeatUi(unittest.TestCase):
    def test_pure_text_and_chip(self):
        got = _node(["ruleText", {"freq": "daily", "at": "20:00", "tz": "America/New_York"}],
                    ["ruleText", {"freq": "weekly", "days": [0, 2], "at": "09:30", "tz": "UTC"}], ["ruleText", None],
                    ["scheduleChip", None, False, "after_previous"], ["scheduleChip", None, True, "after_previous"], ["scheduleChip", "fired", False, "after_previous"])
        self.assertEqual(got, ["Repeats daily at 20:00 America/New_York", "Repeats weekly on Mon, Wed at 09:30 UTC", "",
                               ["pending", "After previous"], ["pending", "Scheduled"], ["ready", "Fired"]])

    def test_wiring(self):
        js = FILES["operator.js"]
        for needle in ("showRecurrence:", "build-after", "build-rec-at", "build-rec-tz", "Stop repeating", "scheduleOffsetSeconds"):
            self.assertIn(needle, js, needle)


class FcpxmlUi(unittest.TestCase):
    def test_the_export_tab_offers_fcpxml(self):
        js = FILES["operator.js"]
        self.assertIn("xRadio('fmt', 'fcpxml'", js)
        self.assertIn("d.format === 'fcpxml' ? 'fcpxml'", js)
