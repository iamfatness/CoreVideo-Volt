import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_dispatch import _statuses
from tests.test_playout import Clock, _row
from tests.test_rooms import _no_sleep, obs
from unittest import mock

from volt.cues import add_cue, cues
from volt.library import index_words
from volt.playout import PlayoutError, TakingError, go, hold, on_air, release
from volt.rooms import FakeRoom
from volt.rundown import add_item, attach, kill, open_show
from volt.store import Store
from volt.take import take_item


def _show(store, cues_per_item):
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    index_words(store, asset.id, [(f"w{n}", f"01:00:0{n}:00", f"01:00:0{n}:08") for n in range(len(cues_per_item))])
    show = open_show(store, "Final Four")
    ids = []
    for n, span in enumerate(store.conn.execute("SELECT id FROM span ORDER BY tc_in").fetchall()):
        item = add_item(store, show, f"item-{n}")
        attach(store, item, span["id"])
        for command, payload in cues_per_item[n]:
            add_cue(store, item, "corevideo.pro", command, payload)
        ids.append(item)
    return show, ids


class TakeWithCues(unittest.TestCase):
    def test_take_item_is_taking_during_dispatch(self):
        # Production change that would fail this: take_item going on air before the room, or leaving on_air() true.
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "volt.db")
            setup = Store(path)
            show, (a,) = _show(setup, [[("overlay.take", "on")]])
            setup.close()
            entered, gate = threading.Event(), threading.Event()

            class SlowRoom(FakeRoom):
                def invoke(self, action, args):
                    entered.set()
                    gate.wait(10)
                    super().invoke(action, args)

            room = SlowRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
            out = {}

            def run():
                writer = Store(path)
                try:
                    out["result"] = take_item(writer, room, show, clock=Clock(), sleep=_no_sleep)
                finally:
                    writer.close()

            t = threading.Thread(target=run)
            t.start()
            self.assertTrue(entered.wait(10))
            other = Store(path)
            try:
                self.assertEqual(_row(other, a)["status"], "taking")
                self.assertIsNone(_row(other, a)["as_run_in"])
                self.assertIsNone(on_air(other, show))
                with self.assertRaises(TakingError) as ctx:
                    go(other, show, Clock())
                self.assertEqual(ctx.exception.item_id, a)
                gate.set()
                t.join(10)
                self.assertFalse(t.is_alive())
                self.assertTrue(out["result"].advanced)
                self.assertEqual(_row(other, a)["status"], "on air")
            finally:
                gate.set()
                t.join(10)
                other.close()

    def test_failed_cue_rewinds_from_taking(self):
        # Production change that would fail this: dispatch seeing on air, or rewind leaving taking/on air.
        store = Store()
        show, (a,) = _show(store, [[("audio.snapshot", "walk-in")]])
        during = []

        def during_dispatch(st, room, item_id, **kw):
            during.append(_row(st, item_id)["status"])
            from volt.dispatch import dispatch_item
            return dispatch_item(st, room, item_id, **kw)

        with mock.patch("volt.take.dispatch_item", during_dispatch):
            result = take_item(store, FakeRoom([obs("idle")]), show, clock=Clock(), sleep=_no_sleep)
        self.assertEqual(during, ["taking"])
        self.assertFalse(result.advanced)
        self.assertEqual(_row(store, a)["status"], "ready")
        self.assertIsNone(on_air(store, show))
        self.assertIsNone(_row(store, a)["as_run_in"])
        self.assertEqual(_statuses(store, a), ["armed"])

    def test_success_puts_the_item_on_air_and_fires_its_cues(self):
        store = Store()
        show, (a,) = _show(store, [[("overlay.take", "on")]])
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        result = take_item(store, room, show, clock=Clock(), sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertEqual(result.item_id, a)
        self.assertEqual(on_air(store, show), a)
        self.assertEqual(_statuses(store, a), ["fired"])
        self.assertIsNotNone(_row(store, a)["as_run_in"])

    def test_item_without_cues_just_airs(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        result = take_item(store, FakeRoom([obs("idle")]), show, clock=Clock(), sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertEqual(result.results, [])

    def test_failed_cue_rolls_the_rundown_back_and_keeps_the_cue_armed(self):
        store = Store()
        show, (a, b) = _show(store, [[], [("overlay.take", "on"), ("stream.start", "")]])
        clock = Clock()
        take_item(store, FakeRoom([obs("idle")]), show, clock=clock, sleep=_no_sleep)
        before_a = dict(_row(store, a))
        room = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        result = take_item(store, room, show, clock=clock, attempts=2, sleep=_no_sleep)
        self.assertFalse(result.advanced)
        self.assertEqual([r.status for r in result.results], ["fired", "failed"])
        self.assertEqual(on_air(store, show), a)
        self.assertEqual(dict(_row(store, a)), before_a)
        self.assertEqual(_row(store, b)["status"], "ready")
        self.assertIsNone(_row(store, b)["as_run_in"])
        self.assertEqual(_statuses(store, b), ["fired", "armed"])

    def test_replay_resumes_at_the_failed_cue(self):
        store = Store()
        show, (a,) = _show(store, [[("overlay.take", "on"), ("stream.start", "")]])
        clock = Clock()
        bad = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        self.assertFalse(take_item(store, bad, show, clock=clock, attempts=2, sleep=_no_sleep).advanced)
        good = FakeRoom([obs("idle")], senders=[["producing"]])
        result = take_item(store, good, show, clock=clock, sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertFalse(good.invoked("graphics.lowerThird.set", [True]))
        self.assertEqual(_statuses(store, a), ["fired", "fired"])
        self.assertEqual(on_air(store, show), a)

    def test_rollback_restores_killed_items_that_were_passed_over(self):
        store = Store()
        show, (a, b, c) = _show(store, [[], [], [("stream.start", "")]])
        kill(store, b)
        clock = Clock()
        take_item(store, FakeRoom([obs("idle")]), show, clock=clock, sleep=_no_sleep)
        bad = FakeRoom([obs("idle")], senders=[["requested"]])
        self.assertFalse(take_item(store, bad, show, clock=clock, attempts=2, sleep=_no_sleep).advanced)
        self.assertEqual(_row(store, b)["status"], "killed")

    def test_end_of_show_advances_with_nothing_to_dispatch(self):
        store = Store()
        show, (a,) = _show(store, [[]])
        clock = Clock()
        take_item(store, FakeRoom([obs("idle")]), show, clock=clock, sleep=_no_sleep)
        result = take_item(store, FakeRoom([obs("idle")]), show, clock=clock, sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertIsNone(result.item_id)


class TakeWhileHeld(unittest.TestCase):
    def test_held_take_dispatches_nothing_and_changes_nothing(self):
        store = Store()
        show, (a, b) = _show(store, [[("overlay.take", "on")], [("overlay.take", "off")]])
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        take_item(store, room, show, clock=Clock(), sleep=_no_sleep)
        calls = list(room.calls)
        before = (_row(store, a)["status"], _row(store, b)["status"], _statuses(store, b))
        hold(store, show, "technical")
        with self.assertRaises(PlayoutError):
            take_item(store, room, show, clock=Clock(), sleep=_no_sleep)
        self.assertEqual(room.calls, calls)
        self.assertEqual(
            (_row(store, a)["status"], _row(store, b)["status"], _statuses(store, b)), before
        )
        self.assertEqual(on_air(store, show), a)

    def test_release_then_take_fires_the_next_items_cues(self):
        store = Store()
        show, (a, b) = _show(store, [[], [("overlay.take", "on")]])
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        take_item(store, room, show, clock=Clock(), sleep=_no_sleep)
        hold(store, show, "technical")
        release(store, show)
        result = take_item(store, room, show, clock=Clock(), sleep=_no_sleep)
        self.assertTrue(result.advanced)
        self.assertEqual(result.item_id, b)
        self.assertEqual(_statuses(store, b), ["fired"])


if __name__ == "__main__":
    unittest.main()
