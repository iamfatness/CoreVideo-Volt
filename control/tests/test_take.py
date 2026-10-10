import unittest

from tests.test_dispatch import _statuses
from tests.test_playout import Clock, _row
from tests.test_rooms import _no_sleep, obs
from volt.cues import add_cue, cues
from volt.library import index_words
from volt.playout import go, on_air
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


if __name__ == "__main__":
    unittest.main()
