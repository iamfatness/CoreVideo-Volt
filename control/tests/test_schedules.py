"""Relative ("after the previous item") and recurring schedules (migration 19)."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.historical_shapes import SHAPES
from tests.test_clock import Now, T0, _tick
from tests.test_operator_api import Api
from tests.test_rooms import _no_sleep
from tests.test_take import _show
from volt import clock, migrations
from volt.clock import set_schedule
from volt.playout import advance, commit_take
from volt.rooms import FakeRoom
from volt.store import Store


def _state(store, item):
    r = store.conn.execute(
        "SELECT status, schedule_state, scheduled_at, schedule_mode, schedule_offset_seconds FROM item WHERE id = ?", (item,)).fetchone()
    return dict(zip(("status", "schedule_state", "scheduled_at", "schedule_mode", "schedule_offset_seconds"), tuple(r)))


def _lengths(store, item_ids, seconds=(2, 2, 2)):
    """Give each item's span an essence at 25 fps and a stored frame range of `seconds` long."""
    asset = store.conn.execute("SELECT DISTINCT asset_id FROM span").fetchone()["asset_id"]
    store.add_essence(asset, "hi-res", "x.mxf", frame_rate="25", timecode_start="01:00:00:00")
    for item_id, secs in zip(item_ids, seconds):
        span = store.conn.execute("SELECT span_id FROM item WHERE id = ?", (item_id,)).fetchone()["span_id"]
        store.conn.execute("UPDATE span SET frame_in = 0, frame_out = ? WHERE id = ?", (secs * 25, span))
    store.conn.commit()


def _air(store, show, item, at):
    """Put `item` on air at `at` through the real playout path."""
    adv = advance(store, show, lambda: at.strftime("%Y-%m-%dT%H:%M:%SZ"), pending=True)
    commit_take(store, adv, lambda: at.strftime("%Y-%m-%dT%H:%M:%SZ"))


class Relative(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.show, self.items = _show(self.store, [[], [], []])
        _lengths(self.store, self.items)
        clock.set_relative(self.store, self.items[1], 10)   # 10 s after the previous item has run its length

    def test_not_due_while_the_previous_item_has_not_started(self):
        self.assertEqual(_tick(self.store, Now(T0)), [])
        self.assertIsNone(_state(self.store, self.items[1])["scheduled_at"])

    def test_due_at_previous_start_plus_its_length_plus_the_offset(self):
        _air(self.store, self.show, self.items[0], T0)          # item 0 starts at T0, runs 2 s
        self.assertEqual(_tick(self.store, Now(T0 + timedelta(seconds=5))), [])
        self.assertEqual(_state(self.store, self.items[1])["scheduled_at"], "2026-10-11T20:00:12Z")
        self.assertEqual(_tick(self.store, Now(T0 + timedelta(seconds=11))), [])    # 1 s early
        events = _tick(self.store, Now(T0 + timedelta(seconds=12)))
        self.assertEqual([e.outcome for e in events], ["fired"])

    def test_an_unknown_length_counts_as_zero(self):
        store = Store()
        show, items = _show(store, [[], []])               # no essence: no length
        clock.set_relative(store, items[1], 4)
        _air(store, show, items[0], T0)
        _tick(store, Now(T0))
        self.assertEqual(_state(store, items[1])["scheduled_at"], "2026-10-11T20:00:04Z")

    def test_a_skipped_or_killed_previous_item_is_passed_over(self):
        store = Store()
        show, items = _show(store, [[], [], []])
        _lengths(store, items)
        clock.set_relative(store, items[2], 0)
        store.conn.execute("UPDATE item SET status = 'killed' WHERE id = ?", (items[1],))
        store.conn.commit()
        _air(store, show, items[0], T0)
        _tick(store, Now(T0))
        self.assertEqual(_state(store, items[2])["scheduled_at"], "2026-10-11T20:00:02Z")   # item 0 + 2 s

    def test_the_first_item_cannot_be_relative(self):
        with self.assertRaises(ValueError):
            clock.set_relative(self.store, self.items[0], 0)

    def test_switching_back_to_an_absolute_time_clears_the_mode(self):
        set_schedule(self.store, self.items[1], T0 + timedelta(seconds=99))
        row = _state(self.store, self.items[1])
        self.assertIsNone(row["schedule_mode"])
        self.assertIsNone(row["schedule_offset_seconds"])


class Api19(Api):
    def setUp(self):
        super().setUp()
        self.show, (self.a, self.b) = _show(self.store, [[], []])

    def sched(self, item, body):
        return self.call("POST", f"/api/items/{item}/schedule", body)

    def test_mode_after_previous_is_set_and_shown(self):
        status, out, _ = self.sched(self.b, {"mode": "after_previous", "offsetSeconds": 30})
        self.assertEqual(status, 200)
        it = out["rundown"]["items"][1]
        self.assertEqual((it["scheduleMode"], it["scheduleOffsetSeconds"], it["scheduledAt"]), ("after_previous", 30, None))
        status, out, _ = self.sched(self.b, {"mode": "none"})
        self.assertEqual((status, out["rundown"]["items"][1]["scheduleMode"]), (200, None))

    def test_bad_mode_bodies(self):
        for body in ({"mode": "sometime"}, {"mode": "after_previous", "offsetSeconds": -1}, {"mode": "after_previous", "offsetSeconds": 86400},
                     {"mode": "after_previous", "offsetSeconds": 1.5}, {"mode": "after_previous", "at": "2026-11-01T20:00:00Z"},
                     {"mode": "none", "offsetSeconds": 3}):
            status, out, _ = self.sched(self.b, body)
            self.assertEqual((status, out["error"]["code"]), (422, "bad_input"), body)

    def test_the_first_item_is_refused(self):
        status, out, _ = self.sched(self.a, {"mode": "after_previous"})
        self.assertEqual((status, out["error"]["code"]), (422, "bad_input"))


def _has_zones():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_zones(), "needs a time-zone database (on Windows: pip install tzdata)")
class Recurrence(unittest.TestCase):
    NY = {"freq": "daily", "at": "20:00", "tz": "America/New_York"}

    def setUp(self):
        self.store = Store()
        self.show, self.items = _show(self.store, [[("overlay.take", "on")], [], []])
        self.store.conn.execute("UPDATE show SET slot_seconds = 900 WHERE id = ?", (self.show,))
        self.store.conn.commit()
        clock.set_recurrence(self.store, self.show, self.NY)

    def occurrences(self):
        return self.store.conn.execute(
            "SELECT id, title, occurrence_on FROM show WHERE recurrence_of = ? ORDER BY occurrence_on", (self.show,)).fetchall()

    def test_a_daily_rule_clones_the_next_occurrence_inside_24_hours(self):
        # 2026-10-11 20:00 NY (EDT, -4) is 2026-10-12T00:00:00Z; a daily rule always has one within a day
        made = clock.materialise_recurring(self.store, datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(len(made), 1)
        self.assertEqual([(r["title"], r["occurrence_on"]) for r in self.occurrences()], [("Final Four 2026-10-11", "2026-10-11")])

    def test_nothing_is_cloned_while_the_next_occurrence_is_more_than_a_day_away(self):
        store = Store()
        show, _ = _show(store, [[], []])
        clock.set_recurrence(store, show, {"freq": "weekly", "days": [0], "at": "09:00", "tz": "UTC"})
        self.assertEqual(clock.materialise_recurring(store, datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)), [])   # a Tuesday
        self.assertEqual(len(clock.materialise_recurring(store, datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc))), 1)   # the Sunday before

    def test_the_clone_is_a_fresh_ready_copy_with_the_first_item_scheduled(self):
        clock.materialise_recurring(self.store, datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc))
        new = self.occurrences()[0]["id"]
        rows = self.store.conn.execute(
            "SELECT id, slug, status, span_id, scheduled_at, as_run_in FROM item WHERE show_id = ? ORDER BY position", (new,)).fetchall()
        self.assertEqual([r["slug"] for r in rows], ["item-0", "item-1", "item-2"])
        self.assertEqual({r["status"] for r in rows}, {"ready"})
        self.assertTrue(all(r["span_id"] for r in rows))
        self.assertTrue(all(r["as_run_in"] is None for r in rows))
        self.assertEqual(rows[0]["scheduled_at"], "2026-10-12T00:00:00Z")
        cue = self.store.conn.execute("SELECT room, command, payload, status FROM cue WHERE item_id = ?", (rows[0]["id"],)).fetchone()
        self.assertEqual((cue["room"], cue["command"], cue["payload"], cue["status"]), ("corevideo.pro", "overlay.take", "on", "stored"))
        slot = self.store.conn.execute("SELECT slot_seconds, recurrence FROM show WHERE id = ?", (new,)).fetchone()
        self.assertEqual((slot["slot_seconds"], slot["recurrence"]), (900, None))   # the clone does not recur itself

    def test_a_second_tick_and_a_second_clock_clone_nothing_more(self):
        now = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)
        clock.materialise_recurring(self.store, now)
        self.assertEqual(clock.materialise_recurring(self.store, now + timedelta(minutes=5)), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.db")
            a = Store(path)
            show, _ = _show(a, [[], []])
            clock.set_recurrence(a, show, self.NY)
            b = Store(path)
            self.assertEqual(len(clock.materialise_recurring(a, now)), 1)
            self.assertEqual(clock.materialise_recurring(b, now), [])
            a.close(); b.close()

    def test_weekly_runs_only_on_its_days(self):
        store = Store()
        show, _ = _show(store, [[], []])
        clock.set_recurrence(store, show, {"freq": "weekly", "days": [0], "at": "09:00", "tz": "UTC"})   # Mondays
        # 2026-10-11 is a Sunday: the next Monday is 2026-10-12 09:00Z
        self.assertEqual(len(clock.materialise_recurring(store, datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc))), 1)
        row = store.conn.execute("SELECT occurrence_on FROM show WHERE recurrence_of = ?", (show,)).fetchone()
        self.assertEqual(row["occurrence_on"], "2026-10-12")

    def test_dst_spring_forward_moves_a_missing_local_time_forward(self):
        when = clock.next_occurrence({"freq": "daily", "at": "02:30", "tz": "America/New_York"},
                                     datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(when, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))      # shown as 03:30 EDT

    def test_dst_fall_back_takes_the_first_of_an_ambiguous_time(self):
        when = clock.next_occurrence({"freq": "daily", "at": "01:30", "tz": "America/New_York"},
                                     datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(when, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))     # the EDT 01:30, not the EST one an hour later

    def test_a_midnight_boundary_uses_the_zone_date(self):
        # 2026-10-11 23:30 UTC is already 2026-10-12 in Auckland (NZDT, +13)
        rule = {"freq": "daily", "at": "09:00", "tz": "Pacific/Auckland"}
        # 2026-10-11 23:30 UTC is already 2026-10-12 12:30 in Auckland (NZDT, +13): today's 09:00 has gone, so the next is tomorrow's
        self.assertEqual(clock.next_occurrence(rule, datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)),
                         datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc))        # 2026-10-13 09:00 NZDT
        self.assertEqual(clock.next_occurrence(rule, datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)),
                         datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc))        # 2026-10-12 09:00 NZDT

    def test_bad_rules_are_refused(self):
        for rule in ({"freq": "monthly", "at": "20:00", "tz": "UTC"}, {"freq": "daily", "at": "25:00", "tz": "UTC"},
                     {"freq": "daily", "at": "20:00", "tz": "Nowhere/Land"}, {"freq": "weekly", "at": "20:00", "tz": "UTC"},
                     {"freq": "weekly", "days": [7], "at": "20:00", "tz": "UTC"}, {"freq": "daily", "at": "20:00"},
                     {"freq": "daily", "at": "20:00", "tz": "UTC", "until": "2027-01-01"}, "daily"):
            with self.assertRaises(ValueError, msg=rule):
                clock.validate_recurrence(rule)

    def test_clearing_the_rule_stops_cloning(self):
        clock.set_recurrence(self.store, self.show, None)
        self.assertEqual(clock.materialise_recurring(self.store, datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)), [])


@unittest.skipUnless(_has_zones(), "needs a time-zone database (on Windows: pip install tzdata)")
class RecurrenceApi(Api):
    def setUp(self):
        super().setUp()
        self.show, _ = _show(self.store, [[], []])

    def test_set_read_and_clear(self):
        rule = {"freq": "weekly", "days": [0, 2], "at": "09:30", "tz": "Europe/London"}
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/recurrence", {"rule": rule})
        self.assertEqual((status, out["rundown"]["recurrence"]), (200, rule))
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/recurrence", {"rule": None})
        self.assertEqual((status, out["rundown"]["recurrence"]), (200, None))

    def test_bad_rule_and_unknown_show(self):
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/recurrence", {"rule": {"freq": "hourly"}})
        self.assertEqual((status, out["error"]["code"]), (422, "bad_input"))
        status, out, _ = self.call("POST", "/api/shows/nope/recurrence", {"rule": None})
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))

    def test_a_clone_cannot_be_given_a_rule(self):
        self.store.conn.execute("UPDATE show SET recurrence_of = ? WHERE id = ?", ("x", self.show))
        self.store.conn.commit()
        status, out, _ = self.call("POST", f"/api/shows/{self.show}/recurrence", {"rule": {"freq": "daily", "at": "20:00", "tz": "UTC"}})
        self.assertEqual((status, out["error"]["code"]), (409, "not_a_template"))


class Migration19(unittest.TestCase):
    def test_every_historical_shape_gets_the_columns(self):
        for name, _c, ddl, _pg in SHAPES:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "old.db"
                conn = sqlite3.connect(str(path))
                conn.executescript(ddl)
                conn.commit()
                conn.close()
                store = Store(str(path))
                item = {r[1] for r in store.conn.execute("PRAGMA table_info(item)")}
                show = {r[1] for r in store.conn.execute("PRAGMA table_info(show)")}
                self.assertTrue({"schedule_mode", "schedule_offset_seconds"} <= item, name)
                self.assertTrue({"recurrence", "recurrence_of", "occurrence_on"} <= show, name)
                self.assertGreaterEqual(migrations.LATEST, 19)
                store.close()


if __name__ == "__main__":
    unittest.main()
