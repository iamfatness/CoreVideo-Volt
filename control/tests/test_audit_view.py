"""The audit view (identity plan, task 2): filters, cursor paging, CSV that cannot run formulas."""
import csv
import io
import unittest

from tests.test_operator_api import Api
from volt import auditview, users
from volt.store import Store


def fill(store, n=250, at="2026-11-02T10:00:00Z"):
    for i in range(n):
        store.conn.execute(
            "INSERT INTO audit (id, at, actor, action, target_kind, target_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"id{i:04d}", at, "alice" if i % 2 else "bob", "hold" if i % 3 else "take", "show", f"s{i % 5}", None))
    store.conn.commit()


class Paging(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        fill(self.store)

    def test_cursor_pages_without_gaps_or_repeats_even_when_every_time_is_equal(self):
        seen, cursor = [], None
        while True:
            rows, cursor = self.store.list_audit_page(before=cursor, limit=100)
            seen += [r["id"] for r in rows]
            if cursor is None:
                break
        self.assertEqual(len(seen), 250)
        self.assertEqual(len(set(seen)), 250)
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_filters_combine(self):
        rows, _ = self.store.list_audit_page(actor="alice", action="take", limit=500)
        self.assertTrue(rows)
        self.assertTrue(all(r["actor"] == "alice" and r["action"] == "take" for r in rows))
        rows, _ = self.store.list_audit_page(kind="show", target="s1", limit=500)
        self.assertEqual({r["target_id"] for r in rows}, {"s1"})
        rows, _ = self.store.list_audit_page(since="2026-11-03T00:00:00Z", limit=500)
        self.assertEqual(rows, [])
        rows, _ = self.store.list_audit_page(until="2026-11-01T00:00:00Z", limit=500)
        self.assertEqual(rows, [])

    def test_limit_is_enforced_and_a_bad_cursor_is_refused(self):
        rows, nxt = self.store.list_audit_page(limit=10)
        self.assertEqual((len(rows), nxt is not None), (10, True))
        with self.assertRaises(ValueError):
            self.store.list_audit_page(before="not a cursor")
        with self.assertRaises(ValueError):
            self.store.list_audit_page(limit=501)

    def test_the_indexes_exist(self):
        names = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"audit_at", "audit_actor_at", "audit_action_at"} <= names)


class Csv(unittest.TestCase):
    def test_formulas_are_neutralised(self):
        for dangerous in ("=HYPERLINK(\"http://x\")", "+1", "-2", "@SUM(A1)", "\tx", "\rx"):
            cell = auditview.csv_cell(dangerous)
            self.assertTrue(cell.startswith("'"), dangerous)
        self.assertEqual(auditview.csv_cell("plain"), "plain")
        self.assertEqual(auditview.csv_cell(None), "")

    def test_the_file_round_trips_through_a_csv_reader(self):
        rows = [{"id": "1", "at": "2026-11-02T10:00:00Z", "actor": 'a,"b"', "action": "x\ny", "target_kind": "show", "target_id": "s", "detail": "=1+1"}]
        text = auditview.render_csv(rows)
        back = list(csv.reader(io.StringIO(text)))
        self.assertEqual(back[0], ["at", "actor", "action", "target_kind", "target_id", "detail", "id"])
        self.assertEqual(back[1][1], 'a,"b"')
        self.assertEqual(back[1][2], "x\ny")
        self.assertEqual(back[1][5], "'=1+1")


class AuditApi(Api):
    def setUp(self):
        super().setUp()
        _, self.admin = users.add(self.store, "root", "admin")
        _, self.op = users.add(self.store, "alice", "operator")
        fill(self.store, 30)

    def test_target_only_is_unchanged_for_an_operator_and_no_target_needs_admin(self):
        status, out, _ = self.call("GET", "/api/audit?target=s1", token=self.op)
        self.assertEqual(status, 200)
        self.assertTrue(out["rows"])
        status, out, _ = self.call("GET", "/api/audit", token=self.op)
        self.assertEqual((status, out["error"]["code"]), (403, "forbidden_role"))

    def test_admin_filters_pages_and_exports(self):
        status, out, _ = self.call("GET", "/api/audit?actor=alice&limit=5", token=self.admin)
        self.assertEqual((status, len(out["rows"]), bool(out["next"])), (200, 5, True))
        status, out2, _ = self.call("GET", "/api/audit?actor=alice&limit=500&before=" + out["next"].replace(":", "%3A").replace("|", "%7C"), token=self.admin)
        self.assertEqual(status, 200)
        self.assertFalse({r["id"] for r in out["rows"]} & {r["id"] for r in out2["rows"]})
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        conn.request("GET", "/api/audit?format=csv&limit=500", headers={"Authorization": f"Bearer {self.admin}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        self.assertEqual((resp.status, resp.getheader("Content-Type").split(";")[0]), (200, "text/csv"))
        self.assertTrue(body.startswith("at,actor,action"))

    def test_bad_parameters_are_refused(self):
        for q in ("?limit=501", "?limit=0", "?limit=x", "?nope=1", "?before=zzz", "?format=xml"):
            status, out, _ = self.call("GET", "/api/audit" + q, token=self.admin)
            self.assertEqual((status, out["error"]["code"]), (422, "bad_input"), q)


if __name__ == "__main__":
    unittest.main()
