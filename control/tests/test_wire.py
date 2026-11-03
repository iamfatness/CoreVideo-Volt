"""Wire intake: parsers, polling with an injected fetcher, the inbox, and the human take step (plan Task 7)."""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.operator_harness import seed
from tests.test_operator_api import TOKEN, Api
from volt import serve, wire
from volt.rundown import open_show
from volt.store import Store

GOLD = Path(__file__).parent / "golden" / "wire"
NOW = datetime(2026, 11, 1, 15, 0, 0, tzinfo=timezone.utc)
JSON_MAP = {"items": "data.stories", "externalId": "id", "headline": "head", "body": "text", "byline": "author.name", "publishedAt": "issued"}


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def blob(name):
    return (GOLD / name).read_bytes()


class Parsers(unittest.TestCase):
    def test_rss(self):
        got = wire.parse_rss(blob("rss.xml"))
        self.assertEqual([s.headline for s in got], ["Council approves bridge & budget", "Second story, no guid"])   # the headline-less item is skipped
        a, b = got
        self.assertEqual((a.external_id, a.byline, a.published_at), ("wire-001", "Dana Reyes", "2026-11-01T14:30:00Z"))
        self.assertEqual(a.body, "Council voted six to three.\n\nThe vote follows two years of debate.")      # HTML reduced to text, script content dropped
        self.assertNotIn("alert", a.body)
        self.assertRegex(b.external_id, r"^https://wire\.example/story/2$")                                 # no guid: the link identifies it
        self.assertIsNone(b.published_at)

    def test_atom(self):
        a, b = wire.parse_atom(blob("atom.xml"))
        self.assertEqual((a.headline, a.byline, a.published_at), ("Storm moves east", "Weather desk", "2026-11-01T07:15:00Z"))   # +02:00 becomes UTC
        self.assertEqual(a.body, "Rain arrives by evening.\nWinds reach 60 km/h.")                       # content preferred over summary
        self.assertEqual((b.body, b.published_at), ("Only a summary here.", "2026-11-01T10:00:00Z"))      # no zone is read as UTC

    def test_newsml(self):
        (s,) = wire.parse_newsml(blob("newsml.xml"))
        self.assertEqual((s.external_id, s.headline, s.byline, s.published_at),
                         ("urn:newsml:example.com:20261101:N1", "Markets close higher", "By Sam Okafor", "2026-11-01T12:00:00Z"))
        self.assertEqual(s.body, "Stocks rose on Friday.\n\nVolume was light.")

    def test_json_with_a_mapping(self):
        a, b = wire.parse_json(blob("feed.json"), JSON_MAP)
        self.assertEqual((a.external_id, a.headline, a.byline, a.published_at), ("4411", "Road closures this weekend", "Traffic desk", "2026-11-01T08:00:00Z"))
        self.assertEqual(a.body, "Main Street closes at noon.\nExpect delays.")
        self.assertEqual((b.external_id, b.byline, b.published_at), ("abc", "", "2026-11-01T09:00:00Z"))   # a number is read as a UNIX time
        with self.assertRaises(ValueError):
            wire.parse_json(b"[]", {"headline": "h"})                                                       # no externalId in the mapping
        with self.assertRaises(wire.WireError):
            wire.parse_json(b"not json", JSON_MAP)
        with self.assertRaises(wire.WireError):
            wire.parse_json(b'{"data": {}}', JSON_MAP)                                                      # items path leads nowhere

    def test_doctype_and_entity_refused_before_parse(self):
        for parse in (wire.parse_rss, wire.parse_atom, wire.parse_newsml):
            with self.assertRaises(wire.WireError) as cm:
                parse(blob("doctype.xml"))
            self.assertEqual(cm.exception.code, "doctype_refused")
        for hostile in (b'<?xml version="1.0"?><!doctype x><rss/>', b'<rss><!ENTITY a "b"></rss>'):
            with self.assertRaises(wire.WireError):
                wire.parse_rss(hostile)

    def test_encodings_oversize_and_garbage(self):
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_rss("<rss/>".encode("utf-16"))
        self.assertEqual(cm.exception.code, "bad_encoding")
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_rss(b"<rss>" + b"x" * (wire.MAX_FEED_BYTES + 1))
        self.assertEqual(cm.exception.code, "too_large")
        with self.assertRaises(wire.WireError) as cm:
            wire.parse_rss(b"<rss><item>")
        self.assertEqual(cm.exception.code, "bad_xml")

    def test_html_body_reduced_to_text(self):
        text = wire.to_text('<h1>Title</h1><p>One &amp; two</p><style>p{}</style><p>Three<br>four</p><img src=x onerror=alert(1)>')
        self.assertEqual(text, "Title\n\nOne & two\n\nThree\nfour")
        self.assertEqual(wire.to_text("  plain   text \n\n\n\nmore "), "plain text\n\nmore")

    def test_a_long_feed_is_cut(self):
        items = "".join(f"<item><title>T{i}</title><guid>g{i}</guid></item>" for i in range(wire.MAX_ITEMS + 50))
        self.assertEqual(len(wire.parse_rss(f"<rss><channel>{items}</channel></rss>".encode())), wire.MAX_ITEMS)


class Config(unittest.TestCase):
    def test_no_feeds_configured_means_none(self):
        self.assertEqual(wire.feeds_from_env({}), [])
        self.assertEqual(wire.feeds_from_env({"VOLT_WIRE_FEEDS": "   "}), [])

    def test_feeds_and_mapping_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.json"
            path.write_text(json.dumps({"agency": JSON_MAP}), encoding="utf-8")
            feeds = wire.feeds_from_env({"VOLT_WIRE_FEEDS": "local=rss@https://a.example/rss; agency=json@http://b.example/x?k=1,2\nnx=newsml@https://c.example/n",
                                         "VOLT_WIRE_MAPPINGS": str(path)})
        self.assertEqual([(f.name, f.kind) for f in feeds], [("local", "rss"), ("agency", "json"), ("nx", "newsml")])
        self.assertEqual(feeds[1].url, "http://b.example/x?k=1,2")
        self.assertEqual(feeds[1].mapping, JSON_MAP)

    def test_bad_entries_are_refused_without_printing_the_address(self):
        for bad in ("noequals", "a=rss@ftp://x.example/f", "a=nope@https://x.example", "a b=rss@https://x.example", "a=json@https://secret.example/k?key=SECRET",
                    "a=rss@https://x.example a=atom@https://y.example"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as cm:
                wire.feeds_from_env({"VOLT_WIRE_FEEDS": bad})
            self.assertNotIn("SECRET", str(cm.exception))


class Poll(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "volt.db")
        self.store = Store(self.path)
        self.rss = wire.Feed("local", "rss", "https://a.example/rss?key=SECRET")
        self.atom = wire.Feed("other", "atom", "https://b.example/atom")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def count(self, state=None):
        q, p = "SELECT COUNT(*) FROM wire_story", ()
        if state:
            q, p = q + " WHERE state = ?", (state,)
        return self.store.conn.execute(q, p).fetchone()[0]

    def fetcher(self, files):
        calls = []

        def fetch(url, timeout, max_bytes):
            calls.append((url, timeout, max_bytes))
            data = files[url]
            if isinstance(data, Exception):
                raise data
            return data
        fetch.calls = calls
        return fetch

    def test_no_feeds_configured_fetches_nothing(self):
        f = self.fetcher({})
        self.assertEqual(wire.poll(self.store, [], f, NOW), {})
        self.assertEqual(f.calls, [])

    def test_poll_fills_the_inbox_and_bounds_the_fetch(self):
        f = self.fetcher({self.rss.url: blob("rss.xml")})
        self.assertEqual(wire.poll(self.store, [self.rss], f, NOW), {"local": 2})
        self.assertEqual(self.count("new"), 2)
        self.assertEqual(f.calls, [(self.rss.url, wire.FETCH_TIMEOUT, wire.MAX_FEED_BYTES)])
        row = self.store.conn.execute("SELECT * FROM wire_feed WHERE feed = 'local'").fetchone()
        self.assertEqual((row["last_ok_at"], row["last_error"], row["next_attempt_at"]), ("2026-11-01T15:00:00Z", None, "2026-11-01T15:01:00Z"))

    def test_a_feed_is_not_fetched_again_before_its_next_attempt(self):
        f = self.fetcher({self.rss.url: blob("rss.xml")})
        wire.poll(self.store, [self.rss], f, NOW)
        wire.poll(self.store, [self.rss], f, NOW + timedelta(seconds=30))
        self.assertEqual(len(f.calls), 1)
        wire.poll(self.store, [self.rss], f, NOW + timedelta(seconds=61))
        self.assertEqual(len(f.calls), 2)

    def test_oversize_timeout_and_garbage_are_recorded_not_raised(self):
        whats = (wire.WireError("too_large", "the feed is larger than the limit"), TimeoutError("timed out"),
                 OSError(f"cannot reach {self.rss.url}"), b"<rss><item>")
        for n, what in enumerate(whats):
            with self.subTest(what=repr(what)[:40]):
                f = self.fetcher({self.rss.url: what})
                report = wire.poll(self.store, [self.rss], f, NOW + timedelta(minutes=10 * n))
                self.assertIn(report["local"], ("too_large", "error", "bad_xml"))
                row = self.store.conn.execute("SELECT * FROM wire_feed WHERE feed = 'local'").fetchone()
                self.assertTrue(row["last_error"])
                self.assertNotIn("SECRET", row["last_error"])                               # the feed's address never reaches the record
                self.assertEqual(row["next_attempt_at"], _stamp(NOW + timedelta(minutes=10 * n + 5)))
        self.assertEqual(self.count(), 0)

    def test_one_bad_feed_does_not_stop_others(self):
        f = self.fetcher({self.rss.url: RuntimeError("boom"), self.atom.url: blob("atom.xml")})
        self.assertEqual(wire.poll(self.store, [self.rss, self.atom], f, NOW), {"local": "error", "other": 2})
        self.assertEqual(self.count("new"), 2)
        feeds = {f["feed"]: f for f in wire.list_inbox(self.store)["feeds"]}
        self.assertTrue(feeds["local"]["lastError"])
        self.assertIsNone(feeds["other"]["lastError"])

    def test_a_good_poll_clears_the_error(self):
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: RuntimeError("boom")}), NOW)
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: blob("rss.xml")}), NOW + timedelta(minutes=6))
        self.assertIsNone(self.store.conn.execute("SELECT last_error FROM wire_feed").fetchone()[0])

    def test_duplicate_external_id_updates_new_row_leaves_taken_row(self):
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: blob("rss.xml")}), NOW)
        again = blob("rss.xml").replace(b"Council approves bridge &amp; budget", b"Council approves bridge, corrected")
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: again}), NOW + timedelta(minutes=2))
        self.assertEqual(self.count(), 2)                                                    # updated in place, never duplicated
        self.assertEqual(self.store.conn.execute("SELECT headline FROM wire_story WHERE external_id = 'wire-001'").fetchone()[0], "Council approves bridge, corrected")
        self.store.conn.execute("UPDATE wire_story SET state = 'taken', headline = 'as taken' WHERE external_id = 'wire-001'")
        self.store.conn.commit()
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: blob("rss.xml")}), NOW + timedelta(minutes=4))
        self.assertEqual(self.store.conn.execute("SELECT headline FROM wire_story WHERE external_id = 'wire-001'").fetchone()[0], "as taken")

    def test_empty_or_malformed_feed_leaves_the_inbox_untouched(self):
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: blob("rss.xml")}), NOW)
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: b"<rss><channel/></rss>"}), NOW + timedelta(minutes=2))
        wire.poll(self.store, [self.rss], self.fetcher({self.rss.url: b"garbage"}), NOW + timedelta(minutes=4))
        self.assertEqual(self.count("new"), 2)


class Take(Api):
    def setUp(self):
        super().setUp()
        serve.ROOM, self.show, _ = seed(self.store, "normal")
        wire.poll(self.store, [wire.Feed("local", "rss", "https://a.example/r")], lambda *a: blob("rss.xml"), NOW)
        self.ids = {r["headline"]: r["id"] for r in self.store.conn.execute("SELECT id, headline FROM wire_story")}
        self.first = self.ids["Council approves bridge & budget"]

    def item(self, item_id):
        return self.store.conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()

    def test_take_makes_draft_item_with_source_line_and_links_wire_row(self):
        status, out = self.post(f"/api/wire/{self.first}/take", {"show": self.show})
        self.assertEqual((status, out["ok"], out["placed"]), (200, True, True))
        row = self.item(out["item"])
        self.assertEqual((row["status"], row["story_status"], row["script_rev"], row["approved_by"], row["source_wire_id"]), ("ready", "draft", 1, None, self.first))
        self.assertEqual(row["slug"], "Council approves bridge & budget")
        self.assertEqual(row["script"].split("\n")[0], "[WIRE local - Dana Reyes - 2026-11-01T14:30:00Z]")
        self.assertIn("Council voted six to three.", row["script"])
        w = self.store.conn.execute("SELECT state, item_id FROM wire_story WHERE id = ?", (self.first,)).fetchone()
        self.assertEqual((w["state"], w["item_id"]), ("taken", out["item"]))
        ev = self.store.conn.execute("SELECT kind, actor FROM story_event WHERE item_id = ?", (out["item"],)).fetchall()
        self.assertEqual([tuple(e) for e in ev], [("import", "desk")])
        self.assertEqual(out["rundown"]["items"][-1]["id"], out["item"])

    def test_slug_and_position_are_honoured(self):
        _, out = self.post(f"/api/wire/{self.first}/take", {"show": self.show, "slug": "My slug", "position": 1})
        self.assertEqual(self.item(out["item"])["slug"], "My slug")
        # position 1 is before the item already on air: it cannot go there, so it stays at the end and the answer says so
        self.assertEqual(out["placed"], False)
        self.assertEqual(self.statuses(self.show)[-1][0], out["item"])
        _, out2 = self.post(f"/api/wire/{self.ids['Second story, no guid']}/take", {"show": self.show, "position": 99})
        self.assertTrue(out2["placed"])

    def test_wire_text_never_becomes_a_cue_or_a_take(self):
        before = self.store.conn.execute("SELECT COUNT(*) FROM cue").fetchone()[0]
        self.post(f"/api/wire/{self.ids['Second story, no guid']}/take", {"show": self.show})
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM cue").fetchone()[0], before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE status = 'on air'").fetchone()[0], 1)
        script = self.store.conn.execute("SELECT script FROM item WHERE source_wire_id = ?", (self.ids["Second story, no guid"],)).fetchone()[0]
        self.assertNotRegex(script, r"(?m)^//")                                                      # a wire line is not turned into a hidden note

    def test_double_take_is_one_item(self):
        results, errors = [], []

        def go():
            s = Store(self.path)
            try:
                results.append(wire.take(s, self.first, self.show, None, None, "desk"))
            except wire.WireRefusal as exc:
                errors.append(exc.code)
            finally:
                s.close()
        threads = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(results), errors), (1, ["already_taken"] * 3))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE source_wire_id = ?", (self.first,)).fetchone()[0], 1)
        self.refused(409, "already_taken", f"/api/wire/{self.first}/take", {"show": self.show})

    def test_dismiss_then_take_refused(self):
        s, out = self.post(f"/api/wire/{self.first}/dismiss")
        self.assertEqual((s, out["state"]), (200, "dismissed"))
        self.assertEqual(self.post(f"/api/wire/{self.first}/dismiss")[0], 200)                       # twice is harmless
        self.refused(409, "already_taken", f"/api/wire/{self.first}/take", {"show": self.show})
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM item WHERE source_wire_id IS NOT NULL").fetchone()[0], 0)

    def test_dismiss_after_take_refused(self):
        self.post(f"/api/wire/{self.first}/take", {"show": self.show})
        self.refused(409, "already_taken", f"/api/wire/{self.first}/dismiss")

    def test_take_into_unknown_show_404_and_unknown_wire_404(self):
        self.refused(404, "not_found", f"/api/wire/{self.first}/take", {"show": "nope"})
        self.refused(404, "not_found", "/api/wire/nope/take", {"show": self.show})
        self.refused(404, "not_found", "/api/wire/nope/dismiss")
        self.assertEqual(self.store.conn.execute("SELECT state FROM wire_story WHERE id = ?", (self.first,)).fetchone()[0], "new")   # nothing moved
        self.refused(422, "bad_input", f"/api/wire/{self.first}/take", {"show": self.show, "position": 0})
        self.refused(422, "bad_input", f"/api/wire/{self.first}/take", {"show": self.show, "extra": 1})
        self.refused(422, "bad_input", f"/api/wire/{self.first}/take", {})

    def test_inbox_lists_newest_first_pages_and_reports_feeds(self):
        status, out, _ = self.call("GET", "/api/wire?state=new&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["stories"]), 1)
        self.assertTrue(out["next"])
        _, out2, _ = self.call("GET", "/api/wire?state=new&limit=1&cursor=" + out["next"].replace("|", "%7C"))
        self.assertEqual(len(out2["stories"]), 1)
        self.assertIsNone(out2["next"])
        self.assertNotEqual(out["stories"][0]["id"], out2["stories"][0]["id"])
        self.assertEqual(out["feeds"][0]["feed"], "local")
        self.assertNotIn("body", out["stories"][0])
        self.assertEqual(self.call("GET", "/api/wire?state=bogus")[0], 422)
        self.assertEqual(self.call("GET", "/api/wire?x=1")[0], 422)

    def test_roles_and_audit(self):
        self.assertEqual(serve.ROUTE_ROLES["act_wire_take"], "operator")
        self.assertEqual(serve.ROUTE_ROLES["act_wire_dismiss"], "operator")
        self.assertEqual(serve._AUDIT_KIND["wire"], "wire")
        self.post(f"/api/wire/{self.first}/take", {"show": self.show})
        row = self.store.conn.execute("SELECT actor, action, target_kind, target_id FROM audit WHERE action LIKE 'wire%'").fetchone()
        self.assertEqual(tuple(row), ("desk", "wire_take", "wire", self.first))

    def test_a_token_is_needed(self):
        serve.TOKEN = TOKEN
        self.assertEqual(self.post(f"/api/wire/{self.first}/take", {"show": self.show})[0], 401)
        self.assertEqual(self.call("GET", "/api/wire")[0], 401)


class Schema(unittest.TestCase):
    def test_wire_feed_table_is_in_both_schemas_and_the_migration(self):
        from volt import migrations, store as store_mod
        root = Path(store_mod.__file__).parent
        self.assertIn("CREATE TABLE IF NOT EXISTS wire_feed", (root / "schema.sql").read_text(encoding="utf-8"))
        self.assertIn("CREATE TABLE IF NOT EXISTS wire_feed", store_mod.SCHEMA)
        self.assertTrue(any("wire_feed" in " ".join(s for s in m.sqlite if isinstance(s, str)) for m in migrations.MIGRATIONS))


from tests.pgutil import PgCase


class PostgresWire(PgCase):
    """The same poll, inbox paging and double take on a live Postgres (skipped without VOLT_TEST_PG_DSN)."""

    def test_poll_page_take_and_race(self):
        store = self.store()
        show = open_show(store, "Live")
        feed = wire.Feed("local", "rss", "https://a.example/r")
        wire.poll(store, [feed], lambda *a: blob("rss.xml"), NOW)
        wire.poll(store, [feed], lambda *a: blob("rss.xml"), NOW + timedelta(seconds=10))     # not due: nothing fetched
        page = wire.list_inbox(store, "new", 1)
        self.assertEqual((len(page["stories"]), bool(page["next"])), (1, True))
        rest = wire.list_inbox(store, "new", 1, page["next"])
        self.assertEqual((len(rest["stories"]), rest["next"]), (1, None))
        self.assertEqual(page["feeds"][0]["lastOkAt"], "2026-11-01T15:00:00Z")
        wid = page["stories"][0]["id"]
        results, errors = [], []

        def go():
            s = self.store()
            try:
                results.append(wire.take(s, wid, show, None, None, "desk"))
            except wire.WireRefusal as exc:
                errors.append(exc.code)
        threads = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(results), errors), (1, ["already_taken"] * 2))
        wire.poll(store, [feed], lambda *a: RuntimeError("x") and (_ for _ in ()).throw(RuntimeError("boom")), NOW + timedelta(minutes=5))
        self.assertTrue(wire.list_inbox(store)["feeds"][0]["lastError"])


if __name__ == "__main__":
    unittest.main()
