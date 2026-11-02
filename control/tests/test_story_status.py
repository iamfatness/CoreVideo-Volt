"""Story status, the approval record and the optional air gate (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md).

HTTP tests over a real server on SQLite and (with VOLT_TEST_PG_DSN) live Postgres, plus direct calls for the take paths
that have no route of their own (the clock's scheduled take, the cue arm).
"""

import itertools
import os
import unittest
from datetime import timedelta
from unittest import mock

from tests.storykit import Behaviour, build
from tests.test_clock import Now, T0
from tests.test_rooms import _no_sleep, obs
from volt import clock, cues, story
from volt.playout import StoryNotApproved, advance
from volt.rooms import FakeRoom
from volt.store import _now

STATES = ("draft", "ready", "approved", "killed")
LEGAL = {("draft", "ready"), ("ready", "draft"), ("ready", "approved"), ("approved", "ready"),
         ("draft", "killed"), ("ready", "killed"), ("approved", "killed"), ("killed", "draft")}


class Status(Behaviour):
    def setUp(self):
        super().setUp()
        self.show = self.new_show()
        self.item = self.new_item(self.show, "Bridge vote", "Council voted.")

    def force(self, status, rev=1, item=None):
        """Put the story in a state directly (the rows an earlier session would have left)."""
        item = item or self.item
        approved = status == "approved"
        self.store.conn.execute(
            "UPDATE item SET story_status = ?, script_rev = ?, approved_by = ?, approved_at = ?, approved_rev = ? WHERE id = ?",
            (status, rev, "ed" if approved else None, _now() if approved else None, rev if approved else None, item))
        self.store.conn.commit()

    def move(self, to, base_rev=1, **kw):
        return self.post(f"/api/items/{self.item}/story-status", {"status": to, "baseRev": base_rev, **kw.pop("body", {})}, **kw)

    def story(self, token=None):
        s, p = self.get(f"/api/items/{self.item}/story", token=token)
        self.assertEqual(s, 200, p)
        return p

    # ---- the table ----
    def test_every_legal_move_and_every_illegal_pair(self):
        self.assertEqual({(a, b) for a in STATES for b in story.TRANSITIONS[a]}, LEGAL)
        for a, b in itertools.product(STATES, STATES):
            with self.subTest(move=f"{a} -> {b}"):
                self.force(a)
                s, p = self.move(b)
                if (a, b) in LEGAL:
                    self.assertEqual((s, p["ok"], p["storyStatus"]), (200, True, b), p)
                    self.assertEqual(self.row(self.item)["story_status"], b)
                else:
                    self.assertEqual((s, p["ok"], p["error"]["code"]), (409, False, "bad_transition"), p)
                    self.assertEqual(self.row(self.item)["story_status"], a)

    def test_draft_to_approved_refused(self):
        self.refused(409, "bad_transition", f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1})
        self.assertIsNone(self.row(self.item)["approved_by"])

    def test_new_item_reads_as_draft_so_it_can_become_ready(self):
        self.assertIsNone(self.row(self.item)["story_status"])     # NULL reads as draft
        self.ok(f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 1})
        self.assertEqual(self.row(self.item)["story_status"], "ready")

    # ---- the approval record ----
    def test_approve_records_actor_rev_time_and_event(self):
        ed = self.user("ed")
        self.force("ready", rev=3)
        p = self.ok(f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 3, "note": "checked with legal"}, token=ed)
        self.assertEqual((p["storyStatus"], p["approvedBy"], p["approvedRev"]), ("approved", "ed", 3))
        self.assertTrue(p["approvedAt"])
        g = self.story(token=ed)
        self.assertEqual((g["approvedBy"], g["approvedRev"], g["storyStatus"], g["rev"]), ("ed", 3, "approved", 3))
        self.assertTrue(g["approvedAt"])
        (ev,) = self.events(self.item)
        self.assertEqual((ev["kind"], ev["from_value"], ev["to_value"], ev["rev"], ev["actor"], ev["note"]),
                         ("status", "ready", "approved", 3, "ed", "checked with legal"))
        # "who approved revision 3" is one indexed query
        who = self.store.conn.execute(
            "SELECT actor FROM story_event WHERE item_id = ? AND kind = 'status' AND to_value = 'approved' AND rev = 3",
            (self.item,)).fetchone()
        self.assertEqual(who["actor"], "ed")
        self.assertEqual([r["action"] for r in self.store.list_audit(self.item)], ["story_status"])

    def test_unapprove_clears_the_record(self):
        self.force("approved")
        self.ok(f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 1})
        row = self.row(self.item)
        self.assertEqual((row["story_status"], row["approved_by"], row["approved_at"], row["approved_rev"]), ("ready", None, None, None))

    def test_edit_after_approval_returns_to_ready(self):
        ed = self.user("ed")
        self.force("ready")
        self.ok(f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1}, token=ed)
        p = self.ok(f"/api/items/{self.item}/script", {"script": "Changed.", "baseRev": 1}, token=ed)
        self.assertEqual((p["rev"], p["storyStatus"]), (2, "ready"))
        g = self.story(token=ed)
        self.assertEqual((g["storyStatus"], g["approvedBy"], g["approvedRev"]), ("ready", None, None))
        self.assertEqual([(e["to_value"], e["note"]) for e in self.events(self.item)],
                         [("approved", None), ("ready", "edited after approval")])

    def test_killed_clears_an_approval_and_revive_keeps_the_rev(self):
        self.force("approved", rev=4)
        self.ok(f"/api/items/{self.item}/story-status", {"status": "killed", "baseRev": 4})
        row = self.row(self.item)
        self.assertEqual((row["story_status"], row["approved_by"], row["approved_rev"], row["script_rev"]), ("killed", None, None, 4))
        self.ok(f"/api/items/{self.item}/story-status", {"status": "draft", "baseRev": 4})
        self.assertEqual((self.row(self.item)["story_status"], self.row(self.item)["script_rev"]), ("draft", 4))

    def test_killed_story_not_editable_until_revived(self):
        self.ok(f"/api/items/{self.item}/story-status", {"status": "killed", "baseRev": 1})
        self.refused(409, "story_killed", f"/api/items/{self.item}/script", {"script": "x", "baseRev": 1})
        self.ok(f"/api/items/{self.item}/story-status", {"status": "draft", "baseRev": 1})
        self.ok(f"/api/items/{self.item}/script", {"script": "x", "baseRev": 1})

    def test_story_kill_does_not_change_item_status(self):
        self.with_media(self.item)
        self.ok(f"/api/shows/{self.show}/go")
        self.ok(f"/api/items/{self.item}/story-status", {"status": "killed", "baseRev": 1})
        self.assertEqual(self.status(self.item), "on air")
        # and the playout kill does not touch the story
        other = self.new_item(self.show, "Other")
        self.ok(f"/api/items/{other}/kill")
        self.assertIsNone(self.row(other)["story_status"])

    def test_approving_a_killed_skipped_or_aired_item_is_refused(self):
        a, b = self.new_item(self.show, "A"), self.new_item(self.show, "B")
        self.ok(f"/api/items/{a}/kill")
        self.ok(f"/api/items/{b}/skip")
        for item in (a, b):
            self.force("ready", item=item)
            self.refused(409, "not_editable", f"/api/items/{item}/story-status", {"status": "approved", "baseRev": 1})
        self.with_media(self.item)
        self.ok(f"/api/shows/{self.show}/go")
        self.ok(f"/api/shows/{self.show}/go")
        self.assertEqual(self.status(self.item), "aired")
        self.force("ready")
        self.refused(409, "not_editable", f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1})

    def test_stale_base_rev_409(self):
        self.force("draft", rev=2)
        p = self.refused(409, "stale_script", f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 1})
        self.assertEqual(p["current"]["rev"], 2)
        self.assertEqual(self.row(self.item)["story_status"], "draft")

    def test_bad_bodies_422_and_unknown_item_404(self):
        path = f"/api/items/{self.item}/story-status"
        for body in ({"baseRev": 1}, {"status": "ready"}, {"status": "done", "baseRev": 1}, {"status": "ready", "baseRev": "1"},
                     {"status": "ready", "baseRev": 1, "note": 5}, {"status": "ready", "baseRev": 1, "note": "x" * 501},
                     {"status": "ready", "baseRev": 1, "extra": 1}):
            with self.subTest(body=body):
                self.refused(422, "bad_input", path, body)
        self.refused(404, "not_found", "/api/items/nope/story-status", {"status": "ready", "baseRev": 1})

    def test_roles(self):
        from volt import serve
        self.assertEqual(serve.ROUTE_ROLES["act_story_status"], "operator")

    # ---- who may approve ----
    def test_not_approver_403_with_env_list(self):
        alice, bob = self.user("alice"), self.user("bob")
        self.force("ready")
        with mock.patch.dict(os.environ, {"VOLT_APPROVERS": "alice, carol"}):
            self.refused(403, "not_approver", f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1}, token=bob)
            self.assertEqual(self.row(self.item)["story_status"], "ready")
            self.ok(f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1}, token=alice)
            # taking an approval back is also an approver's move; ordinary moves are not
            self.refused(403, "not_approver", f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 1}, token=bob)
            self.ok(f"/api/items/{self.item}/story-status", {"status": "killed", "baseRev": 1}, token=bob)
            self.ok(f"/api/items/{self.item}/story-status", {"status": "draft", "baseRev": 1}, token=bob)
            self.ok(f"/api/items/{self.item}/story-status", {"status": "ready", "baseRev": 1}, token=bob)

    def test_the_shared_legacy_token_cannot_approve_when_an_approver_list_is_set(self):
        # Without a per-user token the actor name is only a header anyone can write, so it proves nothing.
        self.force("ready")
        with mock.patch.dict(os.environ, {"VOLT_APPROVERS": "alice"}):
            self.refused(403, "not_approver", f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1},
                         headers={"X-Volt-Actor": "alice"})

    def test_anyone_signed_in_can_approve_when_no_list_is_set(self):
        bob = self.user("bob")
        self.force("ready")
        with mock.patch.dict(os.environ, {"VOLT_APPROVERS": ""}):
            self.ok(f"/api/items/{self.item}/story-status", {"status": "approved", "baseRev": 1}, token=bob)

    def test_can_approve_seam(self):
        class P:
            def __init__(self, name, legacy=False):
                self.name, self.role, self.legacy = name, "operator", legacy
        with mock.patch.dict(os.environ, {"VOLT_APPROVERS": "alice"}):
            self.assertTrue(story.can_approve(P("alice"), {}))
            self.assertFalse(story.can_approve(P("bob"), {}))
            self.assertFalse(story.can_approve(P("alice", legacy=True), {}))
            self.assertFalse(story.can_approve(None, {}))
        with mock.patch.dict(os.environ, {"VOLT_APPROVERS": ""}):
            self.assertTrue(story.can_approve(P("bob"), {}))
            self.assertTrue(story.can_approve(None, {}))


class Gate(Behaviour):
    """VOLT_REQUIRE_APPROVAL=1: an item whose story is not approved at its current revision cannot go to air."""

    def setUp(self):
        super().setUp()
        from volt import serve
        serve.ROOM = FakeRoom([obs("idle")] * 20)
        self.show = self.new_show()
        self.a = self.new_item(self.show, "A")
        self.b = self.new_item(self.show, "B")
        self.with_media(self.a, self.b)

    def approve(self, item, rev=1):
        self.store.conn.execute(
            "UPDATE item SET story_status = 'approved', script_rev = ?, approved_by = 'ed', approved_at = ?, approved_rev = ? WHERE id = ?",
            (rev, _now(), rev, item))
        self.store.conn.commit()

    def gate(self):
        return mock.patch.dict(os.environ, {"VOLT_REQUIRE_APPROVAL": "1"})

    def test_gate_off_by_default_takes_unapproved(self):
        self.assertFalse(story.require_approval())
        self.assertEqual(self.ok(f"/api/shows/{self.show}/go")["target"], self.a)

    def test_the_switch_reads_only_one(self):
        for value, want in (("1", True), ("0", False), ("", False), ("true", False)):
            with mock.patch.dict(os.environ, {"VOLT_REQUIRE_APPROVAL": value}):
                self.assertEqual(story.require_approval(), want, value)

    def test_gate_on_go_refuses_unapproved_leaves_state_unchanged(self):
        killed = self.new_item(self.show, "K")
        # order: A (aired later), B, K; kill one that sits in front of the unapproved target
        self.ok(f"/api/items/{self.a}/kill")
        with self.gate():
            p = self.refused(409, "story_not_approved", f"/api/shows/{self.show}/go")
            self.assertEqual(p["item"], self.b)
        self.assertEqual([self.status(i) for i in (self.a, self.b, killed)], ["killed", "ready", "ready"])   # nothing skipped, nothing on air
        self.assertIsNone(self.row(self.b)["as_run_in"])

    def test_gate_on_take_refuses_and_approved_story_goes(self):
        with self.gate():
            self.refused(409, "story_not_approved", f"/api/shows/{self.show}/take", {})
            self.assertEqual(self.status(self.a), "ready")
            self.approve(self.a)
            p = self.ok(f"/api/shows/{self.show}/take", {})
            self.assertEqual(p["item"], self.a)

    def test_gate_on_allows_approved_at_current_rev_only(self):
        self.approve(self.a, rev=1)
        self.store.conn.execute("UPDATE item SET script_rev = 2 WHERE id = ?", (self.a,))   # text moved on, approval did not
        self.store.conn.commit()
        with self.gate():
            self.refused(409, "story_not_approved", f"/api/shows/{self.show}/go")
            self.store.conn.execute("UPDATE item SET approved_rev = 2 WHERE id = ?", (self.a,))
            self.store.conn.commit()
            self.assertEqual(self.ok(f"/api/shows/{self.show}/go")["target"], self.a)

    def test_a_ready_story_with_approved_columns_left_over_is_not_approved(self):
        self.store.conn.execute(
            "UPDATE item SET story_status = 'ready', script_rev = 1, approved_by = 'ed', approved_rev = 1 WHERE id = ?", (self.a,))
        self.store.conn.commit()
        with self.gate():
            self.refused(409, "story_not_approved", f"/api/shows/{self.show}/go")

    def test_gate_covers_the_cue_arm_path(self):
        with self.gate():
            with self.assertRaises(StoryNotApproved):
                cues.arm(self.store, self.a)
            self.approve(self.a)
            cues.arm(self.store, self.a)
            self.assertEqual(self.status(self.a), "on air")

    def test_gate_on_scheduled_take_marks_failed_not_retried(self):
        clock.set_schedule(self.store, self.a, T0 + timedelta(seconds=10))
        now = Now()
        now.advance(10)
        room = FakeRoom([obs("idle")] * 5)
        with self.gate():
            events = clock.tick(self.store, room, now=now, sleep=_no_sleep, attempts=2)
            self.assertEqual([(e.item_id, e.outcome) for e in events], [(self.a, "failed")])
            self.assertIn("StoryNotApproved", events[0].note)
            self.assertEqual(self.status(self.a), "ready")
            row = self.row(self.a)
            self.assertEqual(row["schedule_state"], "failed")
            self.assertIn("approved", row["schedule_note"])
            # no silent retry, even after the story is approved: a failed schedule stays failed until a person acts
            self.approve(self.a)
            now.advance(1)
            self.assertEqual(clock.tick(self.store, room, now=now, sleep=_no_sleep, attempts=2), [])
            self.assertEqual(self.status(self.a), "ready")

    def test_gate_off_scheduled_take_still_fires(self):
        clock.set_schedule(self.store, self.a, T0)
        events = clock.tick(self.store, FakeRoom([obs("idle")] * 5), now=Now(), sleep=_no_sleep, attempts=2)
        self.assertEqual([(e.item_id, e.outcome) for e in events], [(self.a, "fired")])

    def test_the_operator_path_is_not_blocked_by_the_gate_when_off(self):
        # go, hold, release, skip, kill all work with the gate off and an unapproved story
        self.ok(f"/api/shows/{self.show}/hold", {})
        self.ok(f"/api/shows/{self.show}/release", {})
        self.ok(f"/api/items/{self.b}/skip")
        self.ok(f"/api/shows/{self.show}/go")
        c = self.new_item(self.show, "C")
        self.ok(f"/api/items/{c}/kill")

    def test_skip_kill_hold_release_work_with_the_gate_on(self):
        c = self.new_item(self.show, "C")
        with self.gate():
            self.ok(f"/api/shows/{self.show}/hold", {})
            self.ok(f"/api/shows/{self.show}/release", {})
            self.ok(f"/api/items/{c}/kill")
            self.ok(f"/api/items/{self.b}/skip")

    def test_direct_advance_raises_story_not_approved_with_the_item(self):
        with self.gate():
            with self.assertRaises(StoryNotApproved) as cm:
                advance(self.store, self.show)
            self.assertEqual(cm.exception.item_id, self.a)


ALL = (Status, Gate)
build(globals(), *ALL)
del Behaviour, Status, Gate

if __name__ == "__main__":
    unittest.main()
