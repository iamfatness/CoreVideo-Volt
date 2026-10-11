import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_rooms import _no_sleep, obs
from tests.test_take import _show
from volt.clock import schedule_rows, set_schedule, settle_stale_claims, tick, to_utc_text
from volt.playout import go, hold, on_air, release
from volt.rooms import FakeRoom
from volt.rundown import kill
from volt.store import Store

T0 = datetime(2026, 10, 11, 20, 0, 0, tzinfo=timezone.utc)


class Now:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


def _tick(store, now, room=None, **kw):
    room = room or FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
    return tick(store, room, now=now, sleep=_no_sleep, attempts=2, **kw)


def _state(store, item):
    r = store.conn.execute(
        "SELECT status, schedule_state, schedule_note, as_run_in FROM item WHERE id = ?", (item,)
    ).fetchone()
    return dict(zip(("status", "schedule_state", "schedule_note", "as_run_in"), tuple(r)))


class Firing(unittest.TestCase):
    def test_fires_at_the_due_time_and_not_before(self):
        store = Store()
        show, (a,) = _show(store, [[("overlay.take", "on")]])
        set_schedule(store, a, T0 + timedelta(seconds=10))
        now = Now()
        self.assertEqual(_tick(store, now), [])
        now.advance(9)
        self.assertEqual(_tick(store, now), [])
        self.assertIsNone(on_air(store, show))
        now.advance(1)
        events = _tick(store, now)
        self.assertEqual([(e.item_id, e.outcome) for e in events], [(a, "fired")])
        self.assertEqual(on_air(store, show), a)
        st = _state(store, a)
        self.assertEqual(st["schedule_state"], "fired")
        self.assertEqual(st["as_run_in"], "2026-10-11T20:00:10Z")  # from the injected clock
        now.advance(5)
        self.assertEqual(_tick(store, now), [])  # never again

    def test_unscheduled_items_are_left_alone(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        self.assertEqual(_tick(store, Now()), [])
        self.assertIsNone(on_air(store, show))

    def test_in_order_schedule_walks_the_rundown(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        set_schedule(store, a, T0)
        set_schedule(store, b, T0 + timedelta(seconds=60))
        now = Now()
        _tick(store, now)
        now.advance(60)
        events = _tick(store, now)
        self.assertEqual([e.outcome for e in events], ["fired"])
        self.assertEqual(on_air(store, show), b)
        self.assertEqual(_state(store, a)["status"], "aired")

    def test_out_of_order_schedule_waits_then_misses(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        set_schedule(store, b, T0)  # b due, a (unscheduled, ready) still ahead of it
        now = Now()
        self.assertEqual([e.outcome for e in _tick(store, now)], ["blocked"])
        self.assertIsNone(on_air(store, show))
        now.advance(31)
        self.assertEqual([e.outcome for e in _tick(store, now)], ["missed"])
        self.assertIsNone(on_air(store, show))

    def test_blocked_item_fires_once_the_predecessor_is_taken(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        set_schedule(store, b, T0)
        now = Now()
        self.assertEqual([e.outcome for e in _tick(store, now)], ["blocked"])
        go(store, show)
        now.advance(5)
        self.assertEqual([e.outcome for e in _tick(store, now)], ["fired"])
        self.assertEqual(on_air(store, show), b)

    def test_killed_predecessor_is_passed_over(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        kill(store, a)
        set_schedule(store, b, T0)
        self.assertEqual([e.outcome for e in _tick(store, Now())], ["fired"])
        self.assertEqual(_state(store, a)["status"], "skipped")


class Late(unittest.TestCase):
    def test_late_start_beyond_grace_is_missed_not_fired(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0)
        now = Now(T0 + timedelta(seconds=31))
        events = _tick(store, now)
        self.assertEqual([e.outcome for e in events], ["missed"])
        self.assertIn("grace", events[0].note)
        self.assertIsNone(on_air(store, show))
        self.assertEqual(_state(store, a)["schedule_state"], "missed")
        self.assertEqual(_tick(store, now), [])

    def test_inside_grace_after_downtime_still_fires(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0)
        self.assertEqual([e.outcome for e in _tick(store, Now(T0 + timedelta(seconds=30)))], ["fired"])

    def test_grace_is_configurable(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0)
        self.assertEqual([e.outcome for e in _tick(store, Now(T0 + timedelta(seconds=120)), grace=300)], ["fired"])

    def test_item_taken_by_hand_first_is_missed_not_retaken(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0 + timedelta(seconds=5))
        go(store, show)
        events = _tick(store, Now(T0 + timedelta(seconds=6)))
        self.assertEqual([e.outcome for e in events], ["missed"])
        self.assertIn("on air", events[0].note)


class Held(unittest.TestCase):
    def _held_then_released(self, store, show, a, after=2):
        set_schedule(store, a, T0)
        now = Now(T0 + timedelta(seconds=1))
        hold(store, show, "tech fault")
        events = _tick(store, now)
        release(store, show)
        now.advance(after)
        return now, events

    def test_held_show_does_not_advance_and_is_reported(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        now, events = self._held_then_released(store, show, a)
        self.assertEqual([e.outcome for e in events], ["held"])
        self.assertIn("tech fault", events[0].note)
        self.assertIsNone(on_air(store, show))
        self.assertEqual(_state(store, a)["schedule_state"], "held")

    def test_not_auto_fired_after_release_by_default(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        now, _ = self._held_then_released(store, show, a)  # still inside grace
        self.assertEqual([e.outcome for e in _tick(store, now)], ["missed"])
        self.assertIsNone(on_air(store, show))
        self.assertEqual(_state(store, a)["schedule_state"], "missed")

    def test_fire_after_hold_policy_fires_inside_grace(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        now, _ = self._held_then_released(store, show, a)
        self.assertEqual([e.outcome for e in _tick(store, now, fire_after_hold=True)], ["fired"])
        self.assertEqual(on_air(store, show), a)

    def test_held_past_grace_is_missed_even_with_policy(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        now, _ = self._held_then_released(store, show, a, after=60)
        self.assertEqual([e.outcome for e in _tick(store, now, fire_after_hold=True)], ["missed"])


class Failing(unittest.TestCase):
    def test_failed_cue_rewinds_and_records_the_reason(self):
        store = Store()
        show, (a, b) = _show(store, [[], [("overlay.take", "on"), ("stream.start", "")]])
        set_schedule(store, a, T0)
        set_schedule(store, b, T0 + timedelta(seconds=60))
        now = Now()
        _tick(store, now)
        before_a = _state(store, a)
        now.advance(60)
        room = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        events = _tick(store, now, room=room)
        self.assertEqual([e.outcome for e in events], ["failed"])
        self.assertIn("stream.start", events[0].note)
        self.assertEqual(on_air(store, show), a)
        self.assertEqual(_state(store, a)["as_run_in"], before_a["as_run_in"])
        st = _state(store, b)
        self.assertEqual((st["status"], st["schedule_state"], st["as_run_in"]), ("ready", "failed", None))
        self.assertIn("stream.start", st["schedule_note"])
        now.advance(5)
        self.assertEqual(_tick(store, now, room=room), [])  # not retried by the clock

    def test_refused_take_is_recorded(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        asset = store.conn.execute("SELECT id FROM asset").fetchone()["id"]
        store.set_cleared(asset, False)
        set_schedule(store, a, T0)
        events = _tick(store, Now())
        self.assertEqual([e.outcome for e in events], ["failed"])
        self.assertEqual(_state(store, a)["status"], "ready")
        self.assertIsNotNone(_state(store, a)["schedule_note"])


class Racing(unittest.TestCase):
    def test_two_clocks_fire_an_item_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.db")
            first = Store(path)
            show, (a,) = _show(first, [[("overlay.take", "on")]])
            set_schedule(first, a, T0)
            rooms = [FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}]) for _ in range(2)]
            out = [None, None]
            barrier = threading.Barrier(2)

            def run(i):
                s = Store(path)
                barrier.wait()
                out[i] = tick(s, rooms[i], now=Now(), sleep=_no_sleep, attempts=2)
                s.close()

            threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            fired = [e for evs in out for e in evs if e.outcome == "fired"]
            self.assertEqual(len(fired), 1)
            self.assertEqual(sum(1 for r in rooms if r.calls), 1)  # only one room saw the cue
            self.assertEqual(on_air(first, show), a)
            first.close()


class Restart(unittest.TestCase):
    def test_state_survives_a_restart_and_nothing_refires(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.db")
            s1 = Store(path)
            show, (a, b) = _show(s1, [[], []])
            set_schedule(s1, a, T0)
            set_schedule(s1, b, T0 + timedelta(seconds=60))
            _tick(s1, Now())
            s1.close()
            s2 = Store(path)  # a new process
            self.assertEqual(_tick(s2, Now(T0 + timedelta(seconds=10))), [])  # a is not fired again
            self.assertEqual([e.outcome for e in _tick(s2, Now(T0 + timedelta(seconds=60)))], ["fired"])
            self.assertEqual(on_air(s2, show), b)
            s2.close()

    def test_dead_clock_claim_is_settled_never_retried(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        set_schedule(store, a, T0)
        set_schedule(store, b, T0)
        store.conn.execute(
            "UPDATE item SET schedule_state = 'claimed', schedule_claimed_at = ? WHERE id IN (?, ?)",
            ("2026-10-11T20:00:00Z", a, b))
        store.conn.execute("UPDATE item SET status = 'on air' WHERE id = ?", (a,))
        store.conn.commit()
        self.assertEqual(settle_stale_claims(store, T0 + timedelta(seconds=10)), [])  # lease not up: leave it
        events = {e.item_id: e for e in settle_stale_claims(store, T0 + timedelta(seconds=400))}
        self.assertEqual(events[a].outcome, "fired")   # on air, no unfired cues
        self.assertEqual(events[b].outcome, "failed")  # never reached the air: say so, do not retake
        self.assertEqual(_tick(store, Now(T0 + timedelta(seconds=400))), [])


class Time(unittest.TestCase):
    def test_offsets_normalise_to_utc(self):
        # 2026-11-01 01:30 America/New_York happens twice; the offset names which one.
        self.assertEqual(to_utc_text("2026-11-01T01:30:00-04:00"), "2026-11-01T05:30:00Z")
        self.assertEqual(to_utc_text("2026-11-01T01:30:00-05:00"), "2026-11-01T06:30:00Z")

    def test_naive_time_is_refused(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        with self.assertRaises(ValueError):
            set_schedule(store, a, datetime(2026, 11, 1, 1, 30))
        with self.assertRaises(ValueError):
            set_schedule(store, a, "2026-11-01T01:30:00")

    def test_fires_across_the_dst_fall_back_by_instant(self):
        store = Store()
        show, (a, b) = _show(store, [[], []])
        set_schedule(store, a, "2026-11-01T01:30:00-04:00")  # 05:30Z
        set_schedule(store, b, "2026-11-01T01:30:00-05:00")  # 06:30Z, same wall time an hour later
        now = Now(datetime(2026, 11, 1, 5, 29, 59, tzinfo=timezone.utc))
        self.assertEqual(_tick(store, now), [])
        now.at = datetime(2026, 11, 1, 5, 30, 0, tzinfo=timezone.utc)
        self.assertEqual([e.item_id for e in _tick(store, now)], [a])
        now.at = datetime(2026, 11, 1, 6, 29, 59, tzinfo=timezone.utc)
        self.assertEqual(_tick(store, now), [])
        now.at = datetime(2026, 11, 1, 6, 30, 0, tzinfo=timezone.utc)
        self.assertEqual([e.item_id for e in _tick(store, now)], [b])

    def test_non_utc_aware_now_is_compared_as_an_instant(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0)
        plus9 = timezone(timedelta(hours=9))
        self.assertEqual([e.outcome for e in _tick(store, Now(T0.astimezone(plus9)))], ["fired"])

    def test_schedule_rows_report_outcome(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        set_schedule(store, a, T0)
        _tick(store, Now())
        self.assertEqual(schedule_rows(store, show)[0]["state"], "fired")

DSN = os.environ.get("VOLT_TEST_PG_DSN")


@unittest.skipUnless(DSN, "set VOLT_TEST_PG_DSN to run the live Postgres tests")
class LivePostgres(unittest.TestCase):
    def setUp(self):
        import uuid
        from urllib.parse import quote

        import psycopg

        self.schema = "volt_clk_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        sep = "&" if "?" in DSN else "?"
        self.dsn = f"{DSN}{sep}options={quote('-csearch_path=' + self.schema)}"
        self.stores = []
        self.addCleanup(self._drop)

    def _drop(self):
        import psycopg

        for st in self.stores:
            st.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def store(self):
        st = Store(self.dsn)
        self.stores.append(st)
        return st

    def test_fires_fails_and_misses_on_postgres(self):
        store = self.store()
        show, (a, b, c) = _show(store, [[], [("stream.start", "")], []])
        set_schedule(store, a, "2026-10-11T22:00:00+02:00")  # == 20:00Z
        set_schedule(store, b, T0 + timedelta(seconds=60))
        now = Now()
        self.assertEqual([e.outcome for e in _tick(store, now)], ["fired"])
        now.advance(60)
        room = FakeRoom([obs("idle")], senders=[["requested"]])
        self.assertEqual([e.outcome for e in _tick(store, now, room=room)], ["failed"])
        self.assertEqual(on_air(store, show), a)
        set_schedule(store, c, T0)
        now.advance(100)
        self.assertEqual([e.outcome for e in _tick(store, now)], ["missed"])
        self.assertEqual(_state(store, a)["as_run_in"], "2026-10-11T20:00:00Z")

    def test_two_clocks_fire_an_item_once_on_postgres(self):
        first = self.store()
        show, (a,) = _show(first, [[("overlay.take", "on")]])
        set_schedule(first, a, T0)
        rooms = [FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}]) for _ in range(2)]
        out = [None, None]
        barrier = threading.Barrier(2)

        def run(i):
            st = self.store()
            barrier.wait()
            out[i] = tick(st, rooms[i], now=Now(), sleep=_no_sleep, attempts=2)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([e for evs in out for e in evs if e.outcome == "fired"]), 1)
        self.assertEqual(sum(1 for r in rooms if r.calls), 1)


if __name__ == "__main__":
    unittest.main()
