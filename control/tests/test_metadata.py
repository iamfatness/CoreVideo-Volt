"""Typed asset metadata: field definitions, vocabularies, validation, purge. Runs on SQLite and, with VOLT_TEST_PG_DSN, Postgres."""
import unittest

from volt import metadata, migrations, retention
from volt.metadata import MetaError
from volt.store import Store

from tests.pgutil import PgCase


class MetaCases:
    """The cases; a subclass supplies self.store."""

    def asset(self, show=None):
        return self.store.mint_asset(owner="desk", show_name=show).id

    def code(self, fn, *a, **kw):
        with self.assertRaises(MetaError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def test_define_and_list_field(self):
        f = metadata.define_field(self.store, "guest", "Guest", "text")
        self.assertEqual((f["key"], f["label"], f["type"], f["required"], f["multi"]), ("guest", "Guest", "text", False, False))
        metadata.define_field(self.store, "mood", "Mood", "enum", multi=True)
        metadata.add_terms(self.store, "mood", ["calm", "tense"])
        listed = {f["key"]: f for f in metadata.list_schema(self.store)}
        self.assertEqual(set(listed), {"guest", "mood"})
        self.assertEqual([t["term"] for t in listed["mood"]["terms"]], ["calm", "tense"])
        self.assertEqual(listed["guest"]["terms"], [])

    def test_key_pattern_refused(self):
        for key in ("Bad-Key", "", "9lives", "x" * 41, "has space", None, 5):
            self.assertEqual(self.code(metadata.define_field, self.store, key, "L", "text"), "bad_input", key)
        metadata.define_field(self.store, "x" * 40, "L", "text")
        self.assertEqual(self.code(metadata.define_field, self.store, "ok", "L", "color"), "bad_input")
        self.assertEqual(self.code(metadata.define_field, self.store, "ok", "", "text"), "bad_input")
        self.assertEqual(self.code(metadata.define_field, self.store, "ok", "L", "number", multi=True), "bad_input")

    def test_type_locked_after_first_value(self):
        a = self.asset()
        metadata.define_field(self.store, "guest", "Guest", "text")
        self.assertEqual(self.code(metadata.define_field, self.store, "guest", "Guest", "text"), "field_exists")
        # no value yet: a type change is an edit that is allowed
        self.assertEqual(metadata.update_field(self.store, "guest", type="longtext")["type"], "longtext")
        metadata.set_values(self.store, a, "guest", ["Okonkwo"], "desk")
        self.assertEqual(self.code(metadata.update_field, self.store, "guest", type="text"), "type_locked")
        self.assertEqual(self.code(metadata.define_field, self.store, "guest", "Guest", "number"), "type_locked")
        self.assertEqual(metadata.update_field(self.store, "guest", label="Guest name")["label"], "Guest name")

    def test_number_date_bool_enum_text_validated(self):
        a = self.asset()
        for key, typ in (("n", "number"), ("d", "date"), ("b", "bool"), ("e", "enum"), ("t", "text"), ("lt", "longtext")):
            metadata.define_field(self.store, key, key, typ)
        metadata.add_terms(self.store, "e", ["red"])
        bad = [("n", float("nan"), "type_mismatch"), ("n", 1e16, "type_mismatch"), ("n", "5", "type_mismatch"),
               ("n", True, "type_mismatch"), ("n", float("inf"), "type_mismatch"),
               ("d", "2026-02-30", "type_mismatch"), ("d", "26-02-03", "type_mismatch"), ("d", 20260203, "type_mismatch"),
               ("b", "true", "type_mismatch"), ("b", 1, "type_mismatch"),
               ("e", "blue", "not_in_vocabulary"), ("e", 3, "type_mismatch"),
               ("t", "x" * 1001, "type_mismatch"), ("t", 5, "type_mismatch"), ("lt", "x" * 10001, "type_mismatch")]
        for key, value, code in bad:
            self.assertEqual(self.code(metadata.set_values, self.store, a, key, [value], "desk"), code, (key, value))
        for key, value in (("n", -3.5), ("n", 1e15), ("d", "2026-02-28"), ("b", False), ("e", "red"),
                           ("t", "x" * 1000), ("lt", "y" * 10000)):
            metadata.set_values(self.store, a, key, [value], "desk")
        got = metadata.get_values(self.store, a)["values"]
        self.assertEqual((got["n"], got["d"], got["b"], got["e"]), ([1e15], ["2026-02-28"], [False], ["red"]))

    def test_enum_retired_term_valid_on_old_values_refused_on_new(self):
        a, b = self.asset(), self.asset()
        metadata.define_field(self.store, "mood", "Mood", "enum")
        metadata.add_terms(self.store, "mood", ["calm", "tense"])
        metadata.set_values(self.store, a, "mood", ["tense"], "desk")
        metadata.retire_term(self.store, "mood", "tense")
        metadata.set_values(self.store, a, "mood", ["tense"], "desk")  # unchanged old value still validates
        self.assertEqual(self.code(metadata.set_values, self.store, b, "mood", ["tense"], "desk"), "not_in_vocabulary")
        self.assertEqual([t["term"] for t in metadata.list_schema(self.store)[0]["terms"]], ["calm"])
        self.assertEqual(self.code(metadata.retire_term, self.store, "mood", "ghost"), "not_in_vocabulary")

    def test_show_scoped_field_unknown_for_other_show(self):
        a, b = self.asset("news"), self.asset("sport")
        metadata.define_field(self.store, "anchor", "Anchor", "text", show_name="news")
        metadata.set_values(self.store, a, "anchor", ["Amy"], "desk")
        self.assertEqual(self.code(metadata.set_values, self.store, b, "anchor", ["Bo"], "desk"), "unknown_field")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "nope", ["x"], "desk"), "unknown_field")
        self.assertEqual([f["key"] for f in metadata.list_schema(self.store, "sport")], [])
        self.assertEqual([f["key"] for f in metadata.list_schema(self.store, "news")], ["anchor"])
        self.assertEqual([f["key"] for f in metadata.list_schema(self.store)], ["anchor"])

    def test_multi_cap_20(self):
        a = self.asset()
        metadata.define_field(self.store, "tags", "Tags", "text", multi=True)
        metadata.define_field(self.store, "one", "One", "text")
        metadata.set_values(self.store, a, "tags", [f"t{i}" for i in range(20)], "desk")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "tags", [f"t{i}" for i in range(21)], "desk"), "bad_input")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "one", ["a", "b"], "desk"), "bad_input")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "tags", ["a", "a"], "desk"), "bad_input")
        self.assertEqual(len(metadata.get_values(self.store, a)["values"]["tags"]), 20)

    def test_required_cannot_be_cleared(self):
        a = self.asset()
        metadata.define_field(self.store, "slug", "Slug", "text", required=True)
        self.assertEqual(self.code(metadata.set_values, self.store, a, "slug", [], "desk"), "required_field")
        metadata.set_values(self.store, a, "slug", ["x"], "desk")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "slug", [], "desk"), "required_field")
        metadata.define_field(self.store, "note", "Note", "text")
        metadata.set_values(self.store, a, "note", ["x"], "desk")
        out = metadata.set_values(self.store, a, "note", [], "desk")
        self.assertEqual(out, {"old": ["x"], "new": []})
        self.assertNotIn("note", metadata.get_values(self.store, a)["values"])

    def test_missing_lists_required_unfilled(self):
        a = self.asset("news")
        metadata.define_field(self.store, "slug", "Slug", "text", required=True)
        metadata.define_field(self.store, "anchor", "Anchor", "text", required=True, show_name="sport")
        metadata.define_field(self.store, "opt", "Opt", "text")
        self.assertEqual(metadata.get_values(self.store, a)["missing"], ["slug"])
        metadata.set_values(self.store, a, "slug", ["x"], "desk")
        self.assertEqual(metadata.get_values(self.store, a), {"values": {"slug": ["x"]}, "missing": []})

    def test_active_field_cap_60(self):
        for i in range(60):
            metadata.define_field(self.store, f"f{i}", "F", "text")
        self.assertEqual(self.code(metadata.define_field, self.store, "extra", "F", "text"), "bad_input")
        metadata.retire_field(self.store, "f0")
        metadata.define_field(self.store, "extra", "F", "text")

    def test_term_caps_and_enum_only(self):
        metadata.define_field(self.store, "mood", "Mood", "enum")
        metadata.define_field(self.store, "t", "T", "text")
        self.assertEqual(self.code(metadata.add_terms, self.store, "t", ["a"]), "bad_input")
        self.assertEqual(self.code(metadata.add_terms, self.store, "ghost", ["a"]), "unknown_field")
        self.assertEqual(self.code(metadata.add_terms, self.store, "mood", ["", "ok"]), "bad_input")
        self.assertEqual(self.code(metadata.add_terms, self.store, "mood", ["x" * 101]), "bad_input")
        self.assertEqual(metadata.list_schema(self.store)[0]["terms"], [])  # the refused batches added nothing
        metadata.add_terms(self.store, "mood", [f"t{i}" for i in range(500)])
        self.assertEqual(self.code(metadata.add_terms, self.store, "mood", ["one-more"]), "bad_input")
        self.assertEqual(metadata.add_terms(self.store, "mood", ["t1"]), [])  # already there: nothing added

    def test_retired_field_hidden_but_values_kept(self):
        a = self.asset()
        metadata.define_field(self.store, "guest", "Guest", "text")
        metadata.set_values(self.store, a, "guest", ["Okonkwo"], "desk")
        metadata.retire_field(self.store, "guest")
        self.assertEqual(metadata.list_schema(self.store), [])
        self.assertEqual([f["key"] for f in metadata.list_schema(self.store, include_retired=True)], ["guest"])
        self.assertEqual(metadata.get_values(self.store, a)["values"], {})
        self.assertEqual(metadata.get_values(self.store, a, include_retired=True)["values"], {"guest": ["Okonkwo"]})
        self.assertEqual(self.code(metadata.set_values, self.store, a, "guest", ["x"], "desk"), "unknown_field")
        self.assertEqual(self.code(metadata.retire_field, self.store, "ghost"), "unknown_field")

    def test_purge_removes_meta_rows(self):
        a = self.asset("news")
        metadata.define_field(self.store, "guest", "Guest", "text")
        metadata.set_values(self.store, a, "guest", ["Okonkwo"], "desk")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM asset_meta_value").fetchone()[0], 1)
        retention.tombstone(self.store, a)
        res = retention.purge_asset(self.store, a, retention.OwnedRoots())
        self.assertEqual(res.status, "purged", res)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM asset_meta_value").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM field_def").fetchone()[0], 1)

    def test_set_replaces_atomically(self):
        a = self.asset()
        metadata.define_field(self.store, "tags", "Tags", "text", multi=True)
        metadata.set_values(self.store, a, "tags", ["a", "b"], "desk")
        self.assertEqual(self.code(metadata.set_values, self.store, a, "tags", ["c", "x" * 1001], "desk"), "type_mismatch")
        self.assertEqual(metadata.get_values(self.store, a)["values"]["tags"], ["a", "b"])
        out = metadata.set_values(self.store, a, "tags", ["c"], "desk")
        self.assertEqual(out, {"old": ["a", "b"], "new": ["c"]})
        self.assertEqual(metadata.get_values(self.store, a)["values"]["tags"], ["c"])

    def test_set_on_unknown_asset(self):
        metadata.define_field(self.store, "guest", "Guest", "text")
        with self.assertRaises(KeyError):
            metadata.set_values(self.store, "nope", "guest", ["x"], "desk")


class SqliteMeta(MetaCases, unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)


class PostgresMeta(MetaCases, PgCase):
    def setUp(self):
        super().setUp()
        self.store = PgCase.store(self)  # shadows the helper method on the instance; nothing else here calls it


class Registry(unittest.TestCase):
    def test_real_and_blob_types_exist(self):
        self.assertEqual(migrations._TYPES["REAL"], ("REAL", "DOUBLE PRECISION"))
        self.assertEqual(migrations._TYPES["BLOB"], ("BLOB", "BYTEA"))

    def test_metadata_tables_exist_on_a_fresh_database(self):
        s = Store(":memory:")
        names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"field_def", "field_term", "asset_meta_value"} <= names)
        s.close()


if __name__ == "__main__":
    unittest.main()
