"""Story and show templates (plan Task 8)."""

import json
import unittest

from tests.operator_harness import seed
from tests.test_operator_api import TOKEN, Api
from volt import cues, serve, story, templates
from volt.playout import hold


class Templates(Api):
    def setUp(self):
        super().setUp()
        serve.ROOM, self.show, _ = seed(self.store, "scripts")
        self.item = {r["slug"]: r["id"] for r in self.store.conn.execute("SELECT id, slug FROM item WHERE show_id = ?", (self.show,))}

    def mk_show_template(self, name="Evening"):
        s, out = self.post("/api/templates", {"kind": "show", "name": name, "fromShow": self.show})
        self.assertEqual(s, 200, out)
        return out["template"]["id"]

    def row(self, item_id):
        return self.store.conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()

    def test_instantiated_stories_are_draft_rev_1_no_approval(self):
        s, out = self.post("/api/templates", {"kind": "story", "name": "Lede", "fromItem": self.item["Open / theme"]})   # an approved, on-air story
        tid = out["template"]["id"]
        _, made = self.post(f"/api/templates/{tid}/instantiate", {"show": self.show})
        r = self.row(made["item"])
        self.assertEqual((r["status"], r["story_status"], r["script_rev"], r["approved_by"], r["approved_at"], r["approved_rev"], bool(r["floated"])),
                         ("ready", "draft", 1, None, None, None, False))
        self.assertEqual(r["slug"], "Open / theme")
        self.assertEqual(made["rundown"]["items"][-1]["id"], made["item"])
        ev = self.store.conn.execute("SELECT kind FROM story_event WHERE item_id = ?", (made["item"],)).fetchall()
        self.assertEqual([e["kind"] for e in ev], ["template"])

    def test_show_template_carries_cues_slot_pace_not_spans(self):
        self.store.conn.execute("UPDATE show SET slot_seconds = 1800, read_wpm = 140 WHERE id = ?", (self.show,))
        self.store.conn.commit()
        cues.add_cue(self.store, self.item["Game action"], "corevideo.pro", "overlay.take", "on")
        self.assertTrue(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ? AND span_id IS NOT NULL", (self.show,)).fetchone()[0])
        tid = self.mk_show_template()
        body = json.loads(self.store.conn.execute("SELECT body FROM story_template WHERE id = ?", (tid,)).fetchone()[0])
        self.assertEqual((body["slotSeconds"], body["wpm"]), (1800, 140))
        self.assertNotIn("span", json.dumps(body).lower())
        s, out = self.post(f"/api/templates/{tid}/instantiate", {"title": "Tomorrow"})
        self.assertEqual(s, 200, out)
        new = out["show"]["id"]
        sh = self.store.conn.execute("SELECT title, slot_seconds, read_wpm FROM show WHERE id = ?", (new,)).fetchone()
        self.assertEqual(tuple(sh), ("Tomorrow", 1800, 140))
        items = self.store.conn.execute("SELECT * FROM item WHERE show_id = ? ORDER BY position", (new,)).fetchall()
        self.assertTrue(all(i["status"] == "ready" and i["span_id"] is None and i["story_status"] == "draft" and i["script_rev"] == 1
                            and i["approved_by"] is None and i["as_run_in"] is None for i in items))
        game = [i for i in items if i["slug"] == "Game action"][0]
        got = self.store.conn.execute("SELECT room, command, payload, status FROM cue WHERE item_id = ? ORDER BY position", (game["id"],)).fetchall()
        want = [tuple(c) for c in self.store.conn.execute("SELECT room, command, payload FROM cue WHERE item_id = ? ORDER BY position", (self.item["Game action"],))]
        self.assertGreaterEqual(len(want), 3)
        self.assertEqual([c[:3] for c in map(tuple, got)], want)                                          # every cue carried, in order
        self.assertEqual({c["status"] for c in got}, {"stored"})                                          # none armed or fired

    def test_from_show_skips_killed_skipped_floated_and_killed_stories(self):
        tid = self.mk_show_template()
        slugs = [s["slug"] for s in json.loads(self.store.conn.execute("SELECT body FROM story_template WHERE id = ?", (tid,)).fetchone()[0])["stories"]]
        self.assertEqual(slugs, ["Pre-show", "Open / theme", "Game action"])        # not Tough call (killed), Commercial break (skipped), Player spotlight (floated), Under review (story killed)

    def test_instantiate_show_with_a_cue_the_server_no_longer_accepts_counts_it(self):
        body = {"slotSeconds": None, "wpm": None, "stories": [{"slug": "A", "script": "x", "cues": [{"room": "corevideo.pro", "command": "no.such", "payload": ""},
                                                                                                    {"room": "corevideo.pro", "command": "overlay.take", "payload": "on"}]}]}
        tid = templates.create(self.store, "show", "Odd", body, "desk")
        _, out = self.post(f"/api/templates/{tid}/instantiate", {"title": "Odd show"})
        self.assertEqual((len(out["items"]), out["skippedCues"]), (1, 1))

    def test_slug_date_token(self):
        tid = templates.create(self.store, "story", "Dated", {"slug": "Weather {date}", "script": ""}, "desk")
        _, out = self.post(f"/api/templates/{tid}/instantiate", {"show": self.show})
        self.assertRegex(self.row(out["item"])["slug"], r"^Weather \d{4}-\d{2}-\d{2}$")

    def test_template_body_validated(self):
        bad = [("story", {"slug": "x", "script": "", "extra": 1}), ("story", {"slug": "", "script": ""}), ("story", {"slug": "x", "script": "y" * 20001}),
               ("story", "text"), ("show", {"stories": "no"}), ("show", {"slotSeconds": 0}), ("show", {"wpm": 10}), ("show", {"nope": 1}),
               ("show", {"stories": [{"slug": "a", "cues": [{"room": "r"}]}]}), ("show", {"stories": [{"slug": "a"}] * 101}),
               ("other", {})]
        for kind, body in bad:
            with self.subTest(kind=kind, body=str(body)[:40]):
                s, p = self.post("/api/templates", {"kind": kind, "name": "n", "body": body})
                self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "", "body": {"slug": "a"}})[0], 422)
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "n"})[0], 422)                               # no source
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "n", "body": {"slug": "a"}, "fromItem": "x"})[0], 422)   # two sources
        self.assertEqual(self.post("/api/templates", {"kind": "show", "name": "n", "fromItem": self.item["Game action"]})[0], 422)       # wrong source for kind
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "n", "fromItem": "nope"})[0], 404)
        self.assertEqual(self.post("/api/templates", {"kind": "show", "name": "n", "fromShow": "nope"})[0], 404)

    def test_a_body_over_the_size_limit_is_refused(self):
        with self.assertRaises(templates.TemplateError) as cm:
            templates.check_body("show", {"stories": [{"slug": "a", "script": "z" * 20000}] * 15})
        self.assertEqual((cm.exception.status, cm.exception.code), (422, "bad_input"))

    def test_delete_is_admin_only(self):
        tid = templates.create(self.store, "story", "x", {"slug": "a"}, "desk")
        self.assertEqual(serve.ROUTE_ROLES["act_template_delete"], "admin")
        self.assertEqual(serve.ROUTE_ROLES["act_template_create"], "operator")
        self.assertEqual(serve.ROUTE_ROLES["act_template_instantiate"], "operator")
        from volt import users
        _, op = users.add(self.store, "olive", "operator")
        _, admin = users.add(self.store, "root", "admin")
        self.assertEqual(self.post(f"/api/templates/{tid}/delete", {}, token=op)[0], 403)
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "n", "body": {"slug": "a"}}, token=op)[0], 200)    # an operator may make one
        s, out = self.post(f"/api/templates/{tid}/delete", {}, token=admin)
        self.assertEqual((s, out["ok"]), (200, True))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM story_template WHERE id = ?", (tid,)).fetchone()[0], 0)
        self.assertEqual(self.post(f"/api/templates/{tid}/delete", {}, token=admin)[0], 404)

    def test_instantiate_unknown_template_404_and_wrong_kind(self):
        self.assertEqual(self.post("/api/templates/nope/instantiate", {"show": self.show})[0], 404)
        sid = templates.create(self.store, "story", "s", {"slug": "a"}, "desk")
        shid = templates.create(self.store, "show", "h", {"stories": []}, "desk")
        self.assertEqual(self.post(f"/api/templates/{sid}/instantiate", {"title": "x"})[0], 422)          # a story template needs a show
        self.assertEqual(self.post(f"/api/templates/{sid}/instantiate", {"show": "nope"})[0], 404)
        self.assertEqual(self.post(f"/api/templates/{shid}/instantiate", {"show": self.show})[0], 422)    # a show template needs a title
        with self.assertRaises(templates.TemplateError) as cm:
            templates.instantiate_show(self.store, sid, "t", "desk")
        self.assertEqual(cm.exception.code, "wrong_kind")

    def test_template_text_with_private_notes_kept_for_editors_never_on_the_prompter(self):
        note = "// Check the final score with Dana before 6pm"
        _, out = self.post("/api/templates", {"kind": "story", "name": "Lede", "fromItem": self.item["Game action"]})
        tid = out["template"]["id"]
        self.assertIn(note, self.store.conn.execute("SELECT body FROM story_template WHERE id = ?", (tid,)).fetchone()[0])        # kept for editors
        _, made = self.post(f"/api/templates/{tid}/instantiate", {"show": self.show})
        self.assertIn(note, self.row(made["item"])["script"])                                                                      # the editor can still see and remove it
        story.set_status(self.store, made["item"], "ready", base_rev=1, actor="dana")
        status, feed, _ = self.call("GET", f"/api/shows/{self.show}/prompter")
        self.assertNotIn("Dana", json.dumps(feed))                                                                                  # a draft is not on the prompter, and no note ever is
        self.assertNotIn("Check the final score", json.dumps(feed))

    def test_list_roles_audit_and_token(self):
        tid = self.mk_show_template("A show")
        s, out, _ = self.call("GET", "/api/templates")
        self.assertEqual((s, [(t["id"], t["kind"], t["name"], t["stories"]) for t in out["templates"]]), (200, [(tid, "show", "A show", 3)]))
        self.assertNotIn("body", out["templates"][0])
        actions = [r["action"] for r in self.store.conn.execute("SELECT action FROM audit WHERE target_kind = 'template'")]
        self.assertEqual(actions, ["template_create"])
        serve.TOKEN = TOKEN
        self.assertEqual(self.call("GET", "/api/templates")[0], 401)
        self.assertEqual(self.post("/api/templates", {"kind": "story", "name": "n", "body": {"slug": "a"}})[0], 401)

    def test_instantiating_works_on_a_held_show_it_is_not_a_take(self):
        hold(self.store, self.show, "held")
        tid = templates.create(self.store, "story", "s", {"slug": "a", "script": "b"}, "desk")
        self.assertEqual(self.post(f"/api/templates/{tid}/instantiate", {"show": self.show})[0], 200)


if __name__ == "__main__":
    unittest.main()
