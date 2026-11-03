"""The prompter feed, its show-scoped ticket, the text export and the page (docs/superpowers/plans/2026-11-01-newsroom-scripts.md, Task 6)."""

import http.client
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.operator_harness import seed
from tests.test_operator_api import TOKEN, Api
from volt import serve, story, tickets
from volt.rundown import open_show

NOTES = ("Check the final score with Dana", "Waiting on the league for the ruling")
SESSION = Path(serve.ROOT) / "operator"
NODE = shutil.which("node")


class Prompter(Api):
    def setUp(self):
        super().setUp()
        serve.ROOM, self.show, self.spans = seed(self.store, "scripts")
        self._media = serve.MEDIA_KEY
        self.items = {r["slug"]: r["id"] for r in self.store.conn.execute("SELECT id, slug FROM item WHERE show_id = ?", (self.show,))}

    def tearDown(self):
        serve.MEDIA_KEY = self._media
        super().tearDown()

    def feed(self, **kw):
        return self.call("GET", f"/api/shows/{self.show}/prompter", **kw)

    def raw(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read(), r
        finally:
            conn.close()

    def ticket(self, ttl=None):
        serve.TOKEN = TOKEN
        status, out = self.post(f"/api/shows/{self.show}/prompter-ticket", {} if ttl is None else {"ttlSeconds": ttl}, token=TOKEN)
        self.assertEqual(status, 200, out)
        return out

    # ---- the feed -------------------------------------------------------------------------------------------
    def test_feed_omits_notes_floated_killed_aired_skipped(self):
        status, feed, _ = self.feed()
        self.assertEqual(status, 200)
        self.assertEqual([i["slug"] for i in feed["items"]], ["Open / theme", "Game action"])
        text = json.dumps(feed)
        for note in NOTES:
            self.assertNotIn(note, text)
        for gone in ("Pre-show", "Under review", "Tough call", "Player spotlight", "Commercial break"):
            self.assertNotIn(gone, text)
        kinds = {b["kind"] for i in feed["items"] for b in i["blocks"]}
        self.assertEqual(kinds, {"read", "instruction"})

    def test_feed_order_and_current(self):
        _, feed, _ = self.feed()
        self.assertEqual(feed["current"], self.items["Open / theme"])
        self.assertEqual(feed["wpm"], 160)
        self.assertEqual(feed["show"], self.show)

    def test_feed_requires_approved_when_gate_on(self):
        with mock.patch.dict(os.environ, {"VOLT_REQUIRE_APPROVAL": "1"}):
            _, feed, _ = self.feed()
        self.assertEqual([i["slug"] for i in feed["items"]], ["Open / theme"])
        self.assertEqual(feed["current"], self.items["Open / theme"])

    def test_gate_on_an_edit_after_approval_drops_the_story_from_the_feed(self):
        story.set_script(self.store, self.items["Open / theme"], script="New words.", base_rev=2, actor="dana")
        with mock.patch.dict(os.environ, {"VOLT_REQUIRE_APPROVAL": "1"}):
            _, feed, _ = self.feed()
        self.assertEqual(feed["items"], [])

    def test_version_changes_only_with_content(self):
        _, a, _ = self.feed()
        _, b, _ = self.feed()
        self.assertEqual(a["version"], b["version"])
        # a note-only edit bumps the revision but not what the page shows
        row = self.store.conn.execute("SELECT script, script_rev FROM item WHERE id = ?", (self.items["Game action"],)).fetchone()
        story.set_script(self.store, self.items["Game action"], script=row["script"].replace("Dana", "Dave"), base_rev=row["script_rev"], actor="dana")
        _, c, _ = self.feed()
        self.assertEqual(a["version"], c["version"])
        story.set_script(self.store, self.items["Game action"], script="Different read words.", base_rev=row["script_rev"] + 1, actor="dana")
        _, d, _ = self.feed()
        self.assertNotEqual(a["version"], d["version"])

    def test_unknown_show_is_404(self):
        status, payload, _ = self.call("GET", "/api/shows/nope/prompter")
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))

    # ---- the ticket -----------------------------------------------------------------------------------------
    def test_ticket_reads_only_its_show_feed(self):
        out = self.ticket()
        self.assertTrue(out["url"].startswith(f"/prompter?show={self.show}&ticket="))
        token = out["url"].split("ticket=")[1]
        feed_path = f"/api/shows/{self.show}/prompter?ticket={token}"
        self.assertEqual(self.raw(feed_path)[0], 200)                     # no Authorization header at all
        other = open_show(self.store, "Another show")
        for path in (f"/api/shows/{other}/prompter?ticket={token}",     # another show
                     f"/api/shows/{self.show}/script?format=text&ticket={token}",   # another route
                     f"/api/state?ticket={token}", f"/rundown?show={self.show}&ticket={token}",
                     f"/api/items/{self.items['Game action']}/story?ticket={token}",
                     f"/api/assets/{self.items['Game action']}/proxy?ticket={token}",   # a proxy route, with a show-bound ticket
                     f"/api/shows/{self.show}/prompter/extra?ticket={token}"):      # a path that only looks like it
            with self.subTest(path=path):
                self.assertEqual(self.raw(path)[0], 401, path)
        self.assertEqual(self.raw(feed_path + "&show=" + str(other))[0], 422)         # no extra parameters

    def test_a_ticket_cannot_be_used_to_post(self):
        token = self.ticket()["url"].split("ticket=")[1]
        status, _ = self.post(f"/api/shows/{self.show}/hold?ticket={token}", {})
        self.assertEqual(status, 401)

    def test_a_media_ticket_of_the_same_id_is_not_a_prompter_ticket(self):
        serve.TOKEN = TOKEN
        tok, _ = tickets.issue(TOKEN, self.show, "proxy", 600, int(__import__("time").time()))
        self.assertEqual(self.raw(f"/api/shows/{self.show}/prompter?ticket={tok}")[0], 401)

    def test_ticket_expired_refused(self):
        out = self.ticket(ttl=60)
        token = out["url"].split("ticket=")[1]
        path = f"/api/shows/{self.show}/prompter?ticket={token}"
        self.assertEqual(self.raw(path)[0], 200)
        with mock.patch.object(serve.time, "time", return_value=out["expiresAt"] + 1):
            self.assertEqual(self.raw(path)[0], 401)

    def test_a_garbled_ticket_is_refused(self):
        serve.TOKEN = TOKEN
        for bad in ("x", "1.abc", ""):
            self.assertEqual(self.raw(f"/api/shows/{self.show}/prompter?ticket={bad}")[0] in (401, 403), True, bad)

    def test_ticket_issue_needs_operator_and_a_signing_key(self):
        serve.TOKEN, serve.MEDIA_KEY = None, None                       # loopback, open: no key to sign with
        status, payload = self.post(f"/api/shows/{self.show}/prompter-ticket", {})
        self.assertEqual((status, payload["error"]["code"]), (503, "no_media_key"))
        self.assertEqual(serve.ROUTE_ROLES["act_prompter_ticket"], "operator")
        serve.TOKEN = TOKEN
        self.assertEqual(self.post(f"/api/shows/{self.show}/prompter-ticket", {})[0], 401)   # no bearer
        self.assertEqual(self.post(f"/api/shows/{self.show}/prompter-ticket", {"ttlSeconds": 13 * 3600}, token=TOKEN)[0], 422)
        self.assertEqual(self.post(f"/api/shows/{self.show}/prompter-ticket", {"x": 1}, token=TOKEN)[0], 422)
        self.assertEqual(self.post("/api/shows/nope/prompter-ticket", {}, token=TOKEN)[0], 404)

    def test_issuing_is_audited_without_the_ticket(self):
        self.ticket()
        rows = self.store.conn.execute("SELECT * FROM audit WHERE action LIKE '%prompter%'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("ticket=", " ".join(str(v) for v in tuple(rows[0])))

    # ---- the text export ------------------------------------------------------------------------------------
    def test_text_export_has_no_notes(self):
        status, body, resp = self.raw(f"/api/shows/{self.show}/script?format=text")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/plain"))
        self.assertIn("OPEN / THEME", text)
        self.assertIn("Good evening, and welcome to Final Four Watch.", text)
        for hidden in NOTES + ("[SOT", "[CAM", "Player spotlight", "PLAYER SPOTLIGHT"):
            self.assertNotIn(hidden, text)
        self.assertEqual(self.raw(f"/api/shows/{self.show}/script")[0], 422)

    # ---- the page -------------------------------------------------------------------------------------------
    def test_prompter_page_static_csp(self):
        serve.TOKEN = TOKEN
        for path, ctype in (("/prompter", "text/html"), ("/operator/prompter.js", "text/javascript"), ("/operator/prompter.css", "text/css")):
            status, body, resp = self.raw(path)
            self.assertEqual(status, 200, path)                          # open without a token, like the operator page
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype))
            self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
            self.assertNotRegex(body.decode("utf-8"), r"https?://")
        html = (SESSION / "prompter.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("<style", html)
        self.assertNotIn("style=", html)
        for ref in re.findall(r'(?:src|href)="([^"]*)"', html):
            self.assertTrue(ref.startswith("/operator/") or ref.startswith("data:"), ref)
        self.assertRegex(html, r'<html lang="en">')
        self.assertIn('aria-live="polite"', html)

    def test_page_script_builds_text_nodes_only(self):
        js = (SESSION / "prompter.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setAttribute('style'", "eval("):
            self.assertNotIn(forbidden, js)
        self.assertNotIn("sessionStorage", js)
        self.assertIn("textContent", js)
        self.assertRegex(js, r"try \{ const v = localStorage")

    @unittest.skipUnless(NODE, "node is needed to run the page's functions")
    def test_hostile_script_text_is_drawn_as_text(self):
        evil = '<img src=x onerror=alert(1)> & <script>x</script>'
        feed = {"current": "a", "items": [{"id": "a", "slug": evil, "blocks": [{"kind": "read", "text": evil, "para": 0},
                                                                              {"kind": "note", "text": "SECRET", "para": 0},
                                                                              {"kind": "instruction", "text": "[" + evil + "]", "para": 0}]}]}
        script = ("const m = require(process.argv[1]); console.log(JSON.stringify(m.lines(JSON.parse(process.argv[2]))));")
        out = subprocess.run([NODE, "-e", script, str(SESSION / "prompter.js"), json.dumps(feed)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual([l["kind"] for l in got], ["slug", "read", "instruction"])      # the note never reaches the page
        self.assertEqual(got[1]["text"], evil)                                              # passed through untouched, drawn with textContent
        self.assertNotIn("SECRET", out.stdout)


if __name__ == "__main__":
    unittest.main()
