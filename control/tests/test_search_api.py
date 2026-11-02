"""GET /api/search, saved searches and bins: the routes, ownership, limits, and what a rewrite or a purge does to a bin."""
import json
import unittest
from urllib.parse import quote

from tests.pgutil import PgCase
from tests.test_operator_api import Api
from volt import collections, library, retention, searchindex, serve, users
from volt.collections import CollectionError
from volt.store import Store


def words(text):
    return [(w, f"00:00:{i:02d}:00", f"00:00:{i:02d}:12") for i, w in enumerate(text.split())]


class SearchApi(Api):
    def setUp(self):
        super().setUp()
        _u, self.op = users.add(self.store, "olive", "operator")
        _u, self.op2 = users.add(self.store, "omar", "operator")
        _u, self.admin = users.add(self.store, "root", "admin")
        self.a = self.make("we will steal the show", "news")
        self.b = self.make("the budget vote tonight", "sport")

    def make(self, text, show):
        asset = self.store.mint_asset(owner="desk", show_name=show)
        library.index_words(self.store, asset.id, words(text))
        return asset.id

    def get(self, path, token=None):
        s, p, _ = self.call("GET", path, token=token or self.op)
        return s, p

    def err(self, status, code, s, p):
        self.assertEqual((s, p["error"]["code"]), (status, code), p)

    def test_search_route_shape_and_unknown_params_refused(self):
        s, p = self.get("/api/search?q=steal&facets=show")
        self.assertEqual(s, 200)
        self.assertTrue(p["ok"])
        self.assertEqual([x["assetId"] for x in p["assets"]], [self.a])
        self.assertEqual(p["assets"][0]["hits"][0]["kind"], "transcript")
        self.assertEqual(set(p), {"ok", "assets", "total", "facets", "next", "degraded"})
        self.assertEqual(p["facets"]["show"]["values"], [{"value": "news", "count": 1}])
        s, p, _ = self.call("GET", "/api/search?q=steal")
        self.assertEqual(s, 401)                                                      # users exist, so a token is needed
        for bad in ("/api/search?bogus=1", "/api/search?q=a&q=b", "/api/search?sort=zigzag", "/api/search?limit=x",
                    "/api/search?after=junk", "/api/search?filter=notjson"):
            s, p = self.get(bad)
            self.assertEqual(s, 422, bad)
            self.assertEqual(p["error"]["code"], "bad_input", bad)
        s, p = self.get("/api/search?q=%22%20OR%20%29%20%2A%20NEAR%28")             # hostile text is inert: words, or a refusal
        self.assertIn(s, (200, 422))
        s, p = self.get("/api/search?group=hit&q=steal")
        self.assertEqual((s, p["hits"][0]["assetId"]), (200, self.a))

    def test_limit_over_100_refused(self):
        self.err(422, "bad_input", *self.get("/api/search?limit=101"))
        self.err(422, "bad_input", *self.get("/api/search?limit=0"))
        self.assertEqual(self.get("/api/search?limit=100")[0], 200)

    def test_filter_param_is_json_and_capped_at_4kb(self):
        flt = quote(json.dumps({"all": [{"field": "show", "op": "eq", "value": "sport"}]}))
        s, p = self.get(f"/api/search?filter={flt}")
        self.assertEqual((s, [x["assetId"] for x in p["assets"]]), (200, [self.b]))
        big = quote(json.dumps({"all": [{"field": "show", "op": "eq", "value": "x" * 150}] * 30}))
        self.assertGreater(len(json.dumps({"all": [{"field": "show", "op": "eq", "value": "x" * 150}] * 30})), 4096)
        self.err(422, "bad_input", *self.get(f"/api/search?filter={big}"))

    def test_saved_search_owner_and_shared(self):
        body = {"name": "Steal stories", "query": {"q": "steal", "facets": "show"}, "shared": False}
        s, p = self.post("/api/searches", body, token=self.op)
        self.assertEqual((s, p["search"]["owner"], p["search"]["shared"]), (200, "olive", False))
        mine, shared = p["search"]["id"], None
        s, p = self.post("/api/searches", {"name": "Shared one", "query": {"q": "budget"}, "shared": True}, token=self.op)
        shared = p["search"]["id"]
        names = lambda token: sorted(x["name"] for x in self.get("/api/searches", token)[1]["searches"])  # noqa: E731
        self.assertEqual(names(self.op), ["Shared one", "Steal stories"])
        self.assertEqual(names(self.op2), ["Shared one"])                              # an unshared one is private
        self.assertEqual(names(self.admin), ["Shared one"])                             # the list is "yours and shared", admin or not
        s, p = self.post(f"/api/searches/{shared}/delete", {}, token=self.op2)
        self.err(403, "forbidden_role", s, p)
        s, p = self.post(f"/api/searches/{mine}/delete", {}, token=self.op2)
        self.err(404, "not_found", s, p)                                               # not even visible to another operator
        self.assertEqual(self.post(f"/api/searches/{shared}/delete", {}, token=self.admin)[0], 200)   # an admin may
        self.assertEqual(self.post(f"/api/searches/{mine}/delete", {}, token=self.op)[0], 200)
        self.assertEqual(names(self.op), [])
        self.err(404, "not_found", *self.post(f"/api/searches/{mine}/delete", {}, token=self.op))

    def test_saved_search_body_is_validated(self):
        for body in ({"name": "", "query": {"q": "x"}}, {"name": "x" * 101, "query": {"q": "x"}}, {"name": "ok"},
                     {"name": "ok", "query": "q=steal"}, {"name": "ok", "query": {"q": "x", "bogus": 1}},
                     {"name": "ok", "query": {"sort": "relevance"}}, {"name": "ok", "query": {"after": "abc"}},
                     {"name": "ok", "query": {"q": "x"}, "shared": "yes"}, {"name": "ok", "query": {"q": "x"}, "extra": 1}):
            s, p = self.post("/api/searches", body, token=self.op)
            self.assertEqual((s, p["error"]["code"]), (422, "bad_input"), body)

    def test_saved_search_runs_through_search_not_around_it(self):
        flt = {"all": [{"field": "show", "op": "eq", "value": "news"}]}
        s, p = self.post("/api/searches", {"name": "n", "query": {"q": "steal", "filter": flt}, "shared": True}, token=self.op)
        saved = self.get("/api/searches", self.op2)[1]["searches"][0]["query"]
        self.assertEqual(saved, {"q": "steal", "filter": flt})
        params = "&".join(f"{k}={quote(v if isinstance(v, str) else json.dumps(v))}" for k, v in saved.items())
        s, ran = self.get(f"/api/search?{params}", self.op2)
        s2, direct = self.get("/api/search?q=steal&filter=" + quote(json.dumps(flt)), self.op2)
        self.assertEqual((s, s2, ran), (200, 200, direct))
        # the saved text is data: a person clause in a saved search still writes the audit row when someone runs it
        person = {"all": [{"field": "label.person", "op": "eq", "value": "p9"}]}
        self.post("/api/searches", {"name": "who", "query": {"filter": person}, "shared": True}, token=self.op)
        self.get("/api/search?filter=" + quote(json.dumps(person)), self.op2)
        rows = self.store.conn.execute("SELECT actor, target_kind, target_id, detail FROM audit WHERE action = 'search_person'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("omar", "person", "p9", None)])

    def test_person_search_writes_audit_without_name(self):
        searchindex.put_doc(self.store, self.a, "face", "l1", "Amy Adeyemi", status="confirmed", person_id="p1", confidence=0.9)
        self.store.conn.commit()
        flt = quote(json.dumps({"all": [{"field": "label.person", "op": "eq", "value": "p1"}]}))
        s, p = self.get(f"/api/search?filter={flt}")
        self.assertEqual((s, [x["assetId"] for x in p["assets"]]), (200, [self.a]))
        s, p = self.get("/api/audit?target=p1")
        self.assertEqual([(r["actor"], r["action"], r["target_kind"], r["detail"]) for r in p["rows"]],
                         [("olive", "search_person", "person", None)])
        self.assertNotIn("Amy", json.dumps(p))
        self.get("/api/search?q=steal")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'search_person'").fetchone()[0], 1)

    def test_bin_routes_roundtrip_and_ownership(self):
        s, p = self.post("/api/bins", {"name": "Tomorrow", "shared": True}, token=self.op)
        self.assertEqual((s, p["bin"]["owner"], p["bin"]["shared"], p["bin"]["count"]), (200, "olive", True, 0))
        bin_id = p["bin"]["id"]
        s, p = self.post("/api/bins", {"name": "Private"}, token=self.op)
        private = p["bin"]["id"]
        span = self.store.add_span(self.a, "clip", "00:00:01:00", "00:00:03:00", label="best", frame_in=25, frame_out=75)
        s, p = self.post(f"/api/bins/{bin_id}/add", {"assetId": self.a, "spanId": span}, token=self.op)
        self.assertEqual((s, p["member"]["assetId"], p["member"]["spanId"], p["added"]), (200, self.a, span, True))
        s, p = self.post(f"/api/bins/{bin_id}/add", {"assetId": self.a, "spanId": span}, token=self.op)
        self.assertEqual((s, p["added"]), (200, False))                                # idempotent
        self.post(f"/api/bins/{bin_id}/add", {"assetId": self.b}, token=self.op)
        s, p = self.get(f"/api/bins/{bin_id}", self.op2)                               # shared: another operator reads it
        self.assertEqual([(m["assetId"], m["spanId"], m["gone"]) for m in p["members"]], [(self.a, span, False), (self.b, None, False)])
        self.assertEqual(p["members"][0]["label"], "best")
        self.err(403, "forbidden_role", *self.post(f"/api/bins/{bin_id}/add", {"assetId": self.b}, token=self.op2))
        self.err(403, "forbidden_role", *self.post(f"/api/bins/{bin_id}/remove", {"assetId": self.b}, token=self.op2))
        self.err(403, "forbidden_role", *self.post(f"/api/bins/{bin_id}/delete", {}, token=self.op2))
        self.err(404, "not_found", *self.get(f"/api/bins/{private}", self.op2))        # an unshared bin is invisible
        self.assertEqual([b["name"] for b in self.get("/api/bins", self.op2)[1]["bins"]], ["Tomorrow"])
        self.assertEqual(sorted(b["name"] for b in self.get("/api/bins", self.op)[1]["bins"]), ["Private", "Tomorrow"])
        s, p = self.post(f"/api/bins/{bin_id}/remove", {"assetId": self.b}, token=self.op)
        self.assertEqual((s, p["removed"]), (200, True))
        s, p = self.post(f"/api/bins/{bin_id}/remove", {"assetId": self.b}, token=self.op)
        self.assertEqual((s, p["removed"]), (200, False))
        self.assertEqual(self.get(f"/api/bins/{bin_id}")[1]["bin"]["count"], 1)
        self.assertEqual(self.post(f"/api/bins/{bin_id}/delete", {}, token=self.admin)[0], 200)   # an admin may delete anyone's
        self.err(404, "not_found", *self.get(f"/api/bins/{bin_id}"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM bin_member").fetchone()[0], 0)
        # removing from a bin never touches the asset
        self.assertEqual(self.store.get_asset(self.a).id, self.a)

    def test_bin_bad_bodies(self):
        bin_id = self.post("/api/bins", {"name": "b"}, token=self.op)[1]["bin"]["id"]
        for path, body in (("/api/bins", {"name": ""}), ("/api/bins", {"name": "x" * 101}), ("/api/bins", {"name": "b", "shared": 1}),
                           ("/api/bins", {"name": "b", "extra": 1}), (f"/api/bins/{bin_id}/add", {}),
                           (f"/api/bins/{bin_id}/add", {"assetId": self.a, "spanId": 5}),
                           (f"/api/bins/{bin_id}/add", {"assetId": self.a, "extra": 1}), (f"/api/bins/{bin_id}/remove", {}),
                           (f"/api/bins/{bin_id}/delete", {"extra": 1})):
            s, p = self.post(path, body, token=self.op)
            self.assertEqual((s, p["error"]["code"]), (422, "bad_input"), (path, body))
        self.err(404, "not_found", *self.post(f"/api/bins/{bin_id}/add", {"assetId": "nope"}, token=self.op))
        self.err(404, "not_found", *self.post("/api/bins/nope/add", {"assetId": self.a}, token=self.op))
        self.err(422, "bad_input", *self.post(f"/api/bins/{bin_id}/add", {"assetId": self.a, "spanId": "nope"}, token=self.op))
        other = self.store.add_span(self.b, "clip", "00:00:01:00", "00:00:03:00", frame_in=25, frame_out=75)
        self.err(422, "bad_input", *self.post(f"/api/bins/{bin_id}/add", {"assetId": self.a, "spanId": other}, token=self.op))

    def test_roles_in_route_roles(self):
        for name in ("act_search_save", "act_search_delete", "act_bin_create", "act_bin_add", "act_bin_remove", "act_bin_delete"):
            self.assertEqual(serve.ROUTE_ROLES[name], "operator", name)

    def test_audit_rows(self):
        sid = self.post("/api/searches", {"name": "Okonkwo story", "query": {"q": "steal"}}, token=self.op)[1]["search"]["id"]
        bid = self.post("/api/bins", {"name": "Amy Adeyemi"}, token=self.op)[1]["bin"]["id"]
        self.post(f"/api/bins/{bid}/add", {"assetId": self.a}, token=self.op)
        self.post(f"/api/bins/{bid}/remove", {"assetId": self.a}, token=self.op)
        self.post(f"/api/bins/{bid}/delete", {}, token=self.op)
        self.post(f"/api/searches/{sid}/delete", {}, token=self.op)
        got = lambda target: [(r["action"], r["target_kind"], r["actor"]) for r in reversed(self.store.list_audit(target))]  # noqa: E731
        self.assertEqual(got(sid), [("search_save", "search", "olive"), ("search_delete", "search", "olive")])
        self.assertEqual(got(bid), [("bin_create", "bin", "olive"), ("bin_add", "bin", "olive"),
                                    ("bin_remove", "bin", "olive"), ("bin_delete", "bin", "olive")])
        everything = json.dumps([tuple(r) for r in self.store.conn.execute("SELECT * FROM audit")])
        self.assertNotIn("Okonkwo", everything)                                         # names never go into the insert-only audit
        self.assertNotIn("Amy", everything)


class CollectionCases:
    """collections.py on a store; a subclass supplies self.store."""

    def asset(self, text="alpha beta", show="news"):
        a = self.store.mint_asset(owner="desk", show_name=show)
        library.index_words(self.store, a.id, words(text))
        return a.id

    def code(self, fn, *a, **kw):
        with self.assertRaises(CollectionError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def test_bin_add_idempotent(self):
        b = collections.create_bin(self.store, "olive", "Tomorrow", False)
        a = self.asset()
        first = collections.add_member(self.store, b["id"], a, None, "olive")
        again = collections.add_member(self.store, b["id"], a, None, "olive")
        self.assertEqual((first["added"], again["added"]), (True, False))
        span = self.store.conn.execute("SELECT id FROM span WHERE asset_id = ? LIMIT 1", (a,)).fetchone()["id"]
        self.assertTrue(collections.add_member(self.store, b["id"], a, span, "olive")["added"])    # whole asset and a clip are different
        self.assertFalse(collections.add_member(self.store, b["id"], a, span, "olive")["added"])
        self.assertEqual(len(collections.members(self.store, b["id"])), 2)
        self.assertEqual([m["position"] for m in collections.members(self.store, b["id"])], [1, 2])

    def test_bin_survives_span_rewrite(self):
        b = collections.create_bin(self.store, "olive", "Clips", False)
        a = self.asset("one two three")
        span = self.store.add_span(a, "clip", "00:00:00:00", "00:00:02:00", label="c", frame_in=0, frame_out=50)
        collections.add_member(self.store, b["id"], a, span, "olive")
        self.assertFalse(collections.members(self.store, b["id"])[0]["gone"])
        # a transcript or detection rewrite deletes spans; a bin must neither block it nor raise afterwards
        self.store.conn.execute("DELETE FROM span WHERE id = ?", (span,))
        self.store.conn.commit()
        got = collections.members(self.store, b["id"])
        self.assertEqual([(m["assetId"], m["spanId"], m["gone"]) for m in got], [(a, span, True)])
        self.assertIsNone(got[0]["tcIn"])
        self.assertEqual(collections.get_bin(self.store, b["id"], "olive", False)["count"], 1)

    def test_purge_removes_bin_members(self):
        b = collections.create_bin(self.store, "olive", "Clips", False)
        a = self.asset()
        keep = self.asset("keep")
        collections.add_member(self.store, b["id"], a, None, "olive")
        collections.add_member(self.store, b["id"], keep, None, "olive")
        retention.tombstone(self.store, a)
        self.assertTrue(collections.members(self.store, b["id"])[0]["assetDeleted"])
        res = retention.purge_asset(self.store, a, retention.OwnedRoots())
        self.assertEqual(res.status, "purged", res)
        self.assertEqual([m["assetId"] for m in collections.members(self.store, b["id"])], [keep])

    def test_limits(self):
        for i in range(collections.MAX_SEARCHES):
            collections.save_search(self.store, "olive", f"s{i}", {"q": "x"}, False)
        self.assertEqual(self.code(collections.save_search, self.store, "olive", "one more", {"q": "x"}, False), "bad_input")
        collections.save_search(self.store, "omar", "other owner", {"q": "x"}, False)          # the cap is per owner
        for i in range(collections.MAX_BINS):
            collections.create_bin(self.store, "olive", f"b{i}", False)
        self.assertEqual(self.code(collections.create_bin, self.store, "olive", "one more", False), "bad_input")
        b = collections.create_bin(self.store, "omar", "full", False)
        assets = [self.asset(f"w{i}") for i in range(3)]
        now = "2026-11-02T00:00:00Z"
        rows = [(b["id"], assets[0], None, n, "omar", now) for n in range(1, collections.MAX_MEMBERS)]
        # 4,999 members for one asset is not a real shape, but the cap counts rows, not distinct assets
        for i in range(0, len(rows), 500):               # multi-row inserts: the pg adapter has no executemany
            chunk = rows[i:i + 500]
            self.store.conn.execute(
                "INSERT INTO bin_member (bin_id, asset_id, span_id, position, added_by, added_at) VALUES "
                + ", ".join(["(?, ?, ?, ?, ?, ?)"] * len(chunk)),
                tuple(v for r in chunk for v in (r[0], r[1], f"gone{r[3]}", r[3], r[4], r[5])))
        self.store.conn.commit()
        self.assertTrue(collections.add_member(self.store, b["id"], assets[1], None, "omar")["added"])      # the 5,000th
        self.assertEqual(self.code(collections.add_member, self.store, b["id"], assets[2], None, "omar"), "bin_full")
        self.assertFalse(collections.add_member(self.store, b["id"], assets[1], None, "omar")["added"])     # a repeat is not "full"

    def test_ownership_rules(self):
        b = collections.create_bin(self.store, "olive", "Mine", False)
        a = self.asset()
        self.assertEqual(self.code(collections.add_member, self.store, b["id"], a, None, "omar"), "not_found")       # invisible
        shared = collections.create_bin(self.store, "olive", "Shared", True)
        self.assertEqual(self.code(collections.add_member, self.store, shared["id"], a, None, "omar"), "forbidden_role")
        self.assertTrue(collections.add_member(self.store, shared["id"], a, None, "omar", is_admin=True)["added"])
        s = collections.save_search(self.store, "olive", "Q", {"q": "x"}, True)
        self.assertEqual(self.code(collections.delete_search, self.store, s["id"], "omar", False), "forbidden_role")
        collections.delete_search(self.store, s["id"], "omar", True)
        self.assertEqual(self.code(collections.delete_search, self.store, s["id"], "omar", True), "not_found")

    def test_tombstoned_asset_cannot_be_added(self):
        b = collections.create_bin(self.store, "olive", "Mine", False)
        a = self.asset()
        retention.tombstone(self.store, a)
        self.assertEqual(self.code(collections.add_member, self.store, b["id"], a, None, "olive"), "not_found")


class SqliteCollections(CollectionCases, unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)


class PostgresCollections(CollectionCases, PgCase):
    def setUp(self):
        super().setUp()
        self.store = PgCase.store(self)


if __name__ == "__main__":
    unittest.main()
