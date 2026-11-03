"""Comments, mentions and in-app notifications (identity plan, task 8).

Time is injected (a frozen clock) and nothing sleeps. A comment is only ever readable, writable or notifiable by someone who can view
its show; the body is never in the audit table, the log or a URL; and nothing on the on-air path reads these tables.
"""
import contextlib
import io
import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.pgutil import PgCase
from tests.test_operator_api import Api
from tests.test_take import _show
from volt import access, building, discuss, leases, migrations, retention, serve, users
from volt.store import Store

T0 = 1_800_000_000.0
SECRET = "the-quiet-plan-zebra-4417"


def principal(name):
    return serve.Principal(name, "operator", False, ())


class Base:
    """A store with people on it: root (admin), alice, bob, carol (operators), vic (viewer), gone (disabled), and two shows."""

    def build(self, store):
        self.store = store
        self.tokens = {}
        for name, role in (("root", "admin"), ("alice", "operator"), ("bob", "operator"), ("carol", "operator"), ("vic", "viewer"),
                           ("gone", "operator")):
            self.tokens[name] = users.add(store, name, role)[1]
        users.disable(store, "gone")
        self.show = building.create_show(store, "Open show")[0]
        self.items = [building.add_item(store, self.show, f"story {n}", f"text {n}")[0] for n in range(3)]
        self.a, self.b, self.c = self.items
        self.sec = building.create_show(store, "Secret show")[0]
        self.sec_item = building.add_item(store, self.sec, "secret story", "secret text")[0]
        access.set_acl(store, self.sec, "user", "alice", "edit")
        access.set_acl(store, self.sec, "user", "carol", "view")

    def say(self, author, kind, target, body, parent=None, at=T0):
        return discuss.add_comment(self.store, author, kind, target, body, parent, now=at)

    def inbox(self, user, **kw):
        return discuss.notifications(self.store, user, **kw)


class Unit(Base, unittest.TestCase):
    def setUp(self):
        self.build(Store())

    # ---- adding ------------------------------------------------------------

    def test_the_constants_are_the_specs(self):
        self.assertEqual((discuss.BODY_MAX, discuss.RATE_LIMIT, discuss.RATE_WINDOW, discuss.NOTIFICATION_DAYS), (4000, 30, 60, 90))
        self.assertEqual(discuss.KINDS, ("item", "show", "asset"))

    def test_add_comment_returns_the_comment(self):
        got = self.say("alice", "item", self.a, "  First point  ")
        self.assertEqual((got["kind"], got["target"], got["show"], got["author"], got["body"], got["parent"]),
                         ("item", self.a, self.show, "alice", "First point", None))
        self.assertEqual((got["deleted"], got["resolved"], got["mentions"]), (False, False, []))
        self.assertTrue(got["createdAt"].endswith("Z"))
        self.assertTrue(got["id"])

    def test_the_body_is_one_to_4000_characters(self):
        for bad in ("", "   \n ", "x" * 4001, None, 5, ["a"], "nul\x00byte"):
            with self.assertRaises(discuss.DiscussError, msg=repr(bad)[:20]) as ctx:
                self.say("alice", "item", self.a, bad)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "bad_input"))
        self.assertEqual(len(self.say("alice", "item", self.a, "x" * 4000)["body"]), 4000)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0], 1)

    def test_the_target_must_exist_and_the_kind_be_known(self):
        for kind, target, status in (("item", "nope", 404), ("show", "nope", 404), ("asset", "nope", 404), ("cue", self.a, 422), (None, "x", 422)):
            with self.assertRaises(discuss.DiscussError, msg=(kind, target)) as ctx:
                self.say("alice", kind, target, "hi")
            self.assertEqual(ctx.exception.status, status)

    def test_show_and_asset_comments(self):
        got = self.say("alice", "show", self.show, "about the whole show")
        self.assertEqual((got["kind"], got["show"]), ("show", self.show))
        asset = self.store.mint_asset(owner="desk")
        got = self.say("alice", "asset", asset.id, "about a clip")
        self.assertIsNone(got["show"])
        self.assertEqual([c["body"] for c in discuss.list_comments(self.store, "asset", asset.id, "bob")], ["about a clip"])

    def test_a_reply_is_one_level_only(self):
        root = self.say("alice", "item", self.a, "root")
        reply = self.say("bob", "item", self.a, "reply", parent=root["id"])
        self.assertEqual(reply["parent"], root["id"])
        with self.assertRaises(discuss.DiscussError) as ctx:
            self.say("carol", "item", self.a, "reply to reply", parent=reply["id"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "reply_depth"))

    def test_second_level_reply_refused(self):
        self.test_a_reply_is_one_level_only()

    def test_a_reply_must_be_on_the_same_target_and_exist_and_not_be_deleted(self):
        root = self.say("alice", "item", self.a, "root")
        for target in (self.b,):
            with self.assertRaises(discuss.DiscussError) as ctx:
                self.say("bob", "item", target, "elsewhere", parent=root["id"])
            self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(discuss.DiscussError) as ctx:
            self.say("bob", "item", self.a, "orphan", parent="no-such-comment")
        self.assertEqual(ctx.exception.status, 404)
        discuss.delete(self.store, root["id"], "alice", False, now=T0 + 1)
        with self.assertRaises(discuss.DiscussError) as ctx:
            self.say("bob", "item", self.a, "late", parent=root["id"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "parent_deleted"))

    def test_rate_limit_429(self):
        for n in range(30):
            self.say("alice", "item", self.a, f"c{n}", at=T0 + n)
        with self.assertRaises(discuss.DiscussError) as ctx:
            self.say("alice", "item", self.a, "one too many", at=T0 + 31)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (429, "rate_limited"))
        self.say("bob", "item", self.a, "someone else is not limited", at=T0 + 31)       # per author
        self.say("alice", "item", self.a, "a minute after the first", at=T0 + 60)           # the window slid
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM comment WHERE author = 'alice'").fetchone()[0], 31)

    def test_deleting_does_not_give_the_rate_limit_back(self):
        for n in range(30):
            made = self.say("alice", "item", self.a, f"c{n}", at=T0)
            discuss.delete(self.store, made["id"], "alice", False, now=T0)
        with self.assertRaises(discuss.DiscussError) as ctx:
            self.say("alice", "item", self.a, "again", at=T0 + 1)
        self.assertEqual(ctx.exception.status, 429)

    # ---- mentions ------------------------------------------------------------

    def test_parse_mentions_enabled_users_with_view_access_only(self):
        got = discuss.parse_mentions(self.store, "hi @bob and @carol and @gone and @nobody and @vic", self.show)
        self.assertEqual(got, ["bob", "carol", "vic"])                       # `gone` is disabled, `nobody` does not exist
        got = discuss.parse_mentions(self.store, "hi @bob and @carol and @alice and @vic", self.sec)
        self.assertEqual(got, ["carol", "alice"])                            # bob and vic are not on the secret show

    def test_parse_mentions_reads_names_the_way_a_person_writes_them(self):
        self.assertEqual(discuss.parse_mentions(self.store, "@bob, can you look? (@carol) and @alice.", self.show), ["bob", "carol", "alice"])
        self.assertEqual(discuss.parse_mentions(self.store, "mail me at x@bob.com or a@carol", self.show), [])    # inside a word
        self.assertEqual(discuss.parse_mentions(self.store, "@bob @bob @bob", self.show), ["bob"])               # once
        self.assertEqual(discuss.parse_mentions(self.store, "@Bob", self.show), [])                              # names are exact
        self.assertEqual(discuss.parse_mentions(self.store, "no names here", self.show), [])

    def test_parse_mentions_is_capped(self):
        for n in range(30):
            users.add(self.store, f"u{n}", "operator")
        text = " ".join(f"@u{n}" for n in range(30))
        self.assertEqual(len(discuss.parse_mentions(self.store, text, self.show)), discuss.MAX_MENTIONS)

    def test_parse_mentions_for_an_asset_is_every_enabled_user(self):
        self.assertEqual(discuss.parse_mentions(self.store, "@bob @gone @vic", None), ["bob", "vic"])

    def test_mention_notifies_enabled_user_with_access_only(self):
        got = self.say("alice", "item", self.sec_item, "@carol please check, and @bob, @gone, @ghost")
        self.assertEqual(got["mentions"], ["carol"])
        self.assertEqual([(n["kind"], n["actor"], n["target"]) for n in self.inbox("carol")], [("mention", "alice", self.sec_item)])
        for nobody in ("bob", "gone", "vic", "alice"):
            self.assertEqual(self.inbox(nobody), [], nobody)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 1)
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT user_name FROM comment_mention")], ["carol"])

    def test_a_mention_of_yourself_notifies_nobody(self):
        got = self.say("alice", "item", self.a, "note to self @alice")
        self.assertEqual(self.inbox("alice"), [])
        self.assertEqual(got["mentions"], ["alice"])

    def test_reply_notifies_thread_participants_not_author(self):
        root = self.say("alice", "item", self.a, "root")
        self.say("bob", "item", self.a, "first reply", parent=root["id"], at=T0 + 1)
        self.assertEqual([n["kind"] for n in self.inbox("alice")], ["reply"])
        self.assertEqual(self.inbox("bob"), [])                                         # your own reply tells you nothing
        self.say("carol", "item", self.a, "second reply", parent=root["id"], at=T0 + 2)
        self.assertEqual(sorted((n["kind"], n["actor"]) for n in self.inbox("alice")), [("reply", "bob"), ("reply", "carol")])
        self.assertEqual([(n["kind"], n["actor"]) for n in self.inbox("bob")], [("reply", "carol")])    # bob is in the thread now
        self.assertEqual(self.inbox("carol"), [])
        self.assertEqual(self.inbox("vic"), [])

    def test_a_top_level_comment_notifies_nobody_by_itself(self):
        self.say("alice", "item", self.a, "no names")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 0)

    def test_a_person_who_is_mentioned_and_in_the_thread_gets_one_notification(self):
        root = self.say("alice", "item", self.a, "root")
        self.say("bob", "item", self.a, "hey @alice", parent=root["id"], at=T0 + 1)
        self.assertEqual([n["kind"] for n in self.inbox("alice")], ["mention"])

    def test_thread_participants_who_lost_access_or_are_disabled_are_not_notified(self):
        root = self.say("alice", "item", self.sec_item, "root")
        self.say("carol", "item", self.sec_item, "i can see this", parent=root["id"], at=T0 + 1)
        access.set_acl(self.store, self.sec, "user", "carol", None)                     # carol is no longer on the show
        self.say("alice", "item", self.sec_item, "again", parent=root["id"], at=T0 + 2)
        self.assertEqual(self.inbox("carol"), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification WHERE user_name = 'carol'").fetchone()[0], 0)

    def test_notifications_hidden_when_access_is_lost_later_and_back_when_regained(self):
        self.say("alice", "item", self.sec_item, "@carol look")
        self.assertEqual(len(self.inbox("carol")), 1)
        access.set_acl(self.store, self.sec, "user", "carol", None)
        self.assertEqual(self.inbox("carol"), [])                                       # the row is there, the read filters it
        self.assertEqual(discuss.unread_count(self.store, "carol"), 0)
        access.set_acl(self.store, self.sec, "user", "carol", "view")
        self.assertEqual(len(self.inbox("carol")), 1)

    def test_a_disabled_user_has_no_notifications(self):
        self.say("alice", "item", self.a, "@bob look")
        users.disable(self.store, "bob")
        self.assertEqual(self.inbox("bob"), [])
        self.assertEqual(discuss.unread_count(self.store, "bob"), 0)

    def test_notify_itself_refuses_a_user_without_access(self):
        self.assertIsNone(discuss.notify(self.store, "bob", "mention", show_id=self.sec, target_kind="item", target_id=self.sec_item,
                                         actor="alice", now=T0))
        self.assertIsNone(discuss.notify(self.store, "ghost", "mention", show_id=self.show, target_kind="item", target_id=self.a,
                                         actor="alice", now=T0))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 0)
        with self.assertRaises(discuss.DiscussError):
            discuss.notify(self.store, "bob", "banana", show_id=self.show, target_kind="item", target_id=self.a, actor="alice", now=T0)

    # ---- listing ------------------------------------------------------------

    def test_list_comments_orders_by_time_and_flags_mine(self):
        first = self.say("alice", "item", self.a, "one", at=T0)
        self.say("bob", "item", self.a, "two", parent=first["id"], at=T0 + 1)
        self.say("bob", "item", self.b, "elsewhere", at=T0 + 2)
        got = discuss.list_comments(self.store, "item", self.a, "bob")
        self.assertEqual([(c["body"], c["mine"], c["parent"]) for c in got], [("one", False, None), ("two", True, first["id"])])

    def test_deleted_comment_body_hidden_in_list(self):
        made = self.say("alice", "item", self.a, SECRET + " @bob")
        discuss.delete(self.store, made["id"], "alice", False, now=T0 + 1)
        got = discuss.list_comments(self.store, "item", self.a, "bob")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["deleted"])
        self.assertIsNone(got[0]["body"])
        self.assertEqual(got[0]["mentions"], [])
        self.assertNotIn(SECRET, json.dumps(got))
        stored = self.store.conn.execute("SELECT body FROM comment WHERE id = ?", (made["id"],)).fetchone()["body"]
        self.assertNotIn(SECRET, stored)                                                # not kept at rest either
        self.assertEqual(self.inbox("bob"), [])                                         # and the ping for it is gone

    def test_resolve_and_reopen_a_thread(self):
        root = self.say("alice", "item", self.a, "root")
        reply = self.say("bob", "item", self.a, "reply", parent=root["id"], at=T0 + 1)
        got = discuss.resolve(self.store, root["id"], "carol", True, now=T0 + 5)
        self.assertEqual((got["resolved"], got["resolvedBy"]), (True, "carol"))
        self.assertTrue(got["resolvedAt"].endswith("Z"))
        again = discuss.resolve(self.store, root["id"], "carol", False, now=T0 + 6)
        self.assertEqual((again["resolved"], again["resolvedBy"], again["resolvedAt"]), (False, None, None))
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.resolve(self.store, reply["id"], "carol", True, now=T0 + 7)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "not_a_thread"))
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.resolve(self.store, "nope", "carol", True, now=T0 + 7)
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.resolve(self.store, root["id"], "carol", "yes", now=T0 + 7)
        self.assertEqual(ctx.exception.status, 422)

    def test_a_deleted_thread_cannot_be_resolved(self):
        root = self.say("alice", "item", self.a, "root")
        discuss.delete(self.store, root["id"], "alice", False, now=T0 + 1)
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.resolve(self.store, root["id"], "carol", True, now=T0 + 2)
        self.assertEqual(ctx.exception.status, 409)

    def test_delete_needs_author_or_admin(self):
        made = self.say("alice", "item", self.a, "mine")
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.delete(self.store, made["id"], "bob", False, now=T0 + 1)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "forbidden_role"))
        discuss.delete(self.store, made["id"], "root", True, now=T0 + 1)
        again = discuss.delete(self.store, made["id"], "alice", False, now=T0 + 2)      # idempotent for the author
        self.assertTrue(again["deleted"])
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.delete(self.store, "nope", "root", True, now=T0)
        self.assertEqual(ctx.exception.status, 404)

    def test_open_thread_counts_by_item(self):
        r1 = self.say("alice", "item", self.a, "open one")
        self.say("bob", "item", self.a, "a reply is not a thread", parent=r1["id"])
        r2 = self.say("alice", "item", self.a, "to be resolved")
        self.say("alice", "item", self.b, "open on b")
        r4 = self.say("alice", "item", self.c, "deleted")
        discuss.resolve(self.store, r2["id"], "bob", True, now=T0 + 1)
        discuss.delete(self.store, r4["id"], "alice", False, now=T0 + 1)
        self.assertEqual(discuss.open_counts(self.store, self.show), {self.a: 1, self.b: 1})

    # ---- notifications ---------------------------------------------------------

    def test_notifications_are_private_to_owner(self):
        self.say("alice", "item", self.a, "@bob look")
        self.say("alice", "item", self.a, "@carol look")
        self.assertEqual([n["actor"] for n in self.inbox("bob")], ["alice"])
        self.assertEqual(len(self.inbox("carol")), 1)
        self.assertEqual(self.inbox("alice"), [])
        self.assertEqual(self.inbox("bob")[0]["id"] in [n["id"] for n in self.inbox("carol")], False)

    def test_mark_read_only_own_ids(self):
        self.say("alice", "item", self.a, "@bob look")
        self.say("alice", "item", self.a, "@carol look")
        bob_id, carol_id = self.inbox("bob")[0]["id"], self.inbox("carol")[0]["id"]
        self.assertEqual(discuss.mark_read(self.store, "bob", [carol_id], now=T0 + 5), 0)         # not bob's
        self.assertEqual(discuss.unread_count(self.store, "carol"), 1)
        self.assertEqual(discuss.mark_read(self.store, "bob", [bob_id, carol_id, "nope"], now=T0 + 5), 1)
        self.assertEqual(discuss.unread_count(self.store, "bob"), 0)
        self.assertEqual(discuss.unread_count(self.store, "carol"), 1)
        self.assertTrue(self.inbox("bob")[0]["read"])
        self.assertEqual(self.inbox("bob", unread_only=True), [])
        self.assertEqual(discuss.mark_read(self.store, "bob", [bob_id], now=T0 + 9), 0)           # already read: unchanged

    def test_mark_all_read(self):
        self.say("alice", "item", self.a, "@bob 1")
        self.say("carol", "item", self.a, "@bob 2")
        self.assertEqual(discuss.unread_count(self.store, "bob"), 2)
        self.assertEqual(discuss.mark_read(self.store, "bob", None, now=T0 + 5), 2)
        self.assertEqual(discuss.unread_count(self.store, "bob"), 0)

    def test_the_inbox_is_newest_first_and_bounded(self):
        for n in range(5):
            self.say("alice", "item", self.a, f"@bob {n}", at=T0 + n * 10)
        got = self.inbox("bob")
        self.assertEqual([n["createdAt"] for n in got], sorted((n["createdAt"] for n in got), reverse=True))
        self.assertEqual(len(discuss.notifications(self.store, "bob", limit=2)), 2)

    def test_a_notification_carries_ids_and_a_label_but_never_a_body(self):
        self.say("alice", "item", self.a, SECRET + " @bob")
        got = self.inbox("bob")[0]
        self.assertEqual((got["show"], got["targetKind"], got["target"], got["label"]), (self.show, "item", self.a, "story 0"))
        self.assertNotIn(SECRET, json.dumps(got))
        self.assertNotIn(SECRET, json.dumps([dict(r) for r in self.store.conn.execute("SELECT * FROM notification")]))

    def test_old_notifications_purged_after_90_days(self):
        self.say("alice", "item", self.a, "@bob old", at=T0)
        self.say("alice", "item", self.a, "@bob newer", at=T0 + 89 * 86400)
        gone = discuss.purge_notifications(self.store, now=T0 + 91 * 86400)
        self.assertEqual(gone, 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0], 2)        # comments are kept
        self.assertEqual(discuss.purge_notifications(self.store, now=T0 + 91 * 86400), 0)
        self.assertEqual(discuss.purge_notifications(self.store, now=T0 + 89 * 86400 + 90 * 86400), 1)

    # ---- the lease hook ------------------------------------------------------------

    def test_serve_wires_the_lease_taken_hook(self):
        self.assertIs(leases.ON_TAKEN, discuss.on_lease_taken)

    def test_a_takeover_notifies_the_previous_holder_only(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        leases.takeover(self.store, "item", self.a, "bob", "tabB", T0 + 5)
        got = self.inbox("alice")
        self.assertEqual([(n["kind"], n["actor"], n["target"], n["show"]) for n in got], [("lease_taken", "bob", self.a, self.show)])
        self.assertEqual(self.inbox("bob"), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 1)

    def test_a_takeover_does_not_notify_someone_who_cannot_view_the_show(self):
        leases.acquire(self.store, "item", self.sec_item, "alice", "tab1", T0)
        access.set_acl(self.store, self.sec, "user", "alice", None)
        access.set_acl(self.store, self.sec, "user", "bob", "edit")
        leases.takeover(self.store, "item", self.sec_item, "bob", "tabB", T0 + 5)
        self.assertEqual(self.inbox("alice"), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0], 0)

    # ---- things that go with their subject -------------------------------------------

    def _populate(self, kind, target, show):
        root = self.say("alice", kind, target, "@bob root")
        self.say("carol", kind, target, "@bob reply", parent=root["id"], at=T0 + 1)

    def _rows(self):
        return [self.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("comment", "comment_mention", "notification")]

    def test_removing_an_item_removes_its_comments_mentions_and_notifications(self):
        self._populate("item", self.b, self.show)
        self.say("alice", "item", self.a, "stays @bob")
        before = self._rows()
        building.remove_item(self.store, self.b)
        after = self._rows()
        self.assertEqual(after, [before[0] - 2, before[1] - 2, before[2] - 3])    # the reply also pinged the root's author
        self.assertEqual(discuss.list_comments(self.store, "item", self.a, "bob")[0]["body"], "stays @bob")

    def test_deleting_a_show_removes_its_comments_and_those_of_its_items(self):
        self._populate("item", self.a, self.show)
        self._populate("show", self.show, self.show)
        other = building.create_show(self.store, "Other")[0]
        keep = building.add_item(self.store, other, "keeper")[0]
        self.say("alice", "item", keep, "keep me @bob")
        building.delete_show(self.store, self.show)
        self.assertEqual(self._rows(), [1, 1, 1])
        self.assertEqual(self.store.conn.execute("SELECT target_id FROM comment").fetchone()[0], keep)

    def test_purging_an_asset_removes_its_comments_and_notifications(self):
        asset = self.store.mint_asset(owner="desk")
        other = self.store.mint_asset(owner="desk")
        self._populate("asset", asset.id, None)
        self.say("alice", "asset", other.id, "keep @bob")
        retention.tombstone(self.store, asset.id)
        result = retention.purge_asset(self.store, asset.id, retention.OwnedRoots())
        self.assertEqual(result.status, "purged", result.reasons if hasattr(result, "reasons") else result)
        self.assertEqual(self._rows(), [1, 1, 1])

    def test_with_the_tables_gone_building_and_playout_still_work(self):
        for table in ("notification", "comment_mention", "comment"):
            self.store.conn.execute(f"DROP TABLE {table}")
        self.store.conn.commit()
        self.assertFalse(discuss.available(self.store))
        building.remove_item(self.store, self.c)                                         # no comment table is no reason to refuse
        building.rename_item(self.store, self.a, "renamed", None)
        building.delete_show(self.store, self.sec)

    def test_available_when_built(self):
        self.assertTrue(discuss.available(self.store))


class Http(Base, Api):
    def setUp(self):
        Api.setUp(self)
        self.now = T0
        patcher = mock.patch.object(serve, "CLOCK", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.build(self.store)
        self.t = self.tokens

    def comment(self, token, kind, target, body, **extra):
        return self.post("/api/comments", {"kind": kind, "target": target, "body": body, **extra}, token=token)

    def listing(self, token, kind, target):
        return self.call("GET", f"/api/comments?kind={kind}&target={target}", token=token)

    def audit_rows(self):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM audit ORDER BY at, id")]

    # ---- access ---------------------------------------------------------------------

    def test_comment_requires_view_access_and_is_audited_without_body(self):
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        s, out = self.comment(self.t["bob"], "item", self.sec_item, SECRET)
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0], 0)
        s, out = self.comment(self.t["alice"], "item", self.sec_item, SECRET + " @carol")
        self.assertEqual(s, 200, out)
        self.assertEqual((out["comment"]["body"], out["comment"]["mentions"]), (SECRET + " @carol", ["carol"]))
        rows = [r for r in self.audit_rows() if r["action"] == "comment_add"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor"], rows[0]["target_kind"], rows[0]["target_id"]), ("alice", "comment", out["comment"]["id"]))
        detail = json.loads(rows[0]["detail"])
        self.assertEqual((detail["kind"], detail["target"], detail["show"]), ("item", self.sec_item, self.sec))
        self.assertNotIn(SECRET, json.dumps(self.audit_rows()))                          # no body anywhere in the audit table
        self.assertNotIn(SECRET, logs.getvalue())                                        # nor in the log

    def test_a_viewer_may_comment_and_read_but_not_resolve(self):
        s, out = self.comment(self.t["vic"], "item", self.a, "from the gallery")
        self.assertEqual(s, 200, out)
        cid = out["comment"]["id"]
        self.assertEqual(self.listing(self.t["vic"], "item", self.a)[0], 200)
        self.refused(403, "forbidden_role", f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["vic"])
        s, out = self.post(f"/api/comments/{cid}/delete", {}, token=self.t["vic"])          # their own, so they may
        self.assertEqual(s, 200, out)

    def test_a_viewer_with_no_access_to_the_show_cannot_comment_or_read(self):
        s, out, _ = self.listing(self.t["vic"], "item", self.sec_item)
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))
        s, out = self.comment(self.t["vic"], "show", self.sec, "hello")
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))

    def test_nobody_without_access_reads_a_comment_notifies_or_resolves_it(self):
        s, made = self.comment(self.t["alice"], "item", self.sec_item, SECRET + " @bob @carol")
        self.assertEqual(s, 200, made)
        cid = made["comment"]["id"]
        self.assertEqual(made["comment"]["mentions"], ["carol"])
        for who in ("bob", "vic"):
            s, out, _ = self.listing(self.t[who], "item", self.sec_item)
            self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"), who)
            self.assertNotIn(SECRET, json.dumps(out))
            self.refused(403, "forbidden_show", f"/api/comments/{cid}/delete", {}, token=self.t[who])
        self.refused(403, "forbidden_show", f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["bob"])
        for who in ("bob", "vic", "root"):
            s, out, _ = self.call("GET", "/api/notifications", token=self.t[who])
            self.assertEqual((s, out["notifications"]), (200, []), who)
        s, out, _ = self.call("GET", "/api/notifications", token=self.t["carol"])
        self.assertEqual(len(out["notifications"]), 1)
        self.assertNotIn(SECRET, json.dumps(out))

    def test_view_cannot_resolve_but_edit_can_on_a_restricted_show(self):
        cid = self.comment(self.t["carol"], "item", self.sec_item, "i can only look")[1]["comment"]["id"]
        self.refused(403, "forbidden_show", f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["carol"])
        s, out = self.post(f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["alice"])
        self.assertEqual((s, out["comment"]["resolved"]), (200, True))

    def test_resolve_needs_edit_and_delete_needs_author_or_admin(self):
        cid = self.comment(self.t["alice"], "item", self.a, "mine")[1]["comment"]["id"]
        self.refused(403, "forbidden_role", f"/api/comments/{cid}/delete", {}, token=self.t["bob"])
        s, out = self.post(f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["bob"])
        self.assertEqual((s, out["comment"]["resolvedBy"]), (200, "bob"))
        s, out = self.post(f"/api/comments/{cid}/delete", {}, token=self.t["root"])
        self.assertEqual((s, out["comment"]["deleted"]), (200, True))
        self.refused(404, "not_found", "/api/comments/nope/delete", {}, token=self.t["root"])
        self.refused(404, "not_found", "/api/comments/nope/resolve", {"resolved": True}, token=self.t["root"])

    def test_the_desk_token_comments_as_desk(self):
        serve.TOKEN = "d" * 32
        s, out = self.comment("d" * 32, "item", self.sec_item, "from the desk")
        self.assertEqual((s, out["comment"]["author"]), (200, "desk"))

    def test_asset_comments_are_admin_only_until_the_owner_decides(self):
        asset = self.store.mint_asset(owner="desk")
        for who in ("alice", "vic"):
            self.refused(403, "forbidden_role", "/api/comments", {"kind": "asset", "target": asset.id, "body": "x"}, token=self.t[who])
            s, out, _ = self.listing(self.t[who], "asset", asset.id)
            self.assertEqual((s, out["error"]["code"]), (403, "forbidden_role"), who)
        s, made = self.comment(self.t["root"], "asset", asset.id, "admin note")
        self.assertEqual(s, 200, made)
        cid = made["comment"]["id"]
        s, out, _ = self.listing(self.t["root"], "asset", asset.id)
        self.assertEqual([c["body"] for c in out["comments"]], ["admin note"])
        self.refused(403, "forbidden_role", f"/api/comments/{cid}/resolve", {"resolved": True}, token=self.t["alice"])
        self.refused(403, "forbidden_role", f"/api/comments/{cid}/delete", {}, token=self.t["vic"])
        self.assertEqual(self.post(f"/api/comments/{cid}/delete", {}, token=self.t["root"])[0], 200)

    def test_a_viewer_gets_no_roster_but_an_operator_does(self):
        s, out, _ = self.listing(self.t["vic"], "item", self.a)
        self.assertEqual((s, out["people"]), (200, []))
        s, out, _ = self.listing(self.t["alice"], "item", self.a)
        self.assertIn("bob", [p["name"] for p in out["people"]])

    # ---- validation ----------------------------------------------------------------------

    def test_the_body_is_validated(self):
        t = self.t["alice"]
        self.refused(422, "bad_input", "/api/comments", {"kind": "item", "target": self.a}, token=t)
        self.refused(422, "bad_input", "/api/comments", {"kind": "item", "target": self.a, "body": "  "}, token=t)
        self.refused(422, "bad_input", "/api/comments", {"kind": "item", "target": self.a, "body": "x" * 4001}, token=t)
        self.refused(422, "bad_input", "/api/comments", {"kind": "item", "target": self.a, "body": "ok", "colour": "red"}, token=t)
        self.refused(422, "bad_input", "/api/comments", {"kind": "cue", "target": self.a, "body": "ok"}, token=t)
        self.refused(422, "bad_input", "/api/comments", {"kind": "item", "target": self.a, "body": "ok", "parent": 5}, token=t)
        self.refused(404, "not_found", "/api/comments", {"kind": "item", "target": "nope", "body": "ok"}, token=t)
        self.refused(404, "not_found", "/api/comments", {"kind": "item", "target": self.a, "body": "ok", "parent": "nope"}, token=t)
        self.refused(422, "bad_input", f"/api/comments/x/resolve", {"resolved": "yes"}, token=t)
        self.refused(422, "bad_input", f"/api/comments/x/resolve", {}, token=t)

    def test_second_level_reply_refused_over_http(self):
        t = self.t["alice"]
        root = self.comment(t, "item", self.a, "root")[1]["comment"]["id"]
        reply = self.comment(t, "item", self.a, "reply", parent=root)[1]["comment"]["id"]
        self.refused(422, "reply_depth", "/api/comments", {"kind": "item", "target": self.a, "body": "deeper", "parent": reply}, token=t)

    def test_rate_limit_429_over_http(self):
        for n in range(30):
            self.assertEqual(self.comment(self.t["alice"], "item", self.a, f"c{n}")[0], 200)
        self.refused(429, "rate_limited", "/api/comments", {"kind": "item", "target": self.a, "body": "more"}, token=self.t["alice"])
        self.now += 61
        self.assertEqual(self.comment(self.t["alice"], "item", self.a, "later")[0], 200)

    def test_get_validation(self):
        t = self.t["alice"]
        for query in ("", "?kind=item", "?target=x", "?kind=cue&target=x", f"?kind=item&target={self.a}&extra=1",
                      f"?kind=item&kind=show&target={self.a}"):
            s, out, _ = self.call("GET", "/api/comments" + query, token=t)
            self.assertEqual((s, out["error"]["code"]), (422, "bad_input"), query)
        s, out, _ = self.call("GET", "/api/comments?kind=item&target=nope", token=t)
        self.assertEqual((s, out["error"]["code"]), (404, "not_found"))
        s, out, _ = self.call("GET", "/api/notifications?extra=1", token=t)
        self.assertEqual(s, 422)

    def test_the_listing_carries_the_roster_for_the_mention_picker_limited_to_people_who_can_see(self):
        self.comment(self.t["alice"], "item", self.sec_item, "hello")
        s, out, _ = self.listing(self.t["alice"], "item", self.sec_item)
        self.assertEqual([p["name"] for p in out["people"]], ["alice", "carol", "root"])      # admins can always see; bob and vic cannot
        s, out, _ = self.listing(self.t["alice"], "item", self.a)
        self.assertEqual([p["name"] for p in out["people"]], ["alice", "bob", "carol", "root", "vic"])    # not the disabled one

    # ---- notifications over http ---------------------------------------------------------

    def test_mention_and_reply_notifications_over_http(self):
        root = self.comment(self.t["alice"], "item", self.a, "@bob take a look")[1]["comment"]["id"]
        s, out, _ = self.call("GET", "/api/notifications", token=self.t["bob"])
        self.assertEqual(s, 200)
        self.assertEqual((out["unread"], [(n["kind"], n["actor"], n["read"]) for n in out["notifications"]]), (1, [("mention", "alice", False)]))
        self.comment(self.t["bob"], "item", self.a, "done", parent=root)
        s, out, _ = self.call("GET", "/api/notifications?unread=1", token=self.t["alice"])
        self.assertEqual([n["kind"] for n in out["notifications"]], ["reply"])

    def test_notifications_are_private_to_owner_over_http(self):
        self.comment(self.t["alice"], "item", self.a, "@bob take a look")
        s, out, _ = self.call("GET", "/api/notifications", token=self.t["carol"])
        self.assertEqual(out["notifications"], [])
        nid = self.call("GET", "/api/notifications", token=self.t["bob"])[1]["notifications"][0]["id"]
        s, out = self.post("/api/notifications/read", {"ids": [nid]}, token=self.t["carol"])
        self.assertEqual((s, out["marked"]), (200, 0))
        self.assertEqual(self.call("GET", "/api/notifications", token=self.t["bob"])[1]["unread"], 1)
        s, out = self.post("/api/notifications/read", {"ids": [nid]}, token=self.t["bob"])
        self.assertEqual((s, out["marked"], out["unread"]), (200, 1, 0))

    def test_mark_read_body_is_validated_and_all_marks_every_one(self):
        t = self.t["bob"]
        self.comment(self.t["alice"], "item", self.a, "@bob one")
        self.comment(self.t["carol"], "item", self.a, "@bob two")
        for bad in ({}, {"ids": "x"}, {"ids": [5]}, {"ids": ["a"] * 201}, {"all": "yes"}, {"ids": ["a"], "all": True}, {"all": True, "x": 1}):
            self.refused(422, "bad_input", "/api/notifications/read", bad, token=t)
        s, out = self.post("/api/notifications/read", {"all": True}, token=t)
        self.assertEqual((s, out["marked"], out["unread"]), (200, 2, 0))

    def test_unread_count_in_state(self):
        self.comment(self.t["alice"], "item", self.a, "@bob look")
        s, out, _ = self.call("GET", f"/api/state?show={self.show}", token=self.t["bob"])
        self.assertEqual(out["notifications"], {"unread": 1})
        self.assertEqual(self.call("GET", f"/api/state?show={self.show}", token=self.t["carol"])[1]["notifications"], {"unread": 0})
        self.post("/api/notifications/read", {"all": True}, token=self.t["bob"])
        self.assertEqual(self.call("GET", f"/api/state?show={self.show}", token=self.t["bob"])[1]["notifications"], {"unread": 0})

    def test_open_thread_counts_in_state_are_for_the_shown_show(self):
        self.comment(self.t["alice"], "item", self.a, "open")
        self.comment(self.t["alice"], "item", self.sec_item, "elsewhere")
        s, out, _ = self.call("GET", f"/api/state?show={self.show}", token=self.t["bob"])
        self.assertEqual(out["commentCounts"], {self.a: 1})
        self.assertEqual(out["collab"], {"available": True})
        s, out, _ = self.call("GET", f"/api/state?show={self.sec}", token=self.t["bob"])        # bob cannot see it: not its counts
        self.assertNotIn(self.sec_item, out["commentCounts"])

    def test_the_actor_in_the_state_for_the_desk_has_nothing_unread(self):
        serve.TOKEN = "d" * 32
        self.comment(self.t["alice"], "item", self.a, "@bob look")
        s, out, _ = self.call("GET", f"/api/state?show={self.show}", token="d" * 32)
        self.assertEqual(out["notifications"], {"unread": 0})

    def test_lease_takeover_over_http_notifies_the_previous_holder(self):
        self.post(f"/api/items/{self.a}/lease", {"clientId": "tab1"}, token=self.t["alice"])
        self.post(f"/api/items/{self.a}/lease/takeover", {"clientId": "tabB"}, token=self.t["bob"])
        s, out, _ = self.call("GET", "/api/notifications", token=self.t["alice"])
        self.assertEqual([(n["kind"], n["actor"]) for n in out["notifications"]], [("lease_taken", "bob")])

    # ---- the audit and the registry ----------------------------------------------------

    def test_every_write_is_audited_with_ids_only(self):
        made = self.comment(self.t["alice"], "item", self.a, SECRET + " @bob")[1]["comment"]["id"]
        self.post(f"/api/comments/{made}/resolve", {"resolved": True}, token=self.t["bob"])
        self.post(f"/api/comments/{made}/delete", {}, token=self.t["alice"])
        self.post("/api/notifications/read", {"all": True}, token=self.t["bob"])
        got = [(r["action"], r["target_kind"], r["actor"]) for r in self.audit_rows()]
        self.assertEqual(got, [("comment_add", "comment", "alice"), ("comment_resolve", "comment", "bob"),
                               ("comment_delete", "comment", "alice"), ("notifications_read", "notification", "bob")])
        self.assertEqual({r["target_id"] for r in self.audit_rows()[:3]}, {made})
        self.assertNotIn(SECRET, json.dumps(self.audit_rows()))

    def test_a_refused_write_is_not_audited(self):
        self.comment(self.t["bob"], "item", self.sec_item, "no")
        self.comment(self.t["alice"], "item", self.a, "")
        self.assertEqual(self.audit_rows(), [])

    def test_the_new_routes_are_classified(self):
        want = {"act_comment_add": ("viewer", None), "act_comment_resolve": ("operator", "edit"), "act_comment_delete": ("viewer", "view"),
                "act_notifications_read": ("viewer", None)}
        names = {fn.__name__ for _, fn in serve.ROUTES}
        for name, (role, level) in want.items():
            self.assertIn(name, names)
            self.assertEqual(serve.ROUTE_ROLES[name], role, name)
            self.assertEqual(serve.ROUTE_SHOW_LEVEL.get(name), level, name)
            self.assertEqual(name in serve.NOT_SHOW_SCOPED, level is None, name)
        self.assertEqual(serve.SHOW_RESOLVER["act_comment_resolve"], "comment")
        self.assertEqual((serve._AUDIT_KIND["comments"], serve._AUDIT_KIND["notifications"]), ("comment", "notification"))

    def test_the_comment_resolver_finds_the_show_of_a_comment(self):
        made = self.comment(self.t["alice"], "item", self.sec_item, "x")[1]["comment"]["id"]
        self.assertEqual(access.show_of(self.store, "comment", made), self.sec)
        asset = self.store.mint_asset(owner="desk")
        aid = self.comment(self.t["root"], "asset", asset.id, "clip")[1]["comment"]["id"]
        self.assertIsNone(access.show_of(self.store, "comment", aid))
        self.assertIsNone(access.show_of(self.store, "comment", "missing"))

    # ---- never in the way of the show ----------------------------------------------------

    def broken(self):
        """Every comment read and write fails as it would with the tables dropped or locked. (A fresh connection recreates a dropped table
        through the schema bootstrap, so the failure is injected where the tables are used.)"""
        err = sqlite3.OperationalError("no such table: comment")
        stack = contextlib.ExitStack()
        for name in ("available", "show_of", "add_comment", "resolve", "delete", "list_comments", "roster", "notifications", "unread_count",
                     "open_counts", "mark_read", "notify"):
            stack.enter_context(mock.patch.object(discuss, name, side_effect=err))
        self.addCleanup(stack.close)
        return stack

    def test_on_air_routes_work_with_the_comment_tables_dropped(self):
        with self.broken():
            s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.t["alice"])
            self.assertEqual(s, 200, out)
            self.assertEqual(self.post(f"/api/shows/{self.show}/release", {}, token=self.t["alice"])[0], 200)
            self.assertEqual(self.post(f"/api/items/{self.c}/kill", {}, token=self.t["alice"])[0], 200)
            self.assertEqual(self.post(f"/api/items/{self.b}/skip", {}, token=self.t["alice"])[0], 200)
            s, out = self.post(f"/api/shows/{self.show}/go", {}, token=self.t["alice"])      # no media here, so go refuses for its own reason
            self.assertNotEqual(out["error"]["code"], "collab_unavailable")
            s, out, _ = self.call("GET", f"/api/state?show={self.show}", token=self.t["alice"])
            self.assertEqual(s, 200)
            self.assertEqual((out["collab"], out["notifications"], out["commentCounts"]), ({"available": False}, {"unread": 0}, {}))
            self.refused(503, "collab_unavailable", "/api/comments", {"kind": "item", "target": self.a, "body": "hello"}, token=self.t["alice"])
            self.refused(503, "collab_unavailable", "/api/notifications/read", {"all": True}, token=self.t["alice"])
            self.refused(503, "collab_unavailable", "/api/comments/x/resolve", {"resolved": True}, token=self.t["alice"])
            self.refused(503, "collab_unavailable", "/api/comments/x/delete", {}, token=self.t["alice"])
            self.assertEqual(self.call("GET", f"/api/comments?kind=item&target={self.a}", token=self.t["alice"])[0], 503)
            self.assertEqual(self.call("GET", "/api/notifications", token=self.t["alice"])[0], 503)

    def test_a_takeover_still_works_when_notifications_cannot_be_written(self):
        self.post(f"/api/items/{self.a}/lease", {"clientId": "tab1"}, token=self.t["alice"])
        with self.broken():
            s, out = self.post(f"/api/items/{self.a}/lease/takeover", {"clientId": "tabB"}, token=self.t["bob"])
        self.assertEqual((s, out["lease"]["holder"]), (200, "bob"))


class Runner(Base, unittest.TestCase):
    def test_the_runners_notification_task_purges_old_ones_and_survives_a_missing_table(self):
        from volt import runner

        self.build(Store())
        self.say("alice", "item", self.a, "@bob old", at=T0)
        task = runner.notifications_task(clock=lambda: T0 + 91 * 86400)
        self.assertEqual(task(self.store), {"purged": 1})
        self.store.conn.execute("DROP TABLE notification")
        self.store.conn.commit()
        self.assertEqual(task(self.store), {"purged": 0})


class Migration(unittest.TestCase):
    NAME = "comments and notifications"

    def migration(self):
        return next(m for m in migrations.MIGRATIONS if m.name == self.NAME)

    def test_the_registry_names_it_and_every_statement_is_idempotent(self):
        m = self.migration()
        self.assertGreater(m.version, 33)
        for stmts in (m.sqlite, m.postgres):
            self.assertTrue(all("IF NOT EXISTS" in s for s in stmts))

    def test_this_migration_is_not_assumed_to_be_the_latest(self):
        self.assertLessEqual(self.migration().version, migrations.LATEST)

    def test_a_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = self.migration()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            show = building.create_show(store, "S")[0]
            item = building.add_item(store, show, "one")[0]
            for table in ("notification", "comment_mention", "comment"):
                store.conn.execute(f"DROP TABLE {table}")
            store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            store.conn.commit()
            store.close()
            store = Store(path)
            try:
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(comment)")],
                                 ["id", "target_kind", "target_id", "show_id", "author", "body", "parent_id", "created_at", "resolved_at",
                                  "resolved_by", "deleted_at"])
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(comment_mention)")], ["comment_id", "user_name"])
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(notification)")],
                                 ["id", "user_name", "kind", "show_id", "target_kind", "target_id", "comment_id", "actor", "created_at", "read_at"])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 1)
                discuss.add_comment(store, "alice", "item", item, "works", now=T0)
                self.assertEqual(len(discuss.list_comments(store, "item", item, "alice")), 1)
            finally:
                store.close()

    def test_a_fresh_database_has_the_same_tables_as_a_migrated_one(self):
        store = Store()
        names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"comment", "comment_mention", "notification"} <= names)
        idx = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"comment_by_target", "notification_by_user"} <= idx)


class MigrationPostgres(PgCase):
    def test_a_postgres_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = Migration().migration()
        store = self.store()
        users.add(store, "alice", "operator")
        show = building.create_show(store, "S")[0]
        item = building.add_item(store, show, "one")[0]
        for table in ("notification", "comment_mention", "comment"):
            store.conn.execute(f"DROP TABLE {table}")
        store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        for table, want in (("comment", ["id", "target_kind", "target_id", "show_id", "author", "body", "parent_id", "created_at",
                                         "resolved_at", "resolved_by", "deleted_at"]),
                            ("comment_mention", ["comment_id", "user_name"]),
                            ("notification", ["id", "user_name", "kind", "show_id", "target_kind", "target_id", "comment_id", "actor",
                                              "created_at", "read_at"])):
            rows = again.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ? "
                "ORDER BY ordinal_position", (table,)).fetchall()
            self.assertEqual([r[0] for r in rows], want)
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 1)
        made = discuss.add_comment(again, "alice", "item", item, "works @alice", now=T0)
        self.assertEqual(len(discuss.list_comments(again, "item", item, "alice")), 1)
        discuss.add_comment(again, "alice", "item", item, "reply", made["id"], now=T0 + 1)
        self.assertEqual(discuss.open_counts(again, show), {item: 1})

    def test_the_rate_limit_and_the_purge_on_postgres(self):
        store = self.store()
        users.add(store, "alice", "operator")
        users.add(store, "bob", "operator")
        show = building.create_show(store, "S")[0]
        item = building.add_item(store, show, "one")[0]
        for n in range(30):
            discuss.add_comment(store, "alice", "item", item, f"c{n}", now=T0 + n)
        with self.assertRaises(discuss.DiscussError) as ctx:
            discuss.add_comment(store, "alice", "item", item, "more", now=T0 + 31)
        self.assertEqual(ctx.exception.status, 429)
        discuss.add_comment(store, "bob", "item", item, "@alice hello", now=T0 + 40)
        self.assertEqual(discuss.unread_count(store, "alice"), 1)
        self.assertEqual(discuss.purge_notifications(store, now=T0 + 91 * 86400), 1)
        self.assertEqual(discuss.unread_count(store, "alice"), 0)


if __name__ == "__main__":
    unittest.main()
