"""Edit leases, versioned rename and presence (identity plan, task 7).

A lease is advisory UX backed by a server check; the versioned write holds even when no lease is used. Time is injected (a frozen clock);
nothing sleeps. The on-air path (take, go, hold, release, skip, kill) never reads these tables, and the tests drop them to prove it.
"""
import contextlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.pgutil import DSN, PgCase
from tests.test_operator_api import Api
from tests.test_take import _show
from volt import building, leases, migrations, serve, users
from volt.store import Store

T0 = 1_800_000_000.0


def mkitems(store, n=3):
    show, _ = building.create_show(store, "Leases")
    return show, [building.add_item(store, show, f"story {i}", f"text {i}")[0] for i in range(n)]


class Unit(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.show, (self.a, self.b, self.c) = mkitems(self.store)

    def test_the_constants_are_the_specs(self):
        self.assertEqual((leases.TTL, leases.PRESENCE_LIVE, leases.PRESENCE_PRUNE), (45, 30, 300))

    def test_free_item_acquires_and_renew_extends(self):
        got = leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        self.assertEqual((got.holder, got.client_id, got.expires_at), ("alice", "tab1", T0 + 45))
        self.assertTrue(got.fresh)
        again = leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 15, renew=True)
        self.assertEqual(again.expires_at, T0 + 60)
        self.assertEqual(again.acquired_at, T0)             # a renew keeps when it was first taken
        self.assertFalse(again.fresh)

    def test_expiry_on_frozen_clock_frees_it(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "bob", "tabB", T0 + 44.9)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "leased"))
        got = leases.acquire(self.store, "item", self.a, "bob", "tabB", T0 + 45)       # the instant it ends
        self.assertEqual(got.holder, "bob")
        self.assertEqual(got.acquired_at, T0 + 45)

    def test_a_lapsed_own_lease_taken_again_is_a_fresh_acquire(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        got = leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 100)
        self.assertTrue(got.fresh)
        self.assertEqual(got.acquired_at, T0 + 100)

    def test_other_holder_gets_409_leased_with_holder(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "bob", "tabB", T0 + 10)
        err = ctx.exception
        self.assertEqual((err.status, err.code), (409, "leased"))
        self.assertEqual(err.extra["holder"], "alice")
        self.assertEqual(err.extra["expiresAt"], "2027-01-15T08:00:45.000Z")
        self.assertEqual(err.extra["secondsLeft"], 35)

    def test_same_user_second_client_conflicts(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "alice", "tab2", T0 + 1)
        self.assertEqual(ctx.exception.code, "leased")
        self.assertEqual(ctx.exception.extra["holder"], "alice")

    def test_a_renew_after_a_takeover_is_lease_lost(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        took = leases.takeover(self.store, "item", self.a, "bob", "tabB", T0 + 5)
        self.assertEqual((took.holder, took.client_id), ("bob", "tabB"))
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 15, renew=True)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "lease_lost"))
        self.assertEqual(ctx.exception.extra["holder"], "bob")
        # and a fresh acquire (not a renew) by the old holder is the plain "leased"
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 16)
        self.assertEqual(ctx.exception.code, "leased")

    def test_takeover_of_a_free_or_lapsed_lease_is_not_leased(self):
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.takeover(self.store, "item", self.a, "bob", "tabB", T0)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "not_leased"))
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.takeover(self.store, "item", self.a, "bob", "tabB", T0 + 60)
        self.assertEqual(ctx.exception.code, "not_leased")

    def test_a_takeover_by_the_holder_themselves_changes_nothing(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        got = leases.takeover(self.store, "item", self.a, "alice", "tab1", T0 + 5)
        self.assertEqual((got.holder, got.client_id), ("alice", "tab1"))

    def test_a_second_tab_of_the_same_user_can_take_over_the_first(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        got = leases.takeover(self.store, "item", self.a, "alice", "tab2", T0 + 5)
        self.assertEqual(got.client_id, "tab2")
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 10, renew=True)
        self.assertEqual(ctx.exception.code, "lease_lost")

    def test_the_taken_hook_is_told_who_lost_it(self):
        seen = []
        with mock.patch.object(leases, "ON_TAKEN", lambda store, **kw: seen.append(kw)):
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
            leases.takeover(self.store, "item", self.a, "bob", "tabB", T0 + 5)
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["previous"], seen[0]["actor"], seen[0]["item"]), ("alice", "bob", self.a))
        self.assertEqual(seen[0]["show"], self.show)

    def test_a_failing_hook_does_not_undo_the_takeover(self):
        def boom(store, **kw):
            raise RuntimeError("notifications are down")
        with mock.patch.object(leases, "ON_TAKEN", boom):
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
            got = leases.takeover(self.store, "item", self.a, "bob", "tabB", T0 + 5)
        self.assertEqual(got.holder, "bob")

    def test_release_frees_only_the_callers_own_lease(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        self.assertFalse(leases.release(self.store, "item", self.a, "alice", "tab2"))       # another tab of alice
        self.assertFalse(leases.release(self.store, "item", self.a, "bob", "tab1"))
        self.assertEqual(len(leases.live(self.store, self.show, T0 + 1)), 1)
        self.assertTrue(leases.release(self.store, "item", self.a, "alice", "tab1"))
        self.assertFalse(leases.release(self.store, "item", self.a, "alice", "tab1"))        # idempotent
        self.assertEqual(leases.live(self.store, self.show, T0 + 1), [])

    def test_non_ready_item_cannot_be_leased(self):
        self.store.conn.execute("UPDATE item SET status = 'on air' WHERE id = ?", (self.b,))
        self.store.conn.commit()
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.b, "alice", "tab1", T0)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "not_editable"))
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", "nope", "alice", "tab1", T0)
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "comment", self.a, "alice", "tab1", T0)
        self.assertEqual(ctx.exception.status, 422)

    def test_a_take_ends_the_editing_the_next_renew_is_not_editable(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        self.store.conn.execute("UPDATE item SET status = 'on air' WHERE id = ?", (self.a,))
        self.store.conn.commit()
        with self.assertRaises(leases.LeaseError) as ctx:
            leases.acquire(self.store, "item", self.a, "alice", "tab1", T0 + 15, renew=True)
        self.assertEqual(ctx.exception.code, "not_editable")

    def test_live_lists_the_shows_unexpired_leases_with_seconds_left(self):
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        leases.acquire(self.store, "item", self.b, "bob", "tabB", T0 + 20)
        other, (x,) = mkitems(self.store, 1)
        leases.acquire(self.store, "item", x, "carol", "tabC", T0)
        got = leases.live(self.store, self.show, T0 + 30)
        self.assertEqual([(r["item"], r["holder"], r["secondsLeft"]) for r in got], [(self.a, "alice", 15), (self.b, "bob", 35)])
        self.assertEqual(got[0]["clientId"], "tab1")
        self.assertEqual([r["item"] for r in leases.live(self.store, self.show, T0 + 50)], [self.b])

    def test_check_write_allows_the_holder_and_free_items_only(self):
        leases.check_write(self.store, "item", self.a, "alice", "tab1", T0)                 # nobody holds it
        leases.acquire(self.store, "item", self.a, "alice", "tab1", T0)
        leases.check_write(self.store, "item", self.a, "alice", "tab1", T0 + 1)
        for who, client in (("bob", "tabB"), ("alice", "tab2"), ("alice", None)):
            with self.assertRaises(leases.LeaseError, msg=(who, client)) as ctx:
                leases.check_write(self.store, "item", self.a, who, client, T0 + 1)
            self.assertEqual(ctx.exception.code, "leased")
        leases.check_write(self.store, "item", self.a, "bob", "tabB", T0 + 45)               # lapsed: free again

    def test_item_version_changes_with_slug_or_script_only(self):
        v = building.item_version(self.store, self.a)
        self.assertRegex(v, r"^[0-9a-f]{16}$")
        self.assertEqual(v, building.item_version(self.store, self.a))
        self.assertNotEqual(v, building.item_version(self.store, self.b))
        building.rename_item(self.store, self.a, "new slug", None)
        v2 = building.item_version(self.store, self.a)
        self.assertNotEqual(v, v2)
        building.rename_item(self.store, self.a, None, "new text")
        self.assertNotEqual(v2, building.item_version(self.store, self.a))
        with self.assertRaises(building.BuildError) as ctx:
            building.item_version(self.store, "nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_the_slug_script_boundary_is_not_ambiguous(self):
        x, _ = building.create_show(self.store, "x")
        one = building.add_item(self.store, x, "ab", "c")[0]
        two = building.add_item(self.store, x, "a", "bc")[0]
        self.assertNotEqual(building.item_version(self.store, one), building.item_version(self.store, two))


class Rename(unittest.TestCase):
    """building.rename_item with the new `version` / `client_id` arguments."""

    def setUp(self):
        self.store = Store()
        self.show, (self.a, self.b, _) = mkitems(self.store)

    def test_rename_without_lease_or_version_behaves_as_before(self):
        building.rename_item(self.store, self.a, "renamed", None)
        self.assertEqual(self.store.conn.execute("SELECT slug FROM item WHERE id = ?", (self.a,)).fetchone()["slug"], "renamed")

    def test_rename_with_stale_version_is_409_and_changes_nothing(self):
        old = building.item_version(self.store, self.a)
        building.rename_item(self.store, self.a, "theirs", "their text", "bob")
        with self.assertRaises(building.BuildError) as ctx:
            building.rename_item(self.store, self.a, "mine", "my text", "alice", version=old)
        err = ctx.exception
        self.assertEqual((err.status, err.code), (409, "stale_item"))
        self.assertEqual(err.extra["current"], {"slug": "theirs", "script": "their text", "version": building.item_version(self.store, self.a)})
        row = self.store.conn.execute("SELECT slug, script FROM item WHERE id = ?", (self.a,)).fetchone()
        self.assertEqual((row["slug"], row["script"]), ("theirs", "their text"))

    def test_rename_with_the_current_version_goes_through(self):
        building.rename_item(self.store, self.a, "mine", None, "alice", version=building.item_version(self.store, self.a))
        self.assertEqual(self.store.conn.execute("SELECT slug FROM item WHERE id = ?", (self.a,)).fetchone()["slug"], "mine")

    def test_rename_against_other_holders_live_lease_is_leased(self):
        leases.acquire(self.store, "item", self.a, "bob", "tabB", T0)
        for client in ("tab1", None):
            with self.assertRaises(building.BuildError, msg=client) as ctx:
                building.rename_item(self.store, self.a, "mine", None, "alice", client_id=client, now=T0 + 5)
            self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "leased"))
            self.assertEqual(ctx.exception.extra["holder"], "bob")
        self.assertEqual(self.store.conn.execute("SELECT slug FROM item WHERE id = ?", (self.a,)).fetchone()["slug"], "story 0")
        building.rename_item(self.store, self.a, "bobs", None, "bob", client_id="tabB", now=T0 + 5)      # the holder writes
        building.rename_item(self.store, self.a, "after", None, "alice", client_id="tab1", now=T0 + 46)  # lapsed

    def test_the_lease_is_checked_before_the_version(self):
        leases.acquire(self.store, "item", self.a, "bob", "tabB", T0)
        with self.assertRaises(building.BuildError) as ctx:
            building.rename_item(self.store, self.a, "mine", None, "alice", version="0" * 16, client_id="tab1", now=T0 + 1)
        self.assertEqual(ctx.exception.code, "leased")

    def test_rename_still_refuses_a_non_ready_item_first(self):
        self.store.conn.execute("UPDATE item SET status = 'aired' WHERE id = ?", (self.a,))
        self.store.conn.commit()
        with self.assertRaises(building.BuildError) as ctx:
            building.rename_item(self.store, self.a, "x", None, version="0" * 16)
        self.assertEqual(ctx.exception.code, "not_editable")

    def test_with_the_lease_table_gone_a_rename_still_works(self):
        self.store.conn.execute("DROP TABLE lease")
        self.store.conn.commit()
        building.rename_item(self.store, self.a, "still", None, client_id="tab1", now=T0)
        self.assertEqual(self.store.conn.execute("SELECT slug FROM item WHERE id = ?", (self.a,)).fetchone()["slug"], "still")


class Presence(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.show, _ = mkitems(self.store, 1)
        users.add(self.store, "alice", "operator")
        users.add(self.store, "bob", "operator")

    def test_presence_expires_after_30s_and_rows_pruned_after_5min(self):
        leases.beat(self.store, "alice", "tab1", self.show, "show", T0)
        self.assertEqual([r["user"] for r in leases.who(self.store, self.show, T0 + 29)], ["alice"])
        self.assertEqual(leases.who(self.store, self.show, T0 + 30), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0], 1)    # expired is not deleted yet
        leases.beat(self.store, "bob", "tabB", self.show, "library", T0 + 299)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0], 2)
        leases.beat(self.store, "bob", "tabB", self.show, "library", T0 + 301)
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT user_name FROM presence")], ["bob"])

    def test_one_row_per_user_and_client_and_the_view_updates(self):
        leases.beat(self.store, "alice", "tab1", self.show, "show", T0)
        leases.beat(self.store, "alice", "tab1", self.show, "item:abc", T0 + 10)
        leases.beat(self.store, "alice", "tab2", self.show, "library", T0 + 10)
        got = {r["clientId"]: r["view"] for r in leases.who(self.store, self.show, T0 + 11)}
        self.assertEqual(got, {"tab1": "item:abc", "tab2": "library"})

    def test_who_carries_the_display_name_and_falls_back_to_the_user(self):
        self.store.conn.execute("UPDATE volt_user SET display_name = 'Alice A' WHERE name = 'alice'")
        self.store.conn.commit()
        leases.beat(self.store, "alice", "tab1", self.show, "show", T0)
        leases.beat(self.store, "bob", "tabB", self.show, "show", T0)
        got = {r["user"]: r["name"] for r in leases.who(self.store, self.show, T0 + 1)}
        self.assertEqual(got, {"alice": "Alice A", "bob": "bob"})

    def test_who_is_per_show(self):
        other, _ = mkitems(self.store, 1)
        leases.beat(self.store, "alice", "tab1", self.show, "show", T0)
        leases.beat(self.store, "bob", "tabB", other, "show", T0)
        self.assertEqual([r["user"] for r in leases.who(self.store, other, T0 + 1)], ["bob"])

    def test_a_bad_view_or_client_id_is_refused(self):
        for view in ("", "nowhere", "item:", "item:" + "x" * 200, 5, "show\n"):
            with self.assertRaises(leases.LeaseError, msg=view) as ctx:
                leases.beat(self.store, "alice", "tab1", self.show, view, T0)
            self.assertEqual(ctx.exception.status, 422)
        for client in ("", "has space", "x" * 70, None, 5):
            with self.assertRaises(leases.LeaseError, msg=client):
                leases.beat(self.store, "alice", client, self.show, "show", T0)

    def test_the_table_is_capped_per_user_so_a_hostile_client_cannot_fill_it(self):
        for n in range(leases.MAX_CLIENTS_PER_USER + 5):
            leases.beat(self.store, "alice", f"tab{n}", self.show, "show", T0 + n * 0.01)
        n = self.store.conn.execute("SELECT COUNT(*) FROM presence WHERE user_name = 'alice'").fetchone()[0]
        self.assertEqual(n, leases.MAX_CLIENTS_PER_USER)


class Http(Api):
    def setUp(self):
        super().setUp()
        self.now = T0
        patcher = mock.patch.object(serve, "CLOCK", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tokens = {}
        for name, role in (("root", "admin"), ("alice", "operator"), ("bob", "operator"), ("vic", "viewer")):
            self.tokens[name] = users.add(self.store, name, role)[1]
        self.root, self.alice, self.bob, self.vic = (self.tokens[n] for n in ("root", "alice", "bob", "vic"))
        self.show, self.items = _show(self.store, [[], [], []])
        self.a, self.b, self.c = self.items

    def lease(self, item, token, client="tab1", path="lease", **extra):
        return self.post(f"/api/items/{item}/{path}", {"clientId": client, **extra}, token=token)

    def audit(self, target):
        return [r["action"] for r in self.store.conn.execute("SELECT action FROM audit WHERE target_id = ? ORDER BY at, id", (target,))]

    def state(self, token, show=None):
        s, out, _ = self.call("GET", f"/api/state?show={show or self.show}", token=token)
        self.assertEqual(s, 200, out)
        return out

    # ---- the routes -------------------------------------------------------

    def test_acquire_renew_release_over_http(self):
        s, out = self.lease(self.a, self.alice)
        self.assertEqual(s, 200, out)
        self.assertEqual((out["lease"]["holder"], out["lease"]["secondsLeft"]), ("alice", 45))
        self.assertTrue(out["lease"]["expiresAt"].endswith("Z"))
        self.now += 15
        s, out = self.lease(self.a, self.alice, renew=True)
        self.assertEqual((s, out["lease"]["secondsLeft"]), (200, 45))
        s, out = self.lease(self.a, self.alice, path="lease/release")
        self.assertEqual((s, out["released"]), (200, True))
        s, out = self.lease(self.a, self.alice, path="lease/release")
        self.assertEqual((s, out["released"]), (200, False))

    def test_other_holder_gets_409_leased_with_holder_over_http(self):
        self.lease(self.a, self.alice)
        out = self.refused(409, "leased", f"/api/items/{self.a}/lease", {"clientId": "tabB"}, token=self.bob)
        self.assertEqual(out["holder"], "alice")
        self.assertIn("expiresAt", out)

    def test_takeover_then_old_holder_renew_is_lease_lost(self):
        self.lease(self.a, self.alice)
        self.now += 5
        s, out = self.lease(self.a, self.bob, client="tabB", path="lease/takeover")
        self.assertEqual((s, out["lease"]["holder"]), (200, "bob"))
        out = self.refused(409, "lease_lost", f"/api/items/{self.a}/lease", {"clientId": "tab1", "renew": True}, token=self.alice)
        self.assertEqual(out["holder"], "bob")

    def test_takeover_of_a_free_item_is_409_not_leased(self):
        self.refused(409, "not_leased", f"/api/items/{self.a}/lease/takeover", {"clientId": "tabB"}, token=self.bob)

    def test_the_body_is_validated(self):
        self.refused(422, "bad_input", f"/api/items/{self.a}/lease", {}, token=self.alice)
        self.refused(422, "bad_input", f"/api/items/{self.a}/lease", {"clientId": "bad id"}, token=self.alice)
        self.refused(422, "bad_input", f"/api/items/{self.a}/lease", {"clientId": "t", "renew": "yes"}, token=self.alice)
        self.refused(422, "bad_input", f"/api/items/{self.a}/lease", {"clientId": "t", "ttl": 9999}, token=self.alice)
        self.refused(404, "not_found", "/api/items/nope/lease", {"clientId": "t"}, token=self.alice)

    def test_non_ready_item_cannot_be_leased_over_http(self):
        self.store.conn.execute("UPDATE item SET status = 'aired' WHERE id = ?", (self.c,))
        self.store.conn.commit()
        self.refused(409, "not_editable", f"/api/items/{self.c}/lease", {"clientId": "t"}, token=self.alice)

    def test_a_viewer_cannot_lease_but_can_send_presence(self):
        self.refused(403, "forbidden_role", f"/api/items/{self.a}/lease", {"clientId": "t"}, token=self.vic)
        s, out = self.post("/api/presence", {"clientId": "tabV", "show": self.show, "view": "show"}, token=self.vic)
        self.assertEqual(s, 200, out)

    def test_two_tabs_of_one_user_conflict_over_http(self):
        self.lease(self.a, self.alice, client="tab1")
        out = self.refused(409, "leased", f"/api/items/{self.a}/lease", {"clientId": "tab2"}, token=self.alice)
        self.assertEqual(out["holder"], "alice")

    # ---- enforcement on the writes ---------------------------------------

    def test_rename_with_stale_version_is_409_and_changes_nothing_over_http(self):
        old = building.item_version(self.store, self.a)
        self.assertEqual(self.post(f"/api/items/{self.a}/rename", {"slug": "theirs"}, token=self.bob)[0], 200)
        out = self.refused(409, "stale_item", f"/api/items/{self.a}/rename", {"slug": "mine", "version": old}, token=self.alice)
        self.assertEqual(out["current"]["slug"], "theirs")
        self.assertEqual(self.store.conn.execute("SELECT slug FROM item WHERE id = ?", (self.a,)).fetchone()["slug"], "theirs")

    def test_rename_against_other_holders_live_lease_is_409_over_http(self):
        self.lease(self.a, self.bob, client="tabB")
        out = self.refused(409, "leased", f"/api/items/{self.a}/rename", {"slug": "mine", "clientId": "tab1"}, token=self.alice)
        self.assertEqual(out["holder"], "bob")
        s, out = self.post(f"/api/items/{self.a}/rename", {"slug": "bobs", "clientId": "tabB"}, token=self.bob)
        self.assertEqual(s, 200, out)

    def test_rename_without_lease_or_version_behaves_as_before_over_http(self):
        s, out = self.post(f"/api/items/{self.a}/rename", {"slug": "plain"}, token=self.alice)
        self.assertEqual(s, 200, out)

    def test_rename_fields_are_validated(self):
        self.refused(422, "bad_input", f"/api/items/{self.a}/rename", {"slug": "x", "version": 5}, token=self.alice)
        self.refused(422, "bad_input", f"/api/items/{self.a}/rename", {"slug": "x", "clientId": "bad id"}, token=self.alice)

    def test_the_story_save_respects_a_live_lease_and_keeps_its_revision_check(self):
        story = self.call("GET", f"/api/items/{self.a}/story", token=self.alice)[1]
        self.lease(self.a, self.bob, client="tabB")
        out = self.refused(409, "leased", f"/api/items/{self.a}/script",
                           {"script": "mine", "baseRev": story["rev"], "clientId": "tab1"}, token=self.alice)
        self.assertEqual(out["holder"], "bob")
        self.refused(409, "leased", f"/api/items/{self.a}/script", {"script": "mine", "baseRev": story["rev"]}, token=self.alice)
        s, out = self.post(f"/api/items/{self.a}/script", {"script": "bobs", "baseRev": story["rev"], "clientId": "tabB"}, token=self.bob)
        self.assertEqual(s, 200, out)
        # the existing optimistic check still holds for the holder: a stale baseRev is stale_script, not a pass
        self.refused(409, "stale_script", f"/api/items/{self.a}/script", {"script": "again", "baseRev": story["rev"], "clientId": "tabB"}, token=self.bob)

    def test_the_story_save_without_a_lease_or_client_behaves_as_before(self):
        story = self.call("GET", f"/api/items/{self.b}/story", token=self.alice)[1]
        s, out = self.post(f"/api/items/{self.b}/script", {"script": "plain", "baseRev": story["rev"]}, token=self.alice)
        self.assertEqual(s, 200, out)

    # ---- the on-air path -------------------------------------------------

    def test_take_is_never_blocked_by_a_lease(self):
        self.lease(self.a, self.alice)
        self.lease(self.b, self.bob, client="tabB")
        s, out = self.post(f"/api/shows/{self.show}/go", token=self.root)
        self.assertEqual(s, 200, out)
        self.assertEqual(out["target"], self.a)
        s, out = self.post(f"/api/shows/{self.show}/go", token=self.bob)
        self.assertEqual((s, out["target"]), (200, self.b))
        self.assertEqual(self.post(f"/api/items/{self.c}/skip", token=self.alice)[0], 200)

    def broken(self):
        """Every collaboration read and write fails as it would with the tables dropped or locked. (A fresh connection recreates a dropped
        table through the schema bootstrap, so the failure is injected where the tables are used.)"""
        err = sqlite3.OperationalError("no such table: lease")
        stack = contextlib.ExitStack()
        for name in ("acquire", "release", "takeover", "check_write", "live", "beat", "who"):
            stack.enter_context(mock.patch.object(leases, name, side_effect=err))
        stack.enter_context(mock.patch.object(leases, "available", side_effect=err))
        self.addCleanup(stack.close)
        return stack

    def test_on_air_routes_work_with_lease_and_presence_tables_dropped(self):
        self.lease(self.a, self.alice)
        self.post("/api/presence", {"clientId": "t", "show": self.show, "view": "show"}, token=self.alice)
        with self.broken():
            for path, token in ((f"/api/shows/{self.show}/hold", self.alice), (f"/api/shows/{self.show}/release", self.alice),
                                (f"/api/shows/{self.show}/go", self.alice), (f"/api/shows/{self.show}/go", self.alice),
                                (f"/api/items/{self.c}/kill", self.alice)):
                body = {"reason": "x"} if path.endswith("/hold") else {}
                s, out = self.post(path, body, token=token)
                self.assertEqual(s, 200, (path, out))
            s, out = self.post(f"/api/items/{self.b}/skip", {}, token=self.alice)
            self.assertIn(s, (200, 409), out)                                 # skip may refuse for its own reasons (b is already on air)
            s, out = self.post(f"/api/shows/{self.show}/take", {}, token=self.alice)
            self.assertNotEqual(out.get("error", {}).get("code") if not out["ok"] else None, "collab_unavailable")
            self.assertLess(s, 600)
            state = self.state(self.alice)
            self.assertEqual((state["presence"], state["leases"]), ([], []))
            self.assertFalse(state["collab"]["available"])

    def test_the_collaboration_routes_say_503_when_the_tables_are_gone(self):
        with self.broken():
            self.refused(503, "collab_unavailable", f"/api/items/{self.a}/lease", {"clientId": "t"}, token=self.alice)
            self.refused(503, "collab_unavailable", f"/api/items/{self.a}/lease/release", {"clientId": "t"}, token=self.alice)
            self.refused(503, "collab_unavailable", f"/api/items/{self.a}/lease/takeover", {"clientId": "t"}, token=self.alice)
            self.refused(503, "collab_unavailable", "/api/presence", {"clientId": "t", "show": self.show, "view": "show"}, token=self.alice)

    def test_a_rename_with_the_lease_tables_unreadable_still_saves(self):
        with self.broken():
            s, out = self.post(f"/api/items/{self.a}/rename", {"slug": "still", "clientId": "tab1"}, token=self.alice)
        self.assertEqual(s, 200, out)

    def test_a_locked_lease_table_does_not_stop_the_state_poll(self):
        with mock.patch.object(leases, "live", side_effect=sqlite3.OperationalError("database is locked")):
            state = self.state(self.alice)
        self.assertEqual(state["leases"], [])
        self.assertFalse(state["collab"]["available"])

    # ---- state ------------------------------------------------------------

    def test_state_lists_presence_and_leases(self):
        self.lease(self.a, self.alice)
        self.post("/api/presence", {"clientId": "tab1", "show": self.show, "view": f"item:{self.a}"}, token=self.alice)
        self.post("/api/presence", {"clientId": "tabB", "show": self.show, "view": "show"}, token=self.bob)
        state = self.state(self.bob)
        self.assertTrue(state["collab"]["available"])
        self.assertEqual({(p["user"], p["view"]) for p in state["presence"]}, {("alice", f"item:{self.a}"), ("bob", "show")})
        self.assertEqual([(l["item"], l["holder"], l["secondsLeft"]) for l in state["leases"]], [(self.a, "alice", 45)])
        self.now += 31
        state = self.state(self.bob)
        self.assertEqual(state["presence"], [])                         # presence lapses at 30 s, the lease at 45
        self.assertEqual(len(state["leases"]), 1)

    def test_state_lists_presence_and_leases_filtered_by_show_view(self):
        self.lease(self.a, self.alice)
        self.post("/api/presence", {"clientId": "tab1", "show": self.show, "view": "show"}, token=self.alice)
        s, out = self.post(f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "alice", "level": "run"}, token=self.root)
        self.assertEqual(s, 200, out)
        # bob can no longer see the show: its presence and leases do not reach him, and asking for it by id gets the fallback show
        _, state, _ = self.call("GET", f"/api/state?show={self.show}", token=self.bob)
        self.assertNotEqual(state["show"]["id"] if state["show"] else None, self.show)
        self.assertNotIn("alice", str(state["presence"]) + str(state["leases"]))
        mine = self.state(self.alice)
        self.assertEqual(len(mine["presence"]), 1)
        self.assertEqual(len(mine["leases"]), 1)

    def test_presence_for_a_show_the_caller_cannot_see_is_refused(self):
        self.post(f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "alice", "level": "run"}, token=self.root)
        self.refused(403, "forbidden_show", "/api/presence", {"clientId": "t", "show": self.show, "view": "show"}, token=self.bob)
        self.refused(404, "not_found", "/api/presence", {"clientId": "t", "show": "nope", "view": "show"}, token=self.bob)

    def test_a_lease_on_a_hidden_show_is_refused_by_the_show_gate(self):
        self.post(f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "alice", "level": "run"}, token=self.root)
        self.refused(403, "forbidden_show", f"/api/items/{self.a}/lease", {"clientId": "t"}, token=self.bob)
        self.refused(403, "forbidden_show", f"/api/items/{self.a}/lease/takeover", {"clientId": "t"}, token=self.bob)

    def test_a_view_level_user_cannot_lease(self):
        self.post(f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "alice", "level": "run"}, token=self.root)
        self.post(f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "bob", "level": "view"}, token=self.root)
        self.refused(403, "forbidden_show", f"/api/items/{self.a}/lease", {"clientId": "t"}, token=self.bob)

    # ---- audit ------------------------------------------------------------

    def test_heartbeats_not_audited_but_acquire_takeover_release_are(self):
        self.lease(self.a, self.alice)
        for _ in range(3):
            self.now += 15
            self.lease(self.a, self.alice, renew=True)
            self.post("/api/presence", {"clientId": "tab1", "show": self.show, "view": "show"}, token=self.alice)
        self.assertEqual(self.audit(self.a), ["lease_acquire"])
        self.lease(self.a, self.bob, client="tabB", path="lease/takeover")
        self.lease(self.a, self.bob, client="tabB", path="lease/release")
        self.assertEqual(self.audit(self.a), ["lease_acquire", "lease_takeover", "lease_release"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit WHERE action LIKE '%presence%' OR target_id LIKE '%presence%'").fetchone()[0], 0)

    def test_takeover_audited_with_who_lost_it(self):
        self.lease(self.a, self.alice)
        self.lease(self.a, self.bob, client="tabB", path="lease/takeover")
        row = self.store.conn.execute("SELECT actor, target_kind, detail FROM audit WHERE action = 'lease_takeover'").fetchone()
        self.assertEqual((row["actor"], row["target_kind"]), ("bob", "item"))
        self.assertIn("alice", row["detail"])
        self.assertNotIn("tabB", row["detail"] or "")

    def test_a_refused_acquire_and_a_no_op_release_write_no_audit_row(self):
        self.lease(self.a, self.alice)
        self.post(f"/api/items/{self.a}/lease", {"clientId": "tabB"}, token=self.bob)
        self.post(f"/api/items/{self.b}/lease/release", {"clientId": "tabB"}, token=self.bob)
        self.assertEqual(self.audit(self.a), ["lease_acquire"])
        self.assertEqual(self.audit(self.b), [])

    def test_a_renewal_after_a_lapse_is_a_fresh_acquire_and_is_audited(self):
        self.lease(self.a, self.alice)
        self.now += 100
        self.lease(self.a, self.alice, renew=True)
        self.assertEqual(self.audit(self.a), ["lease_acquire", "lease_acquire"])

    # ---- classification ---------------------------------------------------

    def test_new_routes_are_classified(self):
        names = {"act_item_lease", "act_item_lease_release", "act_item_lease_takeover", "act_presence"}
        self.assertEqual(names - {fn.__name__ for _, fn in serve.ROUTES}, set())
        for n in ("act_item_lease", "act_item_lease_release", "act_item_lease_takeover"):
            self.assertEqual((serve.ROUTE_ROLES[n], serve.ROUTE_SHOW_LEVEL[n], serve.SHOW_RESOLVER[n]), ("operator", "edit", "item"))
        self.assertEqual(serve.ROUTE_ROLES["act_presence"], "viewer")
        self.assertIn("act_presence", serve.NOT_SHOW_SCOPED)
        self.assertIn("presence", serve._AUDIT_KIND)

    def test_the_named_heartbeat_route_is_presence(self):
        self.assertEqual(serve.HEARTBEAT_ROUTES, frozenset({"act_presence"}))


class Race:
    """N writers at one item or one rundown; exactly one wins. Run on SQLite files and on live Postgres."""

    N = 8

    def open_store(self):
        raise NotImplementedError

    def run_all(self, fn):
        barrier = threading.Barrier(self.N)
        results = [None] * self.N

        def work(i):
            store = None
            try:
                store = self.open_store()               # a connection belongs to the thread that made it
                barrier.wait(30)
                results[i] = ("ok", fn(store, i))
            except BaseException as exc:  # noqa: BLE001 - the outcome is what the test looks at
                results[i] = ("err", exc)
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(self.N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return results

    def test_n_acquirers_one_winner(self):
        show, (item, _, _) = mkitems(self.setup_store())
        results = self.run_all(lambda store, i: leases.acquire(store, "item", item, f"user{i}", f"tab{i}", T0))
        wins = [r for r in results if r[0] == "ok"]
        losses = [r for r in results if r[0] == "err"]
        self.assertEqual(len(wins), 1, results)
        self.assertEqual(len(losses), self.N - 1)
        for _, exc in losses:
            self.assertIsInstance(exc, leases.LeaseError)
            self.assertEqual(exc.code, "leased")
            self.assertEqual(exc.extra["holder"], wins[0][1].holder)

    def test_n_acquirers_of_an_expired_lease_one_winner(self):
        store = self.setup_store()
        show, (item, _, _) = mkitems(store)
        leases.acquire(store, "item", item, "old", "tabOld", T0)
        results = self.run_all(lambda s, i: leases.acquire(s, "item", item, f"user{i}", f"tab{i}", T0 + 100))
        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1, results)

    def test_n_takeovers_of_one_live_lease_leave_a_single_consistent_holder(self):
        store = self.setup_store()
        show, (item, _, _) = mkitems(store)
        leases.acquire(store, "item", item, "old", "tabOld", T0)
        results = self.run_all(lambda s, i: leases.takeover(s, "item", item, f"user{i}", f"tab{i}", T0 + 1))
        self.assertTrue(all(r[0] == "ok" for r in results), results)         # a takeover wins whenever it finds a live lease
        row = store.conn.execute("SELECT holder, client_id FROM lease WHERE resource_id = ?", (item,)).fetchone()
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM lease WHERE resource_id = ?", (item,)).fetchone()[0], 1)
        self.assertEqual(row["client_id"], "tab" + row["holder"][4:])

    def test_a_takeover_racing_the_holders_renew_never_leaves_two_holders(self):
        store = self.setup_store()
        show, (item, _, _) = mkitems(store)
        leases.acquire(store, "item", item, "old", "tabOld", T0)

        def fn(s, i):
            if i % 2:
                return leases.takeover(s, "item", item, f"user{i}", f"tab{i}", T0 + 1)
            return leases.acquire(s, "item", item, "old", "tabOld", T0 + 1, renew=True)
        self.run_all(fn)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM lease WHERE resource_id = ?", (item,)).fetchone()[0], 1)

    def test_n_concurrent_reorders_one_winner(self):
        store = self.setup_store()
        show, ids = mkitems(store, 4)
        version = building.order_version(store, show)
        orders = [list(reversed(ids)), [ids[1], ids[0], ids[2], ids[3]], [ids[3], ids[0], ids[1], ids[2]], ids[:2] + ids[:1:-1]]

        def fn(s, i):
            return building.reorder(s, show, orders[i % len(orders)], version)
        results = self.run_all(fn)
        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1, results)
        for kind, exc in results:
            if kind == "err":
                self.assertIsInstance(exc, building.BuildError)
                self.assertEqual(exc.code, "stale_order")
        got = [r["id"] for r in store.conn.execute("SELECT id FROM item WHERE show_id = ? ORDER BY position", (show,))]
        self.assertIn(got, orders)

    def test_n_renames_quoting_one_version_one_winner(self):
        store = self.setup_store()
        show, (item, _, _) = mkitems(store)
        version = building.item_version(store, item)
        results = self.run_all(lambda s, i: building.rename_item(s, item, f"slug {i}", None, f"user{i}", version=version, now=T0))
        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1, results)
        for kind, exc in results:
            if kind == "err":
                self.assertEqual(exc.code, "stale_item")


class RaceSqlite(Race, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "volt.db")
        self.opened = []
        self.addCleanup(lambda: [s.close() for s in self.opened])

    def setup_store(self):
        s = Store(self.db)
        self.opened.append(s)
        return s

    def open_store(self):
        s = Store(self.db)
        s.conn.execute("PRAGMA busy_timeout=20000")
        return s


class RacePostgres(Race, PgCase):
    def setup_store(self):
        return self.store()

    def open_store(self):
        return Store(self.dsn, init=False)


class Migration(unittest.TestCase):
    NAME = "edit leases and presence"

    def migration(self):
        return next(m for m in migrations.MIGRATIONS if m.name == self.NAME)

    def test_the_registry_names_it_and_every_statement_is_idempotent(self):
        m = self.migration()
        self.assertGreater(m.version, 32)
        for stmts in (m.sqlite, m.postgres):
            self.assertTrue(all("IF NOT EXISTS" in s for s in stmts))

    def test_a_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = self.migration()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            show, (a, _, _) = mkitems(store)
            store.conn.execute("DROP TABLE lease")
            store.conn.execute("DROP TABLE presence")
            store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            store.conn.commit()
            store.close()
            store = Store(path)
            try:
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(lease)")],
                                 ["resource_kind", "resource_id", "holder", "client_id", "acquired_at", "renewed_at", "expires_at"])
                self.assertEqual([r[1] for r in store.conn.execute("PRAGMA table_info(presence)")],
                                 ["user_name", "client_id", "show_id", "view", "seen_at"])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 3)
                leases.acquire(store, "item", a, "alice", "t", T0)                      # and it works
                with self.assertRaises(leases.LeaseError):
                    leases.acquire(store, "item", a, "bob", "t", T0 + 1)
            finally:
                store.close()

    def test_the_oldest_historical_shape_gains_them(self):
        from tests.historical_shapes import SHAPES
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(SHAPES[0][2])
            conn.close()
            store = Store(path)
            try:
                names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({"lease", "presence"} <= names)
            finally:
                store.close()

    def test_this_migration_is_not_assumed_to_be_the_latest(self):
        self.assertLessEqual(self.migration().version, migrations.LATEST)


class MigrationPostgres(PgCase):
    def test_a_postgres_database_without_the_tables_gains_them_and_keeps_its_rows(self):
        m = Migration().migration()
        store = self.store()
        users.add(store, "alice", "operator")
        show, (a, _, _) = mkitems(store)
        store.conn.execute("DROP TABLE lease")
        store.conn.execute("DROP TABLE presence")
        store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        for table, want in (("lease", ["resource_kind", "resource_id", "holder", "client_id", "acquired_at", "renewed_at", "expires_at"]),
                            ("presence", ["user_name", "client_id", "show_id", "view", "seen_at"])):
            rows = again.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ? "
                "ORDER BY ordinal_position", (table,)).fetchall()
            self.assertEqual([r[0] for r in rows], want)
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
        got = leases.acquire(again, "item", a, "alice", "t", T0)
        self.assertEqual(got.expires_at, T0 + 45)
        with self.assertRaises(leases.LeaseError):
            leases.acquire(again, "item", a, "bob", "t", T0 + 1)
        leases.beat(again, "alice", "t", show, "show", T0)
        self.assertEqual([r["user"] for r in leases.who(again, show, T0 + 1)], ["alice"])

    def test_expiry_and_renew_on_postgres(self):
        store = self.store()
        show, (a, _, _) = mkitems(store)
        leases.acquire(store, "item", a, "alice", "t", T0)
        renewed = leases.acquire(store, "item", a, "alice", "t", T0 + 15, renew=True)
        self.assertEqual((renewed.acquired_at, renewed.expires_at, renewed.fresh), (T0, T0 + 60, False))
        with self.assertRaises(leases.LeaseError):
            leases.acquire(store, "item", a, "bob", "b", T0 + 59)
        self.assertEqual(leases.acquire(store, "item", a, "bob", "b", T0 + 60).holder, "bob")


if __name__ == "__main__":
    unittest.main()
