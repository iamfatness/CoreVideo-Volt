"""Documentation is part of the contract: every route, table and environment variable the code has is written down."""
import re
import unittest
from pathlib import Path

from volt import serve, store

DOCS = Path(serve.ROOT).parents[2] / "docs"
VOLT = Path(serve.__file__).parent


def _read(rel):
    return (DOCS / rel).read_text(encoding="utf-8")


def _norm(text):
    return re.sub(r"\{[A-Za-z_]+\}", "{}", text)


def _route_paths():
    out = []
    for rx, _fn in serve.ROUTES:
        p = rx.pattern.lstrip("^")
        p = re.sub(r"\(\[A-Za-z0-9_\.:-\]\{1,128\}\)", "{}", p)
        p = p.replace("()", "").rstrip("$")
        out.append(p)
    return out


class Routes(unittest.TestCase):
    def test_new_post_routes_documented(self):
        md = _norm("".join(f.read_text(encoding="utf-8") for f in (DOCS / "specs").glob("*.md")))
        missing = [p for p in _route_paths() if p not in md]
        self.assertEqual(missing, [], "routes absent from docs/specs")


class Tables(unittest.TestCase):
    def test_every_table_is_in_schema_store_and_a_migration(self):
        from volt import migrations
        schema = (VOLT / "schema.sql").read_text(encoding="utf-8")
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
        self.assertTrue(tables)
        sources = {"store.SCHEMA": store.SCHEMA, "migrations": Path(migrations.__file__).read_text(encoding="utf-8")}
        for t in sorted(tables):
            for where, src in sources.items():
                self.assertRegex(src, rf"\b{t}\b", f"{t} missing from {where}")

    def test_collaboration_tables_are_in_the_data_model(self):
        md = _read("data-model.md")
        for t in ("volt_group", "group_member", "session", "auth_flow", "identity", "oidc_group_map", "show_acl", "lease", "presence", "comment", "comment_mention", "notification"):
            self.assertRegex(md, rf"`{t}`", t)


class Env(unittest.TestCase):
    def test_every_new_env_var_is_in_operations(self):
        ops = _read("operations.md")
        src = "".join(p.read_text(encoding="utf-8") for p in VOLT.glob("*.py"))
        names = set(re.findall(r"\bVOLT_(?:OIDC|SESSION|PUBLIC)[A-Z0-9_]*", src))
        self.assertTrue(names)
        for n in sorted(names):
            self.assertIn(f"`{n}`", ops, n)

    def test_every_env_var_is_written_down_somewhere(self):
        src = "".join(p.read_text(encoding="utf-8") for p in VOLT.glob("*.py"))
        docs = "".join(p.read_text(encoding="utf-8") for p in DOCS.rglob("*.md"))
        docs += (DOCS.parent / "README.md").read_text(encoding="utf-8")
        missing = [n for n in sorted(set(re.findall(r"\bVOLT_[A-Z0-9_]+", src))) if n not in docs]
        self.assertEqual(missing, [])


class AuditIsAppendOnly(unittest.TestCase):
    def test_nothing_updates_or_deletes_audit_rows(self):
        pat = re.compile(r"\b(UPDATE|DELETE\s+FROM|INSERT\s+OR\s+REPLACE\s+INTO|DROP\s+TABLE|TRUNCATE(\s+TABLE)?)\s+audit\b", re.I)
        hits = [f"{p.name}:{n}" for p in list(VOLT.glob("*.py")) + [VOLT / "schema.sql"]
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pat.search(line)]
        self.assertEqual(hits, [])


class HarnessScenarios(unittest.TestCase):
    """The browser-check scenarios the identity plan names exist and seed what their screens need."""

    def test_the_plan_scenarios_are_registered(self):
        from tests import operator_harness as h
        for name in ("signed_out_with_providers", "lease_held_by_other", "lease_lost", "presence_two_users", "comments_thread", "activity_filtered"):
            self.assertIn(name, h.SCENARIOS)

    def test_activity_filtered_seeds_varied_audit_rows(self):
        from tests import operator_harness as h
        from volt.store import Store
        store = Store()
        h.seed(store, "activity_filtered")
        rows = store.conn.execute("SELECT actor, action, target_kind FROM audit").fetchall()
        self.assertGreaterEqual(len({r["actor"] for r in rows}), 3)
        self.assertGreaterEqual(len({r["action"] for r in rows}), 5)
        self.assertGreaterEqual(len(rows), 30)

    def test_signed_out_with_providers_turns_sign_in_on_and_requires_a_token(self):
        import os
        from tests import operator_harness as h
        from volt import serve
        old = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.OIDC_CONFIG, os.environ.get("VOLT_PUBLIC_URL"))
        srv = tmp = None
        try:
            srv, tmp, _show, _spans = h.serve_scenario("signed_out_with_providers", 0, token="t" * 24)
            self.assertEqual([p.id for p in serve.OIDC_CONFIG.providers], ["google", "microsoft"])
            self.assertEqual(serve.TOKEN, "t" * 24)
        finally:
            if srv is not None:
                srv.server_close()
                tmp.cleanup()
            serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.OIDC_CONFIG = old[:4]
            if old[4] is None:
                os.environ.pop("VOLT_PUBLIC_URL", None)
            else:
                os.environ["VOLT_PUBLIC_URL"] = old[4]


if __name__ == "__main__":
    unittest.main()
