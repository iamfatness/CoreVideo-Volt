"""Append-only operator audit: store, actor on POST, GET last 200, no row on 401/404/409."""

import unittest

from tests.test_operator_api import TOKEN, Api
from tests.test_playout import _show
from tests.test_rooms import obs
from tests.test_take import _show as _cue_show
from volt import serve
from volt.rooms import FakeRoom
from volt.store import Store


class StoreAudit(unittest.TestCase):
    def test_append_audit_returns_id_and_list_audit_is_newest_first(self):
        store = Store()
        first = store.append_audit("desk", "go", "show", "s1")
        second = store.append_audit("alice", "take", "show", "s1", detail="ok")
        store.append_audit("desk", "go", "show", "other")
        rows = store.list_audit("s1")
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(rows[0]["actor"], "alice")
        self.assertEqual(rows[0]["action"], "take")
        self.assertEqual(rows[0]["target_kind"], "show")
        self.assertEqual(rows[0]["target_id"], "s1")
        self.assertEqual(rows[0]["detail"], "ok")
        self.assertTrue(rows[0]["at"])

    def test_list_audit_caps_at_200(self):
        store = Store()
        for n in range(201):
            store.append_audit("desk", "go", "show", "s1", detail=str(n))
        rows = store.list_audit("s1")
        self.assertEqual(len(rows), 200)
        self.assertEqual(rows[0]["detail"], "200")
        self.assertEqual(rows[-1]["detail"], "1")


class HttpAudit(Api):
    def _actions(self, target_id):
        return [r["action"] for r in self.store.list_audit(target_id)]

    def test_go_writes_audit_for_desk_by_default(self):
        show, ids = _show(self.store)
        s, p = self.post(f"/api/shows/{show}/go")
        self.assertEqual(s, 200)
        self.assertTrue(p["ok"])
        rows = self.store.list_audit(show)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "desk")
        self.assertEqual(rows[0]["action"], "go")
        self.assertEqual(rows[0]["target_kind"], "show")
        self.assertEqual(rows[0]["target_id"], show)
        status, body, _ = self.call("GET", f"/api/audit?target={show}")
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["action"], "go")

    def test_actor_header_wins_over_owner(self):
        show, (a,) = _cue_show(self.store, [[]])
        serve.ROOM = FakeRoom([obs("idle")])
        s, _ = self.post(f"/api/shows/{show}/take", {"owner": "from-body"},
                         headers={"X-Volt-Actor": "header.user"})
        self.assertEqual(s, 200)
        self.assertEqual(self.store.list_audit(show)[0]["actor"], "header.user")

    def test_owner_body_is_actor_when_header_absent(self):
        show, (a,) = _cue_show(self.store, [[]])
        serve.ROOM = FakeRoom([obs("idle")])
        s, _ = self.post(f"/api/shows/{show}/take", {"owner": "cam.op-1"})
        self.assertEqual(s, 200)
        self.assertEqual(self.store.list_audit(show)[0]["actor"], "cam.op-1")

    def test_invalid_actor_is_422_before_the_action(self):
        show, ids = _show(self.store)
        before = self.statuses(show)
        for actor in ("", "bad actor", "a@b", "x" * 101, "slash/nope"):
            p = self.refused(422, "bad_actor", f"/api/shows/{show}/go",
                             headers={"X-Volt-Actor": actor})
            self.assertTrue(p["error"]["reason"])
            self.assertEqual(self.statuses(show), before)
            self.assertEqual(self._actions(show), [])
        p = self.refused(422, "bad_actor", f"/api/shows/{show}/take", {"owner": "not ok"})
        self.assertEqual(self.statuses(show), before)

    def test_401_writes_no_audit(self):
        serve.TOKEN = TOKEN
        show, _ = _show(self.store)
        s, p, _ = self.call("POST", f"/api/shows/{show}/hold", {})
        self.assertEqual((s, p["error"]["code"]), (401, "unauthorized"))
        self.assertEqual(self.store.list_audit(show), [])

    def test_404_writes_no_audit(self):
        self.refused(404, "not_found", "/api/shows/nope/go")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_409_held_writes_no_audit(self):
        show, ids = _show(self.store)
        from volt.playout import hold
        from tests.test_playout import Clock
        hold(self.store, show, "tech", Clock())
        self.refused(409, "held", f"/api/shows/{show}/go")
        self.assertEqual(self._actions(show), [])

    def test_409_cue_failed_writes_no_audit(self):
        show, (a, b) = _cue_show(self.store, [[], [("overlay.take", "on"), ("stream.start", "")]])
        serve.ROOM = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        self.assertEqual(self.post(f"/api/shows/{show}/take")[0], 200)
        self.assertEqual(self._actions(show), ["take"])
        self.refused(409, "cue_failed", f"/api/shows/{show}/take")
        self.assertEqual(self._actions(show), ["take"])

    def test_get_audit_is_token_gated(self):
        show, _ = _show(self.store)
        self.post(f"/api/shows/{show}/hold", {"reason": "tech"})
        serve.TOKEN = TOKEN
        self.assertEqual(self.call("GET", f"/api/audit?target={show}")[0], 401)
        s, body, _ = self.call("GET", f"/api/audit?target={show}", token=TOKEN)
        self.assertEqual(s, 200)
        self.assertEqual(body["rows"][0]["action"], "hold")


if __name__ == "__main__":
    unittest.main()
