import json
import re
import unittest
from pathlib import Path

from tests.operator_harness import seed
from tests.test_operator_api import TOKEN, Api
from volt import serve

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
        self.assertGreaterEqual(len(pairs), 13)
        for method, tpl in pairs:
            path = tpl.replace("{id}", "x1")
            if method == "POST":
                self.assertTrue(any(p.match(path) for p, _ in serve.ROUTES), tpl)
            else:
                status, payload, _ = self.call("GET", path)
                self.assertEqual(status, 200, tpl)
                self.assertIn("rundown", payload)

    def test_every_post_the_page_uses_is_answered_not_404(self):
        # A wrong id is a stable refusal (404 not_found or 422), never an unknown route.
        for method, tpl in re.findall(r"\['(POST)',\s*'(/api/[^']*)'\]", FILES["operator.js"]):
            status, payload = self.post(tpl.replace("{id}", "nope"), {})
            self.assertFalse(payload["ok"], tpl)
            self.assertIn(status, (404, 422, 503), tpl)
            self.assertTrue(payload["error"]["reason"], tpl)


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
