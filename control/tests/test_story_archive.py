"""The story archive, search and copy (plan Task 9)."""

import re
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.operator_harness import seed
from tests.pgutil import PgCase
from tests.test_operator_api import TOKEN, Api
from volt import building, serve, story
from volt.playout import hold
from volt.rundown import open_show

DOCS = Path(__file__).resolve().parents[2] / "docs"


def make(store, show, slug, script, status=None):
    item, _ = building.add_item(store, show, slug, script)
    if status:
        store.conn.execute("UPDATE item SET story_status = ? WHERE id = ?", (status, item))
        store.conn.commit()
    return item


class Archive(Api):
    def setUp(self):
        super().setUp()
        self.show = open_show(self.store, "Late news")
        self.a = make(self.store, self.show, "Bridge vote", "Council voted six to three.\n// call the mayor about the 100% figure\nIt was close.", "ready")
        self.b = make(self.store, self.show, "Sports roundup", "The Wildcats won 100% of home games_this year.", "draft")
        self.c = make(self.store, self.show, "Weather", "Rain by evening.", "killed")

    def rows(self, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        status, out, _ = self.call("GET", "/api/stories" + ("?" + qs if qs else ""))
        self.assertEqual(status, 200, out)
        return out

    # ---- the archive ----------------------------------------------------------------------------------------
    def test_delete_show_archives_stories_atomically(self):
        s, out = self.post(f"/api/shows/{self.show}/delete")
        self.assertEqual((s, out["ok"]), (200, True), out)
        rows = self.store.conn.execute("SELECT slug, show_title, story_status, original_item_id FROM story_archive ORDER BY slug").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("Bridge vote", "Late news", "ready", self.a), ("Sports roundup", "Late news", "draft", self.b),
                                                    ("Weather", "Late news", "killed", self.c)])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ?", (self.show,)).fetchone()[0], 0)
        self.assertIsNone(self.store.conn.execute("SELECT 1 FROM show WHERE id = ?", (self.show,)).fetchone())

    def test_a_failed_archive_write_leaves_the_show_intact(self):
        with mock.patch.object(building, "_archive_items", side_effect=RuntimeError("disk full")):
            s, out = self.post(f"/api/shows/{self.show}/delete")
        self.assertEqual(s, 500)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ?", (self.show,)).fetchone()[0], 3)
        self.assertIsNotNone(self.store.conn.execute("SELECT 1 FROM show WHERE id = ?", (self.show,)).fetchone())
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM story_archive").fetchone()[0], 0)
        # and a failure part-way through the inserts rolls back the ones already made
        store2 = __import__("volt.store", fromlist=["Store"]).Store(self.path)
        real = store2.conn.execute
        calls = []

        def flaky(sql, params=()):
            if sql.startswith("INSERT INTO story_archive"):
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("boom on the second row")
            return real(sql, params)
        store2.conn.execute = flaky
        with self.assertRaises(RuntimeError):
            building.delete_show(store2, self.show)
        store2.close()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM story_archive").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE show_id = ?", (self.show,)).fetchone()[0], 3)

    def test_a_show_that_has_aired_is_still_refused_and_archives_nothing(self):
        self.store.conn.execute("UPDATE item SET status = 'aired', as_run_in = '2026-11-01T10:00:00Z' WHERE id = ?", (self.a,))
        self.store.conn.commit()
        s, out = self.post(f"/api/shows/{self.show}/delete")
        self.assertEqual((s, out["error"]["code"]), (409, "show_not_deletable"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM story_archive").fetchone()[0], 0)

    # ---- search ---------------------------------------------------------------------------------------------
    def test_search_matches_slug_and_script_case_insensitive(self):
        self.assertEqual({r["slug"] for r in self.rows(q="BRIDGE")["rows"]}, {"Bridge vote"})
        self.assertEqual({r["slug"] for r in self.rows(q="wildcats")["rows"]}, {"Sports roundup"})
        self.assertEqual({r["slug"] for r in self.rows(q="rain")["rows"]}, {"Weather"})
        self.assertEqual(self.rows(q="zzz")["rows"], [])
        self.assertEqual(len(self.rows()["rows"]), 3)

    def test_search_escapes_like_wildcards(self):
        self.assertEqual({r["slug"] for r in self.rows(q="%25")["rows"]}, {"Sports roundup"})        # a literal %, not "anything"; the note's 100% does not count
        self.assertEqual({r["slug"] for r in self.rows(q="_")["rows"]}, {"Sports roundup"})          # a literal underscore
        self.assertEqual({r["slug"] for r in self.rows(q="games_this")["rows"]}, {"Sports roundup"})
        self.assertEqual(self.rows(q="home!")["rows"], [])                                            # the escape character itself is literal too

    def test_search_filters_status_show_dates_and_pages(self):
        self.assertEqual([r["slug"] for r in self.rows(status="killed")["rows"]], ["Weather"])
        self.assertEqual(self.rows(show=self.show)["rows"].__len__(), 3)
        self.assertEqual(self.rows(show="Late%20news")["rows"].__len__(), 3)                          # a show's title works too
        self.assertEqual(self.rows(show="nope")["rows"], [])
        self.assertEqual(len(self.rows(**{"from": "2000-01-01", "to": "2999-12-31"})["rows"]), 3)
        self.assertEqual(self.rows(**{"from": "2999-01-01"})["rows"], [])
        self.assertEqual(self.rows(to="2000-01-01")["rows"], [])
        page1 = self.rows(limit=2)
        self.assertEqual((len(page1["rows"]), bool(page1["next"])), (2, True))
        page2 = self.rows(limit=2, cursor=page1["next"].replace("|", "%7C"))
        self.assertEqual((len(page2["rows"]), page2["next"]), (1, None))
        self.assertEqual(len({r["id"] for r in page1["rows"] + page2["rows"]}), 3)
        for bad in ("status=bogus", "limit=0", "limit=abc", "from=yesterday", "extra=1", "cursor=garbage"):
            self.assertEqual(self.call("GET", "/api/stories?" + bad)[0], 422, bad)

    def test_search_covers_the_archive_and_marks_rows(self):
        self.post(f"/api/shows/{self.show}/delete")
        new = open_show(self.store, "Today")
        make(self.store, new, "Bridge follow-up", "More on the bridge.", "draft")
        rows = self.rows(q="bridge")["rows"]
        self.assertEqual(sorted((r["slug"], r["archived"]) for r in rows), [("Bridge follow-up", False), ("Bridge vote", True)])
        arch = [r for r in rows if r["archived"]][0]
        self.assertTrue(arch["id"].startswith("archive:"))
        self.assertEqual(arch["show"], "Late news")
        self.assertEqual([r["slug"] for r in self.rows(status="killed")["rows"]], ["Weather"])

    def test_search_does_not_expose_private_notes_in_snippets(self):
        for row in self.rows(q="close")["rows"]:
            self.assertNotIn("mayor", row["snippet"])
        everything = str(self.rows()["rows"])
        self.assertNotIn("call the mayor", everything)
        self.assertNotIn("//", everything)
        # a query that only matches words inside a private note finds nothing: a note cannot make a story appear
        self.assertEqual(self.rows(q="mayor")["rows"], [])
        self.assertEqual(self.rows(q="100%25%20figure")["rows"], [])

    def test_search_needs_a_token(self):
        serve.TOKEN = TOKEN
        self.assertEqual(self.call("GET", "/api/stories")[0], 401)
        self.assertEqual(self.call("GET", "/api/stories", token=TOKEN)[0], 200)

    # ---- copy -----------------------------------------------------------------------------------------------
    def test_copy_creates_draft_rev_1_without_approval(self):
        story.set_status(self.store, self.a, "approved", base_rev=1, actor="marcus")
        other = open_show(self.store, "Other")
        s, out = self.post(f"/api/items/{self.a}/copy", {"show": other})
        self.assertEqual((s, out["ok"], out["placed"]), (200, True, True), out)
        row = self.store.conn.execute("SELECT * FROM item WHERE id = ?", (out["item"],)).fetchone()
        self.assertEqual((row["show_id"], row["status"], row["story_status"], row["script_rev"], row["approved_by"], row["approved_rev"], bool(row["floated"])),
                         (other, "ready", "draft", 1, None, None, False))
        self.assertEqual(row["slug"], "Bridge vote")
        self.assertIn("call the mayor", row["script"])                       # the copier is an editor: notes stay in the text
        self.assertEqual(self.store.conn.execute("SELECT story_status FROM item WHERE id = ?", (self.a,)).fetchone()[0], "approved")   # the source is untouched
        self.assertEqual(out["rundown"]["items"][-1]["id"], out["item"])

    def test_copy_from_archive_id(self):
        self.post(f"/api/shows/{self.show}/delete")
        arch = self.rows(q="bridge")["rows"][0]["id"]
        other = open_show(self.store, "Other")
        s, out = self.post(f"/api/items/{arch}/copy", {"show": other})
        self.assertEqual(s, 200, out)
        row = self.store.conn.execute("SELECT slug, story_status, script_rev FROM item WHERE id = ?", (out["item"],)).fetchone()
        self.assertEqual(tuple(row), ("Bridge vote", "draft", 1))
        self.assertEqual(self.post("/api/items/archive:nope/copy", {"show": other})[0], 404)

    def test_copy_into_show_with_hold_still_allowed(self):
        other = open_show(self.store, "Other")
        hold(self.store, other, "held for the producer")
        self.assertEqual(self.post(f"/api/items/{self.b}/copy", {"show": other})[0], 200)       # a copy is not a take

    def test_copy_refusals_and_position(self):
        self.assertEqual(self.post(f"/api/items/{self.a}/copy", {"show": "nope"})[0], 404)
        self.assertEqual(self.post("/api/items/nope/copy", {"show": self.show})[0], 404)
        self.assertEqual(self.post(f"/api/items/{self.a}/copy", {})[0], 422)
        self.assertEqual(self.post(f"/api/items/{self.a}/copy", {"show": self.show, "position": 0})[0], 422)
        s, out = self.post(f"/api/items/{self.a}/copy", {"show": self.show, "position": 1})
        self.assertEqual((s, out["placed"]), (200, True))
        self.assertEqual(self.statuses(self.show)[0][0], out["item"])
        self.assertEqual(serve.ROUTE_ROLES["act_item_copy"], "operator")
        actions = [r["action"] for r in self.store.conn.execute("SELECT action FROM audit WHERE action LIKE '%copy%'")]
        self.assertEqual(actions, ["copy"])


class PostgresArchive(PgCase):
    """The same archive, search, paging and copy on a live Postgres (skipped without VOLT_TEST_PG_DSN)."""

    def test_delete_archive_search_and_copy(self):
        store = self.store()
        show = open_show(store, "Late news")
        a = make(store, show, "Bridge vote", "Council voted.\n// private note\nIt was close.", "ready")
        make(store, show, "Sports 100%", "Wildcats won.", "draft")
        make(store, show, "Weather", "Rain.", "killed")
        self.assertEqual({r["slug"] for r in story.search(store, q="%")["rows"]}, {"Sports 100%"})
        self.assertEqual(story.search(store, q="private")["rows"], [])
        live = story.search(store, status="ready")["rows"]
        self.assertEqual([r["archived"] for r in live], [False])
        building.delete_show(store, show)
        rows = story.search(store, limit=2)
        self.assertEqual((len(rows["rows"]), bool(rows["next"]), all(r["archived"] for r in rows["rows"])), (2, True, True))
        rest = story.search(store, limit=2, cursor=rows["next"])
        self.assertEqual((len(rest["rows"]), rest["next"]), (1, None))
        self.assertEqual(len(story.search(store, **{"from_": "2000-01-01", "to": "2999-12-31"})["rows"]), 3)
        other = open_show(store, "Other")
        arch = [r for r in story.search(store, q="bridge")["rows"]][0]["id"]
        out = story.copy_story(store, arch, other, None, "desk")
        row = store.conn.execute("SELECT slug, story_status, script_rev FROM item WHERE id = ?", (out["item"],)).fetchone()
        self.assertEqual(tuple(row), ("Bridge vote", "draft", 1))
        results = []
        threads = [threading.Thread(target=lambda: results.append(story.copy_story(self.store(), arch, other, None, "desk"))) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 3)                                                            # copies are independent: three copies, three items


class Docs(unittest.TestCase):
    """The shipped doc names every thing the tests cannot prove (the spec's 'will not prove' list)."""

    def test_the_unvalidated_list_is_in_the_shipped_doc(self):
        text = (DOCS / "specs" / "newsroom-scripts.md").read_text(encoding="utf-8").lower()
        for needle in ("unvalidated", "enps", "inews", "dalet", "octopus", "real agency", "physical prompter", "default read time", "languages written without spaces",
                       "many simultaneous editors", "search quality at archive scale"):
            self.assertIn(needle, text, needle)
        for env in ("VOLT_WIRE_FEEDS", "VOLT_WIRE_MAPPINGS", "VOLT_REQUIRE_APPROVAL", "VOLT_APPROVERS"):
            self.assertIn(env.lower(), text, env)

    def test_other_docs_point_at_it(self):
        for rel in ("specs/operator-ui.md", "specs/operator-api.md", "specs/rundown.md", "data-model.md", "roadmap.md"):
            self.assertIn("newsroom-scripts", (DOCS / rel).read_text(encoding="utf-8"), rel)
        self.assertRegex((DOCS.parent / "CHANGELOG.md").read_text(encoding="utf-8"), r"(?i)newsroom scripts")


if __name__ == "__main__":
    unittest.main()
