"""POST /api/items/{id}/schedule and the schedule fields in /rundown."""
import unittest

from tests.test_operator_api import Api
from tests.test_take import _show
from volt import serve


class ScheduleRoute(Api):
    def setUp(self):
        super().setUp()
        self.show, (self.a, self.b) = _show(self.store, [[], []])

    def sched(self, item, body):
        return self.call("POST", f"/api/items/{item}/schedule", body)

    def items(self):
        _, state, _ = self.call("GET", f"/api/state?show={self.show}")
        return {i["id"]: i for i in state["rundown"]["items"]}

    def test_an_unscheduled_item_says_so(self):
        it = self.items()[self.a]
        self.assertEqual((it["scheduledAt"], it["scheduleState"], it["scheduleNote"]), (None, None, None))

    def test_set_normalise_and_clear(self):
        status, out, _ = self.sched(self.b, {"at": "2026-11-01T22:00:00+02:00"})
        self.assertEqual(status, 200)
        self.assertEqual(out["rundown"]["items"][1]["scheduledAt"], "2026-11-01T20:00:00Z")
        self.assertEqual(self.items()[self.b]["scheduledAt"], "2026-11-01T20:00:00Z")
        status, out, _ = self.sched(self.b, {"at": None})
        self.assertEqual((status, out["rundown"]["items"][1]["scheduledAt"]), (200, None))

    def test_setting_again_resets_an_earlier_outcome(self):
        self.sched(self.b, {"at": "2026-11-01T20:00:00Z"})
        self.store.conn.execute("UPDATE item SET schedule_state = 'failed', schedule_note = 'a cue failed' WHERE id = ?", (self.b,))
        self.store.conn.commit()
        it = self.items()[self.b]
        self.assertEqual((it["scheduleState"], it["scheduleNote"]), ("failed", "a cue failed"))
        self.sched(self.b, {"at": "2026-11-01T21:00:00Z"})
        it = self.items()[self.b]
        self.assertEqual((it["scheduleState"], it["scheduleNote"]), (None, None))

    def test_bad_bodies_are_refused_with_a_code(self):
        for body in ({}, {"at": "tomorrow"}, {"at": "2026-11-01T20:00:00"}, {"at": 5}, {"at": True}, {"at": ""}, {"at": "2026-11-01T20:00:00Z", "x": 1}):
            status, out, _ = self.sched(self.b, body)
            self.assertEqual((status, out["error"]["code"]), (422, "bad_input"), body)

    def test_an_item_that_has_played_or_cannot_play_is_refused(self):
        for st in ("on air", "taking", "aired", "skipped", "killed"):
            self.store.conn.execute("UPDATE item SET status = ? WHERE id = ?", (st, self.a))
            self.store.conn.commit()
            status, out, _ = self.sched(self.a, {"at": "2026-11-01T20:00:00Z"})
            self.assertEqual((status, out["error"]["code"]), (409, "not_schedulable"), st)
            self.assertIn(st, out["error"]["reason"])
        self.assertIsNone(self.items()[self.a]["scheduledAt"])

    def test_unknown_item_is_404(self):
        status, out, _ = self.sched("nope", {"at": None})
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))

    def test_audited_and_classified(self):
        self.sched(self.b, {"at": "2026-11-01T20:00:00Z"})
        rows = self.store.list_audit(self.b)
        self.assertEqual([(r["action"], r["target_kind"]) for r in rows], [("schedule", "item")])
        self.assertEqual(serve.ROUTE_ROLES["act_item_schedule"], "operator")


if __name__ == "__main__":
    unittest.main()
