"""Floated stories, the reading pace and read-time length (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md)."""

import json
import unittest
from xml.etree import ElementTree

from tests.storykit import Behaviour, build
from tests.test_clock import Now, T0
from tests.test_operator_ui import NODE, _node
from tests.test_rooms import _no_sleep, obs
from volt import clock, cues, mos, serve, story
from volt.playout import PlayoutError, advance
from volt.rooms import FakeRoom


class Float(Behaviour):
    def setUp(self):
        super().setUp()
        self.show = self.new_show()
        self.a, self.b, self.c = (self.new_item(self.show, n) for n in ("A", "B", "C"))
        self.with_media(self.a, self.b, self.c)

    def float(self, item, floated=True, **kw):
        return self.post(f"/api/items/{item}/float", {"floated": floated}, **kw)

    def test_go_passes_over_floated_without_marking_skipped(self):
        self.ok(f"/api/items/{self.b}/float", {"floated": True})
        self.assertEqual(self.ok(f"/api/shows/{self.show}/go")["target"], self.a)
        p = self.ok(f"/api/shows/{self.show}/go")
        self.assertEqual((p["target"], p["passed"]), (self.c, []))
        self.assertEqual(self.status(self.b), "ready")                # not skipped, not killed: still a backup
        self.assertEqual(self.status(self.a), "aired")
        self.assertIsNone(self.row(self.b)["as_run_in"])

    def test_a_show_with_only_floated_left_is_over(self):
        self.ok(f"/api/items/{self.b}/float", {"floated": True})
        self.ok(f"/api/items/{self.c}/float", {"floated": True})
        self.ok(f"/api/shows/{self.show}/go")
        p = self.ok(f"/api/shows/{self.show}/go")
        self.assertIsNone(p["target"])
        self.assertEqual((self.status(self.b), self.status(self.c)), ("ready", "ready"))

    def test_unfloat_restores_position(self):
        before = [r["position"] for r in self.store.conn.execute("SELECT position FROM item ORDER BY position")]
        self.ok(f"/api/items/{self.b}/float", {"floated": True})
        self.ok(f"/api/items/{self.b}/float", {"floated": False})
        self.assertEqual([r["position"] for r in self.store.conn.execute("SELECT position FROM item ORDER BY position")], before)
        order = [self.ok(f"/api/shows/{self.show}/go")["target"] for _ in range(3)]
        self.assertEqual(order, [self.a, self.b, self.c])

    def test_float_is_idempotent_and_recorded(self):
        op = self.user("dana")
        self.ok(f"/api/items/{self.b}/float", {"floated": True}, token=op)
        self.ok(f"/api/items/{self.b}/float", {"floated": True}, token=op)      # no second event
        self.ok(f"/api/items/{self.b}/float", {"floated": False}, token=op)
        ev = self.events(self.b)
        self.assertEqual([(e["kind"], e["to_value"], e["actor"]) for e in ev], [("float", "floated", "dana"), ("float", "unfloated", "dana")])
        s, g = self.get(f"/api/items/{self.b}/story", token=op)
        self.assertIs(g["floated"], False)
        self.assertEqual(self.store.list_audit(self.b)[0]["action"], "float")

    def test_float_refused_for_on_air_taking_aired_skipped_killed(self):
        self.ok(f"/api/shows/{self.show}/go")
        self.ok(f"/api/shows/{self.show}/go")                 # a aired, b on air
        d, e = self.new_item(self.show, "D"), self.new_item(self.show, "E")
        self.ok(f"/api/items/{d}/skip")
        self.ok(f"/api/items/{e}/kill")
        t = self.new_item(self.show, "T")
        self.store.conn.execute("UPDATE item SET status = 'taking' WHERE id = ?", (t,))
        self.store.conn.commit()
        for item in (self.a, self.b, d, e, t):
            with self.subTest(status=self.status(item)):
                self.refused(409, "not_floatable", f"/api/items/{item}/float", {"floated": True})
                self.assertFalse(self.row(item)["floated"])

    def test_unfloat_of_a_floated_item_that_has_since_changed_is_still_allowed(self):
        self.ok(f"/api/items/{self.c}/float", {"floated": True})
        self.store.conn.execute("UPDATE item SET status = 'killed' WHERE id = ?", (self.c,))
        self.store.conn.commit()
        self.ok(f"/api/items/{self.c}/float", {"floated": False})

    def test_bad_bodies(self):
        for body in ({}, {"floated": "yes"}, {"floated": True, "x": 1}):
            self.refused(422, "bad_input", f"/api/items/{self.a}/float", body)
        self.refused(404, "not_found", "/api/items/nope/float", {"floated": True})
        self.assertEqual(serve.ROUTE_ROLES["act_item_float"], "operator")

    def test_a_floated_item_is_never_taken_by_a_direct_arm(self):
        self.ok(f"/api/items/{self.a}/float", {"floated": True})
        with self.assertRaises(PlayoutError):
            cues.arm(self.store, self.a)
        self.assertEqual(self.status(self.a), "ready")

    def test_the_clock_does_not_fire_a_floated_item(self):
        self.ok(f"/api/items/{self.a}/float", {"floated": True})
        clock.set_schedule(self.store, self.a, T0)
        events = clock.tick(self.store, FakeRoom([obs("idle")] * 4), now=Now(), sleep=_no_sleep, attempts=2)
        self.assertNotIn("fired", [e.outcome for e in events])
        self.assertEqual(self.status(self.a), "ready")

    def test_direct_advance_agrees(self):
        self.ok(f"/api/items/{self.a}/float", {"floated": True})
        self.assertEqual(advance(self.store, self.show).target, self.b)

    def test_operator_path_unblocked_with_floated_items(self):
        self.ok(f"/api/items/{self.b}/float", {"floated": True})
        self.ok(f"/api/shows/{self.show}/hold", {})
        self.ok(f"/api/shows/{self.show}/release", {})
        self.ok(f"/api/items/{self.c}/kill")
        self.ok(f"/api/items/{self.b}/skip")


class Pace(Behaviour):
    def setUp(self):
        super().setUp()
        self.show = self.new_show()

    def test_pace_range_422_and_null_clears(self):
        path = f"/api/shows/{self.show}/reading-pace"
        for wpm in (59, 401, 0, -5, 150.5, "160", True):
            with self.subTest(wpm=wpm):
                self.refused(422, "bad_input", path, {"wpm": wpm})
        self.refused(422, "bad_input", path, {})
        self.refused(422, "bad_input", path, {"wpm": 100, "x": 1})
        self.refused(404, "not_found", "/api/shows/nope/reading-pace", {"wpm": 100})
        p = self.ok(path, {"wpm": 200})
        self.assertEqual(p["rundown"]["readWpm"], 200)
        self.assertEqual(self.store.conn.execute("SELECT read_wpm FROM show WHERE id = ?", (self.show,)).fetchone()[0], 200)
        self.ok(path, {"wpm": 60})
        self.ok(path, {"wpm": 400})
        p = self.ok(path, {"wpm": None})
        self.assertEqual(p["rundown"]["readWpm"], story.DEFAULT_WPM)
        self.assertIsNone(self.store.conn.execute("SELECT read_wpm FROM show WHERE id = ?", (self.show,)).fetchone()[0])
        self.assertEqual(serve.ROUTE_ROLES["act_show_pace"], "operator")

    def test_pace_is_audited(self):
        self.ok(f"/api/shows/{self.show}/reading-pace", {"wpm": 180})
        row = self.store.list_audit(self.show)[0]      # newest first; the show's own creation is the older row
        self.assertEqual((row["action"], row["target_kind"]), ("reading_pace", "show"))


class Length(Behaviour):
    """/rundown carries the new item fields and the length source, never the script text."""

    def setUp(self):
        super().setUp()
        self.show = self.new_show()

    def rundown(self):
        s, p = self.get(f"/rundown?show={self.show}")
        self.assertEqual(s, 200)
        return p

    def test_rundown_payload_has_new_fields_and_no_script_text(self):
        secret = "UNIQUEWORDONE two three four five six\n// PRIVATENOTE do not leak\n[SOT 0:10]"
        item = self.new_item(self.show, "Lead", secret)
        p = self.rundown()
        (it,) = p["items"]
        self.assertEqual((it["id"], it["storyStatus"], it["scriptRev"], it["floated"]), (item, "draft", 1, False))
        self.assertEqual((it["words"], it["readSeconds"], it["lengthSource"]), (6, 2, "read"))
        self.assertEqual(p["readWpm"], 160)
        dump = json.dumps(self.rundown()) + json.dumps(self.get(f"/api/state?show={self.show}")[1])
        self.assertNotIn("UNIQUEWORDONE", dump)
        self.assertNotIn("PRIVATENOTE", dump)

    def test_span_less_item_uses_read_time(self):
        self.new_item(self.show, "Anchor", " ".join(["word"] * 80))
        self.new_item(self.show, "Empty")
        a, b = self.rundown()["items"]
        self.assertEqual((a["words"], a["readSeconds"], a["lengthSource"]), (80, 30, "read"))
        self.assertEqual((b["words"], b["readSeconds"], b["lengthSource"]), (0, 0, "none"))
        self.ok(f"/api/shows/{self.show}/reading-pace", {"wpm": 80})
        self.assertEqual(self.rundown()["items"][0]["readSeconds"], 60)

    def test_item_with_media_keeps_media_length(self):
        item = self.new_item(self.show, "Clip", " ".join(["word"] * 80))
        self.with_media(item)
        (it,) = self.rundown()["items"]
        self.assertEqual(it["lengthSource"], "media")

    def test_edits_show_in_the_next_rundown(self):
        item = self.new_item(self.show, "Lead", "one two")
        self.ok(f"/api/items/{item}/script", {"script": "one two three", "baseRev": 1})
        (it,) = self.rundown()["items"]
        self.assertEqual((it["scriptRev"], it["words"]), (2, 3))

    def test_mos_export_omits_floated_and_private_notes(self):
        keep = self.new_item(self.show, "Keep", "Read this.\n// PRIVATE do not export\n[SOT 0:05]\nAnd this.")
        gone = self.new_item(self.show, "Backup", "Floated body.")
        self.ok(f"/api/items/{gone}/float", {"floated": True})
        out = mos.export(self.store, self.show)
        self.assertNotIn("PRIVATE", out["content"])
        self.assertNotIn("Floated body", out["content"])
        self.assertNotIn("Backup", out["content"])
        root = ElementTree.fromstring(out["content"])
        scripts = [e.text for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "script"]
        self.assertEqual(scripts, ["Read this.\n[SOT 0:05]\nAnd this."])
        self.assertEqual(out["stories"], 1)
        self.assertEqual(keep, mos.load_stories(self.store, self.show)[0].id)


ALL = (Float, Pace, Length)
build(globals(), *ALL)


@unittest.skipUnless(NODE, "node is needed to run the page's functions")
class FloatTimingTwin(unittest.TestCase):
    T0 = 1_800_000_000_000

    def iso(self, ms):
        import datetime
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    def items(self, *rows):
        return [{"id": "i%d" % n, "status": st, "floated": fl, "asRunIn": None if at is None else self.iso(self.T0 + at * 1000)}
                for n, (st, fl, at) in enumerate(rows)]

    def test_floated_excluded_from_totals(self):
        items = self.items(("aired", False, 0), ("ready", True, None), ("ready", False, None))
        seconds = {"i0": 60, "i1": 500, "i2": 20}
        (r,) = _node(["frontTimes", items, seconds, self.T0])
        self.assertEqual([x["offset"] for x in r["rows"]], [0, None, 60])
        self.assertEqual(r["totalSec"], 80)
        (b,) = _node(["backTimes", items, seconds, 100])
        self.assertEqual(b["totalSec"], 80)
        self.assertEqual(b["overSec"], -20)
        self.assertIsNone(b["rows"][1]["backMs"])

    def test_a_floated_item_does_not_anchor_the_clock_or_move_drift(self):
        items = self.items(("ready", True, 5), ("on air", False, 70), ("ready", False, None))
        (r,) = _node(["frontTimes", items, {"i0": 40, "i1": 60, "i2": 10}, self.T0])
        self.assertEqual((r["rows"][1]["offset"], r["driftSec"]), (0, 0))

    def test_span_less_item_uses_read_time(self):
        got = _node(["itemPlan", {"lengthSource": "read", "readSeconds": 42}, None],
                    ["itemPlan", {"lengthSource": "read", "readSeconds": 0}, None],
                    ["itemPlan", {"lengthSource": "none", "readSeconds": 0}, None])
        self.assertEqual(got, [{"seconds": 42, "source": "read"}, {"seconds": 0, "source": "read"}, {"seconds": None, "source": "none"}])

    def test_item_with_media_keeps_media_length(self):
        got = _node(["itemPlan", {"lengthSource": "media", "readSeconds": 99}, {"frames": 250, "rate": "25"}],
                    ["itemPlan", {"lengthSource": "media", "readSeconds": 99}, {"frames": 10}],
                    ["itemPlan", {"lengthSource": "media", "readSeconds": 99}, None])
        self.assertEqual(got, [{"seconds": 10, "source": "media"}, {"seconds": None, "source": "media"}, {"seconds": None, "source": "media"}])

    def test_an_older_server_without_the_new_fields_still_gives_media_length(self):
        (got,) = _node(["itemPlan", {}, {"frames": 250, "rate": "25"}])
        self.assertEqual(got, {"seconds": 10, "source": "media"})


del Behaviour, Float, Pace, Length

if __name__ == "__main__":
    unittest.main()
