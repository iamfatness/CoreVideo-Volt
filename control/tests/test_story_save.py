"""Story revisions and the optimistic script save (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md).

HTTP tests over a real server, each on SQLite and (with VOLT_TEST_PG_DSN) live Postgres. The rules live in
volt/story.py; serve.py only reads the body, calls it, and maps the refusals.
"""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from tests.storykit import Behaviour, build
from volt import clock, serve, story
from volt.store import _now


class Save(Behaviour):
    def setUp(self):
        super().setUp()
        self.show = self.new_show()
        self.item = self.new_item(self.show, "Bridge vote", "Council voted.")

    def save(self, **body):
        return self.post(f"/api/items/{self.item}/script", body)

    def test_a_new_item_reads_as_draft_revision_one(self):
        s, p = self.get(f"/api/items/{self.item}/story")
        self.assertEqual(s, 200, p)
        self.assertEqual((p["rev"], p["storyStatus"], p["slug"], p["script"]), (1, "draft", "Bridge vote", "Council voted."))
        self.assertEqual((p["approvedBy"], p["approvedAt"], p["approvedRev"], p["floated"]), (None, None, None, False))
        self.assertEqual(p["stats"]["words"], 2)
        self.assertEqual([r["rev"] for r in p["revisions"]], [1])

    def test_save_bumps_rev_and_stores_revision(self):
        s, p = self.save(script="Council voted six to three.", baseRev=1)
        self.assertEqual(s, 200, p)
        self.assertEqual((p["ok"], p["rev"], p["changed"], p["storyStatus"]), (True, 2, True, "draft"))
        self.assertEqual(p["stats"]["words"], 5)
        row = self.row(self.item)
        self.assertEqual((row["script"], row["script_rev"]), ("Council voted six to three.", 2))
        # the revision before the first edit is kept too, so the history is complete
        self.assertEqual(self.revs(self.item), [1, 2])
        got = {r["rev"]: r for r in self.store.conn.execute("SELECT * FROM story_rev WHERE item_id = ?", (self.item,))}
        self.assertEqual(got[1]["script"], "Council voted.")
        self.assertEqual((got[2]["script"], got[2]["slug"], got[2]["actor"]), ("Council voted six to three.", "Bridge vote", "desk"))
        s, g = self.get(f"/api/items/{self.item}/story")
        self.assertEqual([r["rev"] for r in g["revisions"]], [2, 1])
        self.assertEqual(g["revisions"][0]["actor"], "desk")

    def test_slug_alone_bumps_the_rev(self):
        p = self.ok(f"/api/items/{self.item}/script", {"slug": "  Bridge  ", "baseRev": 1})
        self.assertEqual(p["rev"], 2)
        self.assertEqual(self.row(self.item)["slug"], "Bridge")
        self.assertEqual(self.row(self.item)["script"], "Council voted.")

    def test_same_text_is_noop_same_rev(self):
        p = self.ok(f"/api/items/{self.item}/script", {"script": "Council voted.", "slug": "Bridge vote", "baseRev": 1})
        self.assertEqual((p["rev"], p["changed"]), (1, False))
        self.assertEqual(self.revs(self.item), [])
        self.assertEqual(self.row(self.item)["script_rev"] or 1, 1)
        self.assertEqual(self.events(self.item), [])

    def test_stale_base_rev_409_with_current_text(self):
        self.ok(f"/api/items/{self.item}/script", {"script": "Winner.", "baseRev": 1}, token=None)
        p = self.refused(409, "stale_script", f"/api/items/{self.item}/script", {"script": "Loser.", "baseRev": 1})
        cur = p["current"]
        self.assertEqual((cur["rev"], cur["script"], cur["slug"], cur["actor"]), (2, "Winner.", "Bridge vote", "desk"))
        self.assertTrue(cur["at"])
        self.assertEqual(self.row(self.item)["script"], "Winner.")
        self.assertEqual(self.revs(self.item), [1, 2])

    def test_two_saves_at_one_revision_one_wins(self):
        gate = threading.Barrier(6)

        def one(n):
            gate.wait()
            return self.post(f"/api/items/{self.item}/script", {"script": f"Version {n}.", "baseRev": 1})[0]

        with ThreadPoolExecutor(6) as pool:
            codes = sorted(pool.map(one, range(6)))
        self.assertEqual(codes, [200, 409, 409, 409, 409, 409])
        self.assertEqual(self.row(self.item)["script_rev"], 2)

    def test_aired_item_refused_not_editable(self):
        other = self.new_item(self.show, "Second")
        self.with_media(self.item, other)
        self.ok(f"/api/shows/{self.show}/go")
        self.ok(f"/api/shows/{self.show}/go")   # first is aired, second on air
        self.assertEqual(self.status(self.item), "aired")
        self.refused(409, "not_editable", f"/api/items/{self.item}/script", {"script": "x", "baseRev": 1})
        self.assertEqual(self.row(self.item)["script"], "Council voted.")

    def test_skipped_and_killed_items_refused_not_editable(self):
        a, b = self.new_item(self.show, "A"), self.new_item(self.show, "B")
        self.ok(f"/api/items/{a}/skip")
        self.ok(f"/api/items/{b}/kill")
        for item in (a, b):
            self.refused(409, "not_editable", f"/api/items/{item}/script", {"script": "x", "baseRev": 1})

    def test_on_air_item_allowed_bumps_rev(self):
        self.with_media(self.item)
        self.ok(f"/api/shows/{self.show}/go")
        self.assertEqual(self.status(self.item), "on air")
        p = self.ok(f"/api/items/{self.item}/script", {"script": "Updated live.", "baseRev": 1})
        self.assertEqual(p["rev"], 2)
        self.assertEqual(self.status(self.item), "on air")   # playout is not touched

    def test_over_20000_chars_422(self):
        self.refused(422, "bad_input", f"/api/items/{self.item}/script", {"script": "x" * 20001, "baseRev": 1})
        self.ok(f"/api/items/{self.item}/script", {"script": "x" * 20000, "baseRev": 1})

    def test_bad_bodies_422(self):
        path = f"/api/items/{self.item}/script"
        for body in ({"baseRev": 1}, {"script": "x"}, {"script": "x", "baseRev": "1"}, {"script": "x", "baseRev": 0},
                     {"script": "x", "baseRev": True}, {"script": "x", "baseRev": 1, "extra": 1},
                     {"slug": "   ", "baseRev": 1}, {"script": 5, "baseRev": 1}):
            with self.subTest(body=body):
                self.refused(422, "bad_input", path, body)
        self.assertEqual(self.revs(self.item), [])

    def test_unknown_item_404(self):
        self.refused(404, "not_found", "/api/items/nope/script", {"script": "x", "baseRev": 1})
        s, p = self.get("/api/items/nope/story")
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))

    def test_story_killed_is_not_editable_until_revived(self):
        self.store.conn.execute("UPDATE item SET story_status = 'killed' WHERE id = ?", (self.item,))
        self.store.conn.commit()
        self.refused(409, "story_killed", f"/api/items/{self.item}/script", {"script": "x", "baseRev": 1})

    def test_prune_keeps_50_and_the_approved_rev(self):
        # A save always drops the approval, so an approval never points at an old revision in normal use; the rule is a
        # backstop, tested on the function directly: 60 revisions, approval pinned at rev 3.
        for n in range(1, 61):
            self.store.conn.execute(
                "INSERT INTO story_rev (item_id, rev, slug, script, actor, at) VALUES (?, ?, 's', ?, 'desk', ?)",
                (self.item, n, f"v{n}", _now()))
        self.store.conn.execute(
            "UPDATE item SET story_status = 'approved', script_rev = 60, approved_by = 'ed', approved_at = ?, approved_rev = 3 WHERE id = ?",
            (_now(), self.item))
        self.store.conn.commit()
        story.prune_revisions(self.store, self.item)
        revs = self.revs(self.item)
        self.assertEqual(len(revs), 51)               # newest 50, plus the approved one
        self.assertEqual((revs[0], revs[1], revs[-1]), (3, 11, 60))

    def test_prune_without_approval_keeps_exactly_50(self):
        for n in range(2, 66):
            self.ok(f"/api/items/{self.item}/script", {"script": f"v{n}", "baseRev": n - 1})
        revs = self.revs(self.item)
        self.assertEqual((len(revs), revs[0], revs[-1]), (50, 16, 65))

    def test_editing_an_approved_story_drops_the_approval(self):
        self.store.conn.execute(
            "UPDATE item SET story_status = 'approved', approved_by = 'ed', approved_at = ?, approved_rev = 1 WHERE id = ?",
            (_now(), self.item))
        self.store.conn.commit()
        p = self.ok(f"/api/items/{self.item}/script", {"script": "Changed.", "baseRev": 1})
        self.assertEqual(p["storyStatus"], "ready")
        row = self.row(self.item)
        self.assertEqual((row["story_status"], row["approved_by"], row["approved_at"], row["approved_rev"]), ("ready", None, None, None))
        ev = self.events(self.item)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["from_value"], ev[0]["to_value"], ev[0]["rev"], ev[0]["note"], ev[0]["actor"]),
                         ("status", "approved", "ready", 2, "edited after approval", "desk"))

    def test_rename_with_script_goes_through_set_script(self):
        self.store.conn.execute(
            "UPDATE item SET story_status = 'approved', approved_by = 'ed', approved_at = ?, approved_rev = 1 WHERE id = ?",
            (_now(), self.item))
        self.store.conn.commit()
        self.ok(f"/api/items/{self.item}/rename", {"script": "Renamed text."})   # baseRev optional here
        row = self.row(self.item)
        self.assertEqual((row["script"], row["script_rev"], row["story_status"], row["approved_by"]), ("Renamed text.", 2, "ready", None))
        self.assertEqual(self.revs(self.item), [1, 2])
        self.ok(f"/api/items/{self.item}/rename", {"slug": "New slug"})
        self.assertEqual(self.row(self.item)["script_rev"], 3)
        self.ok(f"/api/items/{self.item}/rename", {"slug": "New slug", "script": "Renamed text."})   # nothing changed
        self.assertEqual(self.row(self.item)["script_rev"], 3)

    def test_rename_still_refuses_an_item_that_is_not_ready(self):
        self.with_media(self.item)
        self.ok(f"/api/shows/{self.show}/go")
        self.refused(409, "not_editable", f"/api/items/{self.item}/rename", {"script": "x"})

    def test_route_roles_and_audit(self):
        self.assertEqual(serve.ROUTE_ROLES["act_story_script"], "operator")
        op = self.user("dana", "operator")
        self.ok(f"/api/items/{self.item}/script", {"script": "By dana.", "baseRev": 1}, token=op)
        rows = self.store.list_audit(self.item)
        self.assertEqual([(r["actor"], r["action"], r["target_kind"]) for r in rows], [("dana", "script", "item")])
        self.assertEqual(self.revs(self.item), [1, 2])
        got = self.store.conn.execute("SELECT actor FROM story_rev WHERE item_id = ? AND rev = 2", (self.item,)).fetchone()
        self.assertEqual(got["actor"], "dana")

    def test_a_viewer_reads_the_story_without_private_notes_and_cannot_save(self):
        self.ok(f"/api/items/{self.item}/script", {"script": "Read this.\n// PRIVATE note\n[SOT 0:05]", "baseRev": 1})
        viewer, op = self.user("vera", "viewer"), self.user("dana", "operator")
        s, v = self.get(f"/api/items/{self.item}/story", token=viewer)
        self.assertEqual(s, 200, v)
        self.assertNotIn("PRIVATE", json.dumps(v))
        self.assertEqual(v["script"], "Read this.\n[SOT 0:05]")
        s, o = self.get(f"/api/items/{self.item}/story", token=op)
        self.assertIn("// PRIVATE note", o["script"])
        self.assertEqual(v["stats"], o["stats"])
        self.refused(403, "forbidden_role", f"/api/items/{self.item}/script", {"script": "x", "baseRev": 2}, token=viewer)
        self.refused(403, "forbidden_role", f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 2}, token=viewer)

    def test_story_get_needs_a_token_when_auth_is_on(self):
        self.user("dana", "operator")
        s, _ = self.get(f"/api/items/{self.item}/story")
        self.assertEqual(s, 401)

    def test_removing_an_item_removes_its_history(self):
        self.ok(f"/api/items/{self.item}/script", {"script": "v2", "baseRev": 1})
        self.ok(f"/api/items/{self.item}/remove")
        self.assertEqual(self.revs(self.item), [])

    def test_deleting_a_show_removes_its_story_history(self):
        self.ok(f"/api/items/{self.item}/script", {"script": "v2", "baseRev": 1})
        self.ok(f"/api/shows/{self.show}/delete")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM story_rev").fetchone()[0], 0)


class Clone(Behaviour):
    def test_recurrence_clone_resets_story_fields(self):
        show = self.new_show("Daily")
        item = self.new_item(show, "Lead", "Words here.")
        self.store.conn.execute(
            "UPDATE item SET story_status = 'approved', script_rev = 7, approved_by = 'ed', approved_at = ?, approved_rev = 7, "
            "floated = ?, source_wire_id = 'w1' WHERE id = ?", (_now(), True, item))
        self.store.conn.commit()
        when = clock.utcnow()
        day = when.strftime("%Y-%m-%d")
        new_id = clock._clone_show(self.store, show, "Daily", when + timedelta(hours=1), day)
        self.assertIsNotNone(new_id)
        row = self.store.conn.execute("SELECT * FROM item WHERE show_id = ?", (new_id,)).fetchone()
        self.assertEqual(row["script"], "Words here.")
        self.assertEqual((row["story_status"], row["script_rev"]), ("draft", 1))
        self.assertEqual((row["approved_by"], row["approved_at"], row["approved_rev"], row["source_wire_id"]), (None, None, None, None))
        self.assertFalse(row["floated"])


ALL = (Save, Clone)
build(globals(), *ALL)
del Behaviour, Save, Clone

if __name__ == "__main__":
    unittest.main()
