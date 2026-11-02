"""GET /api/schema, the schema-edit POSTs and the per-asset metadata routes."""
import json
import re
import unittest

from tests.test_operator_api import Api
from volt import serve, users


class SchemaApi(Api):
    def setUp(self):
        super().setUp()
        self.asset = self.store.mint_asset(owner="desk", show_name="news").id
        _u, self.op = users.add(self.store, "olive", "operator")
        _u, self.admin = users.add(self.store, "root", "admin")

    def adm(self, path, body=None):
        return self.post(path, body, token=self.admin)

    def field(self, key="guest", type_="text", **extra):
        s, p = self.adm("/api/schema/fields", {"key": key, "label": key.title(), "type": type_, **extra})
        self.assertEqual(s, 200, p)
        return p

    def meta(self, key, values, asset=None, token=None):
        return self.post(f"/api/assets/{asset or self.asset}/metadata", {"key": key, "values": values}, token=token or self.op)

    def test_roles(self):
        expect = {"act_schema_field": "admin", "act_schema_retire": "admin", "act_schema_terms": "operator",
                  "act_schema_term_retire": "admin", "act_asset_metadata": "operator"}
        for name, role in expect.items():
            self.assertEqual(serve.ROUTE_ROLES[name], role, name)
        s, p = self.post("/api/schema/fields", {"key": "x", "label": "X", "type": "text"}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (403, "forbidden_role"))
        self.field("mood", "enum")
        s, p = self.post("/api/schema/fields/mood/retire", {}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (403, "forbidden_role"))
        s, p = self.post("/api/schema/fields/mood/terms/retire", {"term": "x"}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (403, "forbidden_role"))
        s, p = self.post("/api/schema/fields/mood/terms", {"terms": ["calm"]}, token=self.op)
        self.assertEqual((s, p["added"]), (200, ["calm"]))

    def test_unclassified_post_is_admin_only(self):
        def act_probe(store, _unused, body):
            return {"ok": True}

        self.assertNotIn("act_probe", serve.ROUTE_ROLES)
        serve.ROUTES.append((re.compile(r"^/api/probe$()"), act_probe))
        self.addCleanup(serve.ROUTES.pop)
        s, p = self.post("/api/probe", {}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (403, "forbidden_role"))
        self.assertEqual(self.post("/api/probe", {}, token=self.admin)[0], 200)

    def test_get_schema_needs_a_token_and_lists_fields(self):
        self.field("guest")
        self.field("mood", "enum")
        self.post("/api/schema/fields/mood/terms", {"terms": ["calm", "tense"]}, token=self.op)
        s, p, _ = self.call("GET", "/api/schema")
        self.assertEqual(s, 401)
        s, p, _ = self.call("GET", "/api/schema", token=self.op)
        self.assertEqual(s, 200)
        keys = {f["key"]: f for f in p["fields"]}
        self.assertEqual(set(keys), {"guest", "mood"})
        self.assertEqual([t["term"] for t in keys["mood"]["terms"]], ["calm", "tense"])
        s, p, _ = self.call("GET", "/api/schema?show=sport", token=self.op)
        self.assertEqual((s, [f["key"] for f in p["fields"]]), (200, ["guest", "mood"]))
        s, p, _ = self.call("GET", "/api/schema?bogus=1", token=self.op)
        self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))

    def test_create_then_edit_a_field(self):
        out = self.field("guest")
        self.assertTrue(out["created"])
        s, p = self.adm("/api/schema/fields", {"key": "guest", "label": "Guest name", "required": True})
        self.assertEqual((s, p["created"], p["field"]["label"], p["field"]["required"]), (200, False, "Guest name", True))
        s, p = self.adm("/api/schema/fields", {"key": "fresh"})
        self.assertEqual((s, p["error"]["code"]), (422, "bad_input"))

    def test_set_metadata_roundtrip_and_missing(self):
        self.field("slug", required=True)
        self.field("tags", multi=True)
        s, p, _ = self.call("GET", f"/api/assets/{self.asset}/metadata", token=self.op)
        self.assertEqual((s, p["values"], p["missing"]), (200, {}, ["slug"]))
        s, p = self.meta("slug", ["lead"])
        self.assertEqual((s, p["ok"], p["values"], p["missing"]), (200, True, ["lead"], []))
        self.assertEqual(self.meta("tags", ["a", "b"])[0], 200)
        s, p, _ = self.call("GET", f"/api/assets/{self.asset}/metadata", token=self.op)
        self.assertEqual((p["values"], p["missing"]), ({"slug": ["lead"], "tags": ["a", "b"]}, []))
        s, p, _ = self.call("GET", "/api/assets/nope/metadata", token=self.op)
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))

    def test_errors_carry_codes(self):
        self.field("n", "number")
        self.field("mood", "enum")
        self.field("slug", required=True)
        self.meta("slug", ["x"])
        self.adm("/api/schema/fields/mood/terms", {"terms": ["calm"]})
        cases = [("nope", ["x"], 422, "unknown_field"), ("n", ["five"], 422, "type_mismatch"),
                 ("mood", ["blue"], 422, "not_in_vocabulary"), ("slug", [], 422, "required_field")]
        for key, values, status, code in cases:
            s, p = self.meta(key, values)
            self.assertEqual((s, p["error"]["code"]), (status, code), key)
        s, p = self.post("/api/assets/nope/metadata", {"key": "n", "values": [1]}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))
        self.meta("n", [3])
        s, p = self.adm("/api/schema/fields", {"key": "n", "type": "text"})
        self.assertEqual((s, p["error"]["code"]), (409, "type_locked"))

    def test_retire_and_term_retire_unknowns_are_404(self):
        s, p = self.adm("/api/schema/fields/ghost/retire")
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))
        s, p = self.post("/api/schema/fields/ghost/terms", {"terms": ["a"]}, token=self.op)
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))
        s, p = self.adm("/api/schema/fields/ghost/terms/retire", {"term": "a"})
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))
        self.field("mood", "enum")
        s, p = self.adm("/api/schema/fields/mood/terms/retire", {"term": "a"})
        self.assertEqual((s, p["error"]["code"]), (422, "not_in_vocabulary"))
        self.assertEqual(self.adm("/api/schema/fields/mood/retire")[0], 200)
        self.assertEqual(self.call("GET", "/api/schema", token=self.op)[1]["fields"], [])

    def test_audit_row_has_old_and_new(self):
        self.field("guest")
        self.meta("guest", ["Amy"])
        self.meta("guest", ["Bo"])
        rows = self.store.list_audit(self.asset)
        self.assertEqual([(r["action"], r["target_kind"], r["actor"]) for r in rows], [("metadata", "asset", "olive")] * 2)
        newest = json.loads(rows[0]["detail"])
        self.assertEqual(newest, {"key": "guest", "old": ["Amy"], "new": ["Bo"]})
        self.assertEqual(json.loads(rows[1]["detail"]), {"key": "guest", "old": [], "new": ["Amy"]})
        rows = self.store.list_audit("guest")
        self.assertEqual([(r["action"], r["target_kind"]) for r in rows], [("schema_field", "schema")])

    def test_unknown_body_keys_refused(self):
        self.field("guest")
        for path, body in ((f"/api/assets/{self.asset}/metadata", {"key": "guest", "values": ["x"], "extra": 1}),
                           ("/api/schema/fields", {"key": "q", "label": "Q", "type": "text", "extra": 1}),
                           ("/api/schema/fields/guest/terms", {"terms": ["a"], "extra": 1}),
                           ("/api/schema/fields/guest/retire", {"extra": 1}),
                           ("/api/schema/fields/guest/terms/retire", {"term": "a", "extra": 1})):
            s, p = self.post(path, body, token=self.admin)
            self.assertEqual((s, p["error"]["code"]), (422, "bad_input"), path)

    def test_metadata_body_shapes(self):
        self.field("guest")
        for body in ({}, {"key": "guest"}, {"values": ["x"]}, {"key": "guest", "values": "x"}, {"key": 5, "values": []}):
            s, p = self.post(f"/api/assets/{self.asset}/metadata", body, token=self.op)
            self.assertEqual(s, 422, body)
            self.assertIn(p["error"]["code"], ("bad_input", "unknown_field"), body)


if __name__ == "__main__":
    unittest.main()
