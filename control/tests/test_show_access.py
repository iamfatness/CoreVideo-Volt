"""Per-show access (identity plan, task 6): who may view, edit or run a show.

A show with no ACL rows is open (today's behaviour). Once it has a row, only the listed users and groups (and every admin, and the
legacy desk token) can touch it, at the listed level, and never above their global role. The clock and the runner are not subject to it.
"""
import re
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pgutil import PgCase
from tests.test_clock import Now, T0, _state, _tick
from tests.test_operator_api import Api
from tests.test_take import _show
from volt import access, building, clock, discuss, migrations, serve, users
from volt.clock import set_schedule
from volt.store import Store

LEVEL_FOR_NEED = {"view": "view", "edit": "edit", "run": "run"}
BELOW = {"edit": "view", "run": "edit"}


def mkshow(store, items=1, cue=False, title="Other show"):
    """A show with ready items (no media) and, optionally, one cue on the first. _show() mints spans for the whole store, so it is
    used once per test; this builds the rest."""
    show, _ = building.create_show(store, title, None)
    ids = [building.add_item(store, show, f"item-{n}")[0] for n in range(items)]
    if cue:
        building.add_cue(store, ids[0], "corevideo.pro", "overlay.take", "on")
    return show, ids


def principal(name="bob", role="operator", groups=(), legacy=False):
    return serve.Principal(name, role, legacy, tuple(groups))


class Levels(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        for name, role in (("root", "admin"), ("alice", "operator"), ("bob", "operator"), ("vic", "viewer")):
            users.add(self.store, name, role)
        access.create_group(self.store, "crew")
        access.set_members(self.store, "crew", add=["bob"])
        self.show, _ = _show(self.store, [[]])

    def level(self, **kw):
        return access.show_level(self.store, principal(**kw), self.show)

    def test_the_vocabulary(self):
        self.assertEqual(access.LEVELS, {"view": 0, "edit": 1, "run": 2})

    def test_a_show_with_no_rows_gives_the_level_the_global_role_implies(self):
        self.assertEqual(self.level(name="alice"), "run")
        self.assertEqual(self.level(name="vic", role="viewer"), "view")

    def test_admin_and_the_legacy_desk_always_run(self):
        access.set_acl(self.store, self.show, "user", "alice", "view")
        self.assertEqual(self.level(name="root", role="admin"), "run")
        self.assertEqual(self.level(name="desk", role="admin", legacy=True), "run")

    def test_once_a_row_exists_only_listed_subjects_have_any_level(self):
        access.set_acl(self.store, self.show, "user", "alice", "edit")
        self.assertEqual(self.level(name="alice"), "edit")
        self.assertIsNone(self.level(name="bob"))

    def test_a_group_row_reaches_members_through_the_principals_groups(self):
        access.set_acl(self.store, self.show, "group", "crew", "run")
        self.assertEqual(self.level(name="bob", groups=("crew",)), "run")
        self.assertIsNone(self.level(name="bob", groups=()))      # no longer a member: nothing

    def test_the_highest_matching_row_wins(self):
        access.set_acl(self.store, self.show, "group", "crew", "view")
        access.set_acl(self.store, self.show, "user", "bob", "run")
        self.assertEqual(self.level(name="bob", groups=("crew",)), "run")

    def test_a_user_row_is_not_matched_by_a_group_of_the_same_name(self):
        access.create_group(self.store, "alice")
        access.set_acl(self.store, self.show, "group", "alice", "run")
        self.assertIsNone(self.level(name="alice"))
        self.assertEqual(self.level(name="x", groups=("alice",)), "run")

    def test_the_global_role_caps_what_a_row_grants(self):
        access.set_acl(self.store, self.show, "user", "vic", "run")
        self.assertEqual(self.level(name="vic", role="viewer"), "view")

    def test_admin_and_the_desk_never_read_the_table(self):
        self.store.conn.execute("DROP TABLE show_acl")
        self.store.conn.commit()
        self.assertEqual(self.level(name="root", role="admin"), "run")
        self.assertEqual(self.level(name="desk", role="admin", legacy=True), "run")
        self.assertTrue(access.view_filter(self.store, principal(name="root", role="admin"))(self.show))
        with self.assertRaises(sqlite3.OperationalError):
            self.level(name="bob")                                   # anyone else cannot be answered, so the server says no (503)
        with self.assertRaises(sqlite3.OperationalError):
            access.view_filter(self.store, principal(name="bob"))

    def test_an_unknown_show_is_just_open(self):
        self.assertEqual(access.show_level(self.store, principal(name="alice"), "no-such-show"), "run")

    def test_set_acl_validates_and_null_removes(self):
        for bad in (("team", "alice", "view"), ("user", "alice", "own"), ("user", "alice", ""), ("user", "nobody", "view"),
                    ("group", "nogroup", "view"), ("user", 5, "view")):
            with self.assertRaises(access.AccessError, msg=bad) as ctx:
                access.set_acl(self.store, self.show, *bad)
            self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(access.AccessError) as ctx:
            access.set_acl(self.store, "nope", "user", "alice", "view")
        self.assertEqual(ctx.exception.status, 404)
        access.set_acl(self.store, self.show, "user", "alice", "view")
        access.set_acl(self.store, self.show, "user", "alice", "run")          # replaces
        self.assertEqual(access.list_acl(self.store, self.show), [{"subject_kind": "user", "subject": "alice", "level": "run"}])
        access.set_acl(self.store, self.show, "user", "alice", None)
        self.assertEqual(access.list_acl(self.store, self.show), [])

    def test_show_of_resolves_items_cues_and_shows(self):
        show, (item,) = mkshow(self.store, cue=True)
        cue = self.store.conn.execute("SELECT id FROM cue WHERE item_id = ?", (item,)).fetchone()["id"]
        self.assertEqual(access.show_of(self.store, "show", show), show)
        self.assertEqual(access.show_of(self.store, "item", item), show)
        self.assertEqual(access.show_of(self.store, "cue", cue), show)
        for kind in ("show", "item", "cue", "comment"):
            self.assertIsNone(access.show_of(self.store, kind, "missing"))
        with self.assertRaises(ValueError):
            access.show_of(self.store, "asset", "x")

    def test_the_visibility_filter(self):
        other, _ = mkshow(self.store)
        access.set_acl(self.store, self.show, "user", "alice", "view")
        seen = access.view_filter(self.store, principal(name="bob"))
        self.assertFalse(seen(self.show))
        self.assertTrue(seen(other))
        self.assertTrue(access.view_filter(self.store, principal(name="alice"))(self.show))
        self.assertTrue(access.view_filter(self.store, principal(name="root", role="admin"))(self.show))
        self.assertTrue(access.view_filter(self.store, principal(name="desk", role="admin", legacy=True))(self.show))

    def test_a_group_used_by_an_acl_cannot_be_deleted(self):
        access.set_acl(self.store, self.show, "group", "crew", "view")
        with self.assertRaises(access.AccessError) as ctx:
            access.delete_group(self.store, "crew")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "group_in_use"))
        access.set_acl(self.store, self.show, "group", "crew", None)
        access.delete_group(self.store, "crew")


class Routes(Api):
    def setUp(self):
        super().setUp()
        self.tokens = {}
        for name, role in (("root", "admin"), ("alice", "operator"), ("bob", "operator"), ("vic", "viewer")):
            self.tokens[name] = users.add(self.store, name, role)[1]
        self.root, self.alice, self.bob, self.vic = (self.tokens[n] for n in ("root", "alice", "bob", "vic"))
        self.show, (self.item, self.item2) = _show(self.store, [[("overlay.take", "on")], []])
        self.open_show, (self.open_item,) = mkshow(self.store)
        self.cue = self.store.conn.execute("SELECT id FROM cue WHERE item_id = ?", (self.item,)).fetchone()["id"]
        self.comment = discuss.add_comment(self.store, "alice", "item", self.item, "a note", now=T0.timestamp())["id"]

    def acl(self, subject_kind, subject, level, show=None, token=None):
        s, out = self.post(f"/api/shows/{show or self.show}/access",
                           {"subject_kind": subject_kind, "subject": subject, "level": level}, token=token or self.root)
        self.assertEqual(s, 200, out)
        return out

    def forbidden_show(self, path, body=None, token=None):
        out = self.refused(403, "forbidden_show", path, body, token=token)
        return out

    # ---- visibility -------------------------------------------------------

    def test_a_show_with_no_rows_is_open_as_before(self):
        s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)
        self.assertEqual(s, 200, out)
        s, state, _ = self.call("GET", "/api/state", token=self.vic)
        self.assertEqual(s, 200)
        self.assertEqual({x["id"] for x in state["shows"]}, {self.show, self.open_show})

    def test_the_first_row_hides_the_show_from_everyone_not_listed(self):
        self.acl("user", "alice", "run")
        for token in (self.bob, self.vic):
            _, state, _ = self.call("GET", "/api/state", token=token)
            self.assertEqual([x["id"] for x in state["shows"]], [self.open_show])
            s, out, _ = self.call("GET", f"/rundown?show={self.show}", token=token)
            self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))
        self.forbidden_show(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)
        _, state, _ = self.call("GET", "/api/state", token=self.alice)
        self.assertEqual({x["id"] for x in state["shows"]}, {self.show, self.open_show})
        s, _, _ = self.call("GET", f"/rundown?show={self.show}", token=self.alice)
        self.assertEqual(s, 200)

    def test_admin_still_sees_it_and_so_does_the_legacy_desk(self):
        self.acl("user", "alice", "run")
        _, state, _ = self.call("GET", "/api/state", token=self.root)
        self.assertEqual({x["id"] for x in state["shows"]}, {self.show, self.open_show})
        serve.TOKEN = "d" * 32
        s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token="d" * 32)
        self.assertEqual(s, 200, out)

    def test_a_remembered_hidden_show_falls_back_to_one_the_caller_can_see(self):
        self.acl("user", "alice", "run")
        _, state, _ = self.call("GET", f"/api/state?show={self.show}", token=self.bob)
        self.assertEqual(state["show"]["id"], self.open_show)
        self.assertNotIn(self.item, (state["cues"] or {}))
        self.assertNotIn(self.show, str(state["rundown"]))

    def test_unconfirmed_mos_show_ids_are_filtered_too(self):
        cols = [r[1] for r in self.store.conn.execute("PRAGMA table_info(mos_sync)")]
        self.assertIn("show_id", cols)
        self.store.conn.execute("INSERT INTO mos_device (device, state, detail, updated_at) VALUES ('ncs', 'up', '', '2026-11-02T00:00:00Z')")
        self.store.conn.execute("INSERT INTO mos_sync (device, show_id, state, updated_at) VALUES ('ncs', ?, 'unconfirmed', '2026-11-02T00:00:00Z')", (self.show,))
        self.store.conn.commit()
        self.acl("user", "alice", "run")
        _, mine, _ = self.call("GET", "/api/state", token=self.alice)
        _, theirs, _ = self.call("GET", "/api/state", token=self.bob)
        self.assertEqual(mine["mos"][0]["unconfirmed"], [self.show])
        self.assertEqual(theirs["mos"][0]["unconfirmed"], [])

    def test_reads_of_a_hidden_shows_items_are_refused_not_just_the_lists(self):
        self.acl("user", "alice", "run")
        for path in (f"/api/items/{self.item}/story", f"/api/items/{self.item}/cues", f"/api/shows/{self.show}/mos",
                     f"/api/shows/{self.show}/access"):
            s, out, _ = self.call("GET", path, token=self.bob)
            self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"), path)
        s, _, _ = self.call("GET", f"/api/items/{self.item}/story", token=self.alice)
        self.assertEqual(s, 200)

    def test_a_missing_show_is_404_and_a_hidden_one_is_403(self):
        self.acl("user", "alice", "run")
        self.refused(404, "not_found", "/api/shows/nope/hold", {}, token=self.bob)
        self.forbidden_show(f"/api/shows/{self.show}/hold", {}, token=self.bob)
        self.refused(404, "not_found", "/api/items/nope/skip", {}, token=self.bob)

    def test_the_refusal_names_the_level_and_lists_nobody(self):
        self.acl("user", "alice", "run")
        self.acl("user", "bob", "view")
        out = self.forbidden_show(f"/api/shows/{self.show}/hold", {}, token=self.bob)
        self.assertIn("run", out["error"]["reason"])
        self.assertNotIn("alice", str(out))
        carol = users.add(self.store, "carol", "operator")[1]
        out = self.forbidden_show(f"/api/shows/{self.show}/go", {}, token=carol)
        self.assertNotIn("alice", str(out))
        self.assertNotIn("bob", str(out))

    # ---- levels -----------------------------------------------------------

    def test_levels_gate_the_routes(self):
        self.acl("user", "bob", "view")
        self.forbidden_show(f"/api/shows/{self.show}/items", {"slug": "n"}, token=self.bob)       # view cannot build
        self.acl("user", "bob", "edit")
        s, out = self.post(f"/api/shows/{self.show}/items", {"slug": "n"}, token=self.bob)
        self.assertEqual(s, 200, out)                                                              # edit builds
        self.forbidden_show(f"/api/shows/{self.show}/take", {}, token=self.bob)                    # edit cannot run
        self.forbidden_show(f"/api/shows/{self.show}/hold", {}, token=self.bob)
        self.forbidden_show(f"/api/items/{self.item2}/skip", {}, token=self.bob)
        self.acl("user", "bob", "run")
        s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)
        self.assertEqual(s, 200, out)

    def test_an_item_and_a_cue_resolve_to_their_show(self):
        self.acl("user", "alice", "run")
        self.forbidden_show(f"/api/items/{self.item}/rename", {"slug": "z"}, token=self.bob)
        self.forbidden_show(f"/api/items/{self.item}/cues", {"command": "overlay.take"}, token=self.bob)
        self.forbidden_show(f"/api/cues/{self.cue}/remove", {}, token=self.bob)
        s, out = self.post(f"/api/items/{self.item}/rename", {"slug": "z"}, token=self.alice)
        self.assertEqual(s, 200, out)

    def test_the_guard_runs_before_the_body_is_read(self):
        self.acl("user", "alice", "run")
        s, out, _ = self.call("POST", f"/api/shows/{self.show}/items", raw=b"{not json", token=self.bob)
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))

    def test_the_global_role_caps_the_acl(self):
        self.acl("user", "vic", "run")
        self.refused(403, "forbidden_role", f"/api/shows/{self.show}/hold", {}, token=self.vic)
        _, state, _ = self.call("GET", "/api/state", token=self.vic)
        self.assertIn(self.show, [x["id"] for x in state["shows"]])

    def test_group_and_user_subjects(self):
        self.post("/api/groups", {"name": "crew"}, token=self.root)
        self.post("/api/groups/crew/members", {"add": ["bob"]}, token=self.root)
        self.acl("group", "crew", "run")
        s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)
        self.assertEqual(s, 200, out)
        carol = users.add(self.store, "carol", "operator")[1]
        self.forbidden_show(f"/api/shows/{self.show}/release", {}, token=carol)

    # ---- authority outliving its source ----------------------------------

    def test_removing_a_group_member_takes_access_away_on_the_next_request(self):
        self.post("/api/groups", {"name": "crew"}, token=self.root)
        self.post("/api/groups/crew/members", {"add": ["bob"]}, token=self.root)
        self.acl("group", "crew", "run")
        s, _ = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)
        self.assertEqual(s, 200)
        self.post("/api/groups/crew/members", {"remove": ["bob"]}, token=self.root)
        self.forbidden_show(f"/api/shows/{self.show}/release", {}, token=self.bob)
        _, state, _ = self.call("GET", "/api/state", token=self.bob)
        self.assertNotIn(self.show, [x["id"] for x in state["shows"]])

    def test_removing_the_acl_row_takes_access_away_on_the_next_request(self):
        self.acl("user", "alice", "run")
        self.acl("user", "bob", "run")
        self.assertEqual(self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)[0], 200)
        self.acl("user", "bob", None)
        self.forbidden_show(f"/api/shows/{self.show}/release", {}, token=self.bob)

    def test_a_role_change_applies_to_the_next_request(self):
        self.acl("user", "alice", "run")
        self.assertEqual(self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.alice)[0], 200)
        self.post("/api/users/alice/role", {"role": "viewer"}, token=self.root)
        self.refused(403, "forbidden_role", f"/api/shows/{self.show}/release", {}, token=self.alice)

    def test_a_disabled_user_with_a_row_is_refused_at_the_door(self):
        self.acl("user", "alice", "run")
        self.post("/api/users/alice/disable", {}, token=self.root)
        self.refused(401, "unauthorized", f"/api/shows/{self.show}/hold", {}, token=self.alice)

    def test_deleting_a_show_deletes_its_acl_so_a_new_show_with_the_id_starts_open(self):
        empty, _ = mkshow(self.store, items=0)
        self.acl("user", "alice", "run", show=empty)
        s, out = self.post(f"/api/shows/{empty}/delete", {}, token=self.root)
        self.assertEqual(s, 200, out)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM show_acl WHERE show_id = ?", (empty,)).fetchone()[0], 0)

    def test_a_repeat_of_a_restricted_show_is_restricted_the_same_way(self):
        self.acl("user", "alice", "edit")
        occ = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
        new = clock._clone_show(self.store, self.show, "Final Four", occ, "2026-11-03")
        self.assertEqual(access.list_acl(self.store, new), access.list_acl(self.store, self.show))
        self.assertIsNone(access.show_level(self.store, principal(name="bob"), new))

    # ---- the registry -----------------------------------------------------

    def test_every_post_route_is_scoped_or_exempt(self):
        names = [fn.__name__ for _, fn in serve.ROUTES]
        self.assertEqual(len(names), len(set(names)))
        scoped, exempt = set(serve.ROUTE_SHOW_LEVEL), set(serve.NOT_SHOW_SCOPED)
        self.assertEqual(scoped & exempt, set(), "a route is in both maps")
        self.assertEqual(set(names) - scoped - exempt, set(), "a POST route is in neither ROUTE_SHOW_LEVEL nor NOT_SHOW_SCOPED")
        self.assertEqual((scoped | exempt) - set(names), set(), "a map names a function that is not a route")
        self.assertEqual(set(serve.SHOW_RESOLVER), scoped, "every scoped route needs a resolver, and only those")
        for name, level in serve.ROUTE_SHOW_LEVEL.items():
            self.assertIn(level, access.LEVELS, name)
            self.assertIn(serve.SHOW_RESOLVER[name], ("show", "item", "cue", "mos", "comment"), name)

    def test_a_route_under_shows_items_or_cues_cannot_hide_in_the_exempt_list(self):
        for pattern, fn in serve.ROUTES:
            under = re.match(r"^\^/api/(shows|items|cues)/", pattern.pattern)
            if under:
                self.assertIn(fn.__name__, serve.ROUTE_SHOW_LEVEL, f"{fn.__name__} acts under /api/{under.group(1)}/{{id}} but is not show-scoped")

    def test_the_levels_are_what_the_spec_says(self):
        run = {"act_go", "act_take", "act_hold", "act_release", "act_skip", "act_kill"}
        for name, level in serve.ROUTE_SHOW_LEVEL.items():
            if name == "act_comment_delete":       # the author (or an admin) deletes; the show only has to be viewable
                self.assertEqual(level, "view", name)
                continue
            self.assertEqual(level, "run" if name in run | {"act_mos_resolve", "act_show_access",
                                                            "act_pro_clip_stage", "act_pro_clip_preview"} else "edit", name)

    def test_the_resolvers_are_the_kind_the_path_names(self):
        for pattern, fn in serve.ROUTES:
            if fn.__name__ in serve.SHOW_RESOLVER:
                kind = serve.SHOW_RESOLVER[fn.__name__]
                want = {"shows": "show", "items": "item", "cues": "cue", "mos": "mos", "comments": "comment"}[re.match(r"^\^/api/(\w+)/", pattern.pattern).group(1)]
                self.assertEqual(kind, "mos" if fn.__name__ == "act_mos_resolve" else want, fn.__name__)

    def _path(self, pattern, ident):
        return pattern.pattern.replace(serve._ID, ident).replace("()", "").lstrip("^").rstrip("$")

    def test_every_scoped_route_enforces_its_level(self):
        """Walk the real route table: a user with no row, or a row one level too low, is refused with forbidden_show before the body
        is read; a user with exactly the needed level is not refused for access (the body is empty, so another refusal is fine)."""
        self.acl("user", "alice", "run")                                # makes the show restricted
        ident = {"show": self.show, "item": self.item, "cue": self.cue, "mos": f"ncs:{self.show}", "comment": self.comment}
        routes = [(self._path(p, ident[serve.SHOW_RESOLVER[fn.__name__]]), fn.__name__) for p, fn in serve.ROUTES
                  if fn.__name__ in serve.ROUTE_SHOW_LEVEL]
        self.assertGreaterEqual(len(routes), 18)
        for path, name in routes:
            need = serve.ROUTE_SHOW_LEVEL[name]
            code = "forbidden_role" if serve.ROUTE_ROLES[name] == "admin" else "forbidden_show"
            s, out, _ = self.call("POST", path, raw=b"{not json", token=self.bob)                 # no row at all
            self.assertEqual((s, out["error"]["code"]), (403, code), path)
            if need in BELOW and serve.ROUTE_ROLES[name] != "admin":
                self.acl("user", "bob", BELOW[need])
                s, out, _ = self.call("POST", path, raw=b"{not json", token=self.bob)
                self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"), path)
                self.acl("user", "bob", None)
        self.acl("user", "bob", "run")
        for path, name in routes:
            s, out, _ = self.call("POST", path, {}, token=self.bob)
            self.assertNotEqual((out or {}).get("error", {}).get("code"), "forbidden_show", path)

    def test_an_unscoped_route_ignores_the_acl(self):
        self.acl("user", "alice", "run")
        s, out = self.post("/api/shows", {"title": "Brand new"}, token=self.bob)
        self.assertEqual(s, 200, out)
        s, out = self.post("/api/searches", {"name": "x", "query": "y"}, token=self.bob)
        self.assertNotEqual((out or {}).get("error", {}).get("code"), "forbidden_show")

    # ---- the clock and the runner -----------------------------------------

    def test_the_clock_fires_an_item_on_a_restricted_show(self):
        self.acl("user", "alice", "run")
        set_schedule(self.store, self.item, T0 + timedelta(seconds=5))
        now = Now(T0 + timedelta(seconds=5))
        events = _tick(self.store, now)
        self.assertEqual([(e.item_id, e.outcome) for e in events], [(self.item, "fired")])
        self.assertEqual(_state(self.store, self.item)["status"], "on air")

    def test_an_unreadable_acl_never_stops_an_admin_or_the_desk_from_running_the_show(self):
        self.acl("user", "alice", "run")
        broken = mock.Mock(side_effect=sqlite3.OperationalError("no such table: show_acl"))
        with mock.patch.object(access, "show_level", broken), mock.patch.object(access, "set_acl", broken):
            s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.root)
            self.assertEqual(s, 200, out)
            self.assertEqual(self.call("GET", "/api/state", token=self.root)[0], 200)
            serve.TOKEN = "d" * 32
            self.assertEqual(self.post(f"/api/shows/{self.show}/release", {}, token="d" * 32)[0], 200)
        self.assertFalse(broken.called)

    def test_when_the_acl_cannot_be_read_everyone_else_is_refused_not_let_through(self):
        self.acl("user", "alice", "run")
        broken = mock.Mock(side_effect=sqlite3.OperationalError("no such table: show_acl"))
        with mock.patch.object(access, "show_level", broken), mock.patch.object(access, "view_filter", broken):
            self.refused(503, "access_unavailable", f"/api/shows/{self.show}/hold", {}, token=self.bob)
            s, out, _ = self.call("GET", "/api/state", token=self.bob)
            self.assertEqual((s, out["error"]["code"]), (503, "access_unavailable"))
        self.assertEqual(self.store.conn.execute("SELECT held_at FROM show WHERE id = ?", (self.show,)).fetchone()[0], None)

    # ---- setting the ACL --------------------------------------------------

    def test_setting_the_acl_is_admin_only_and_audited_without_secrets(self):
        body = {"subject_kind": "user", "subject": "bob", "level": "edit"}
        self.refused(403, "forbidden_role", f"/api/shows/{self.show}/access", body, token=self.alice)
        self.refused(403, "forbidden_role", f"/api/shows/{self.show}/access", body, token=self.vic)
        s, out = self.post(f"/api/shows/{self.show}/access", body, token=self.root)
        self.assertEqual(s, 200, out)
        self.assertEqual(out["access"], [{"subject_kind": "user", "subject": "bob", "level": "edit"}])
        row = [r for r in self.store.list_audit(self.show) if r["action"] == "show_access"]
        self.assertEqual(len(row), 1)
        self.assertEqual((row[0]["actor"], row[0]["target_kind"], row[0]["target_id"]), ("root", "show", self.show))
        self.assertIn("bob", row[0]["detail"])
        self.assertIn("edit", row[0]["detail"])
        self.assertNotIn(self.root, str(row))

    def test_a_refused_acl_change_writes_no_audit_row(self):
        before = self.store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.refused(422, "bad_input", f"/api/shows/{self.show}/access", {"subject_kind": "team", "subject": "x", "level": "view"}, token=self.root)
        self.refused(422, "bad_input", f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "bob", "level": "own"}, token=self.root)
        self.refused(422, "bad_input", f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "bob"}, token=self.root)
        self.refused(422, "bad_input", f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "bob", "level": "view", "x": 1}, token=self.root)
        self.refused(422, "no_such_user", f"/api/shows/{self.show}/access", {"subject_kind": "user", "subject": "ghost", "level": "view"}, token=self.root)
        self.refused(404, "not_found", "/api/shows/nope/access", {"subject_kind": "user", "subject": "bob", "level": "view"}, token=self.root)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], before)

    def test_the_acl_is_readable_by_an_operator_who_can_view_the_show_only(self):
        self.acl("user", "alice", "run")
        s, out, _ = self.call("GET", f"/api/shows/{self.show}/access", token=self.alice)
        self.assertEqual((s, out["access"]), (200, [{"subject_kind": "user", "subject": "alice", "level": "run"}]))
        s, out, _ = self.call("GET", f"/api/shows/{self.show}/access", token=self.bob)
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_show"))
        s, out, _ = self.call("GET", f"/api/shows/{self.open_show}/access", token=self.bob)
        self.assertEqual((s, out["access"]), (200, []))
        s, out, _ = self.call("GET", f"/api/shows/{self.open_show}/access", token=self.vic)
        self.assertEqual((s, out["error"]["code"]), (403, "forbidden_role"))
        s, out, _ = self.call("GET", "/api/shows/nope/access", token=self.alice)
        self.assertEqual((s, out["error"]["code"]), (404, "not_found"))

    def test_removing_the_last_row_opens_the_show_again(self):
        self.acl("user", "alice", "run")
        self.acl("user", "alice", None)
        self.assertEqual(self.post(f"/api/shows/{self.show}/hold", {"reason": "x"}, token=self.bob)[0], 200)

    def test_a_group_in_an_acl_is_409_on_delete_over_http(self):
        self.post("/api/groups", {"name": "crew"}, token=self.root)
        self.acl("group", "crew", "view")
        self.refused(409, "group_in_use", "/api/groups/crew/delete", {}, token=self.root)

    def test_groups_listing_is_unchanged_and_state_for_an_open_install_is_unchanged(self):
        s, state, _ = self.call("GET", "/api/state", token=self.alice)
        self.assertEqual({x["id"] for x in state["shows"]}, {self.show, self.open_show})

    def test_the_open_loopback_mode_with_no_users_is_unchanged(self):
        # no token and no users: the desk, admin, loopback only
        self.store.conn.execute("DELETE FROM volt_user")
        self.store.conn.commit()
        s, out = self.post(f"/api/shows/{self.show}/hold", {"reason": "x"})
        self.assertEqual(s, 200, out)


class Migration(unittest.TestCase):
    NAME = "show access table"

    def migration(self):
        return next(m for m in migrations.MIGRATIONS if m.name == self.NAME)

    def test_the_registry_names_it_and_every_statement_is_idempotent(self):
        m = self.migration()
        self.assertGreater(m.version, 30)
        for stmts in (m.sqlite, m.postgres):
            self.assertTrue(all("IF NOT EXISTS" in s for s in stmts))

    def test_a_database_without_the_table_gains_it_and_keeps_its_rows(self):
        m = self.migration()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.db")
            store = Store(path)
            users.add(store, "alice", "operator")
            show, _ = mkshow(store)
            access.set_acl(store, show, "user", "alice", "run")
            store.conn.execute("DROP TABLE show_acl")
            store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
            store.conn.commit()
            store.close()
            store = Store(path)
            try:
                cols = [r[1] for r in store.conn.execute("PRAGMA table_info(show_acl)")]
                self.assertEqual(cols, ["show_id", "subject_kind", "subject", "level"])
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
                self.assertIn(m.version, [r[0] for r in store.conn.execute("SELECT version FROM schema_version")])
                access.set_acl(store, show, "user", "alice", "view")                      # and it works
                with self.assertRaises(store.IntegrityError):                             # primary key (show, kind, subject)
                    store.conn.execute("INSERT INTO show_acl VALUES (?, 'user', 'alice', 'run')", (show,))
            finally:
                store.close()

    def test_the_oldest_historical_shape_gains_it(self):
        from tests.historical_shapes import SHAPES
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(SHAPES[0][2])
            conn.close()
            store = Store(path)
            try:
                names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertIn("show_acl", names)
            finally:
                store.close()


class MigrationPostgres(PgCase):
    def test_a_postgres_database_without_the_table_gains_it_and_keeps_its_rows(self):
        m = Migration().migration()
        store = self.store()
        users.add(store, "alice", "operator")
        show, _ = mkshow(store)
        store.conn.execute("DROP TABLE show_acl")
        store.conn.execute("DELETE FROM schema_version WHERE version = ?", (m.version,))
        store.conn.commit()
        self.stores.pop().close()
        again = self.store()
        rows = again.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'show_acl' "
            "ORDER BY ordinal_position").fetchall()
        self.assertEqual([r[0] for r in rows], ["show_id", "subject_kind", "subject", "level"])
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM volt_user").fetchone()[0], 1)
        access.set_acl(again, show, "user", "alice", "edit")
        access.set_acl(again, show, "user", "alice", "run")
        self.assertEqual(access.list_acl(again, show), [{"subject_kind": "user", "subject": "alice", "level": "run"}])
        self.assertEqual(access.show_level(again, principal(name="alice"), show), "run")
        self.assertIsNone(access.show_level(again, principal(name="bob"), show))
        users.add(again, "bob", "operator")
        seen = access.view_filter(again, principal(name="bob"))
        self.assertFalse(seen(show))

    def test_a_repeat_copies_the_acl_and_a_deleted_show_takes_its_rows_with_it_on_postgres(self):
        store = self.store()
        users.add(store, "alice", "operator")
        show, _ = mkshow(store, items=2, cue=True)
        access.set_acl(store, show, "user", "alice", "edit")
        new = clock._clone_show(store, show, "Repeat", datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc), "2026-11-03")
        self.assertEqual(access.list_acl(store, new), [{"subject_kind": "user", "subject": "alice", "level": "edit"}])
        building.delete_show(store, new)
        self.assertEqual(access.list_acl(store, new), [])
        self.assertEqual(len(access.list_acl(store, show)), 1)


if __name__ == "__main__":
    unittest.main()
