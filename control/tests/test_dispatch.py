import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_rooms import obs, _no_sleep
from volt.cues import add_cue, arm, cues
from volt.dispatch import dispatch_item
from volt.library import index_words
from volt.rooms import FakeRoom
from volt.rundown import add_item, attach, open_show
from volt.store import Store


def _armed_item(store, *cue_specs):
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    index_words(store, asset.id, [("steal", "01:12:08:12", "01:12:09:00")])
    span_id = store.conn.execute("SELECT id FROM span").fetchone()["id"]
    item_id = add_item(store, open_show(store, "Final Four"), "Steal")
    attach(store, item_id, span_id)
    for room, command, payload in cue_specs:
        add_cue(store, item_id, room, command, payload)
    arm(store, item_id)
    return item_id


def _statuses(store, item_id):
    return [c.status for c in cues(store, item_id)]


class Dispatch(unittest.TestCase):
    def test_unsupported_command_stays_armed_and_sends_nothing(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "audio.snapshot", "walk-in"))
        room = FakeRoom([obs("idle")])
        results = dispatch_item(store, room, item, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["unsupported"])
        self.assertEqual(_statuses(store, item), ["armed"])
        self.assertEqual(room.calls, [])

    def test_other_rooms_are_unsupported_until_they_have_an_adapter(self):
        store = Store()
        item = _armed_item(store, ("vmix", "Input", "2"))
        results = dispatch_item(store, FakeRoom([obs("idle")]), item, sleep=_no_sleep)
        self.assertEqual(results[0].status, "unsupported")
        self.assertEqual(_statuses(store, item), ["armed"])

    def test_stream_start_fires_only_when_a_sender_is_observed_producing(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "stream.start", ""))
        room = FakeRoom([obs("idle")], senders=[["requested"], ["preparing"], ["producing"]])
        results = dispatch_item(store, room, item, sleep=_no_sleep)
        self.assertEqual(results[0].status, "fired")
        self.assertTrue(room.invoked("transport.stream.set", [True]))
        self.assertEqual(_statuses(store, item), ["fired"])

    def test_stream_ack_without_output_stays_armed(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "stream.start", ""))
        room = FakeRoom([obs("idle")], senders=[["requested"]])
        results = dispatch_item(store, room, item, attempts=3, sleep=_no_sleep)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(_statuses(store, item), ["armed"])

    def test_overlay_take_verifies_lower_third_on_air(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "overlay.take", "on"))
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": False}, {"lowerThirdOnAir": True}])
        results = dispatch_item(store, room, item, sleep=_no_sleep)
        self.assertEqual(results[0].status, "fired")
        self.assertTrue(room.invoked("graphics.lowerThird.set", [True]))

    def test_overlay_never_reaching_air_fails(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "overlay.take", "on"))
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": False}])
        results = dispatch_item(store, room, item, attempts=3, sleep=_no_sleep)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(_statuses(store, item), ["armed"])

    def test_record_start_cue_mints_through_the_lifecycle(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "record.start", "iso-3"))
        room = FakeRoom([obs("preparing"), obs("producing", path="C:/iso/a.mp4")])
        results = dispatch_item(store, room, item, owner="desk", sleep=_no_sleep)
        self.assertEqual(results[0].status, "fired")
        asset = store.find_by_source("cv:s1")
        self.assertEqual(asset.camera, "iso-3")
        self.assertEqual(_statuses(store, item), ["fired"])

    def test_a_failure_stops_later_cues_and_replay_skips_fired_ones(self):
        store = Store()
        item = _armed_item(
            store,
            ("corevideo.pro", "overlay.take", "on"),
            ("corevideo.pro", "stream.start", ""),
            ("corevideo.pro", "overlay.take", "off"),
        )
        room = FakeRoom([obs("idle")], senders=[["requested"]], states=[{"lowerThirdOnAir": True}])
        results = dispatch_item(store, room, item, attempts=2, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["fired", "failed"])
        self.assertEqual(_statuses(store, item), ["fired", "armed", "armed"])
        room2 = FakeRoom([obs("idle")], senders=[["producing"]], states=[{"lowerThirdOnAir": False}])
        results = dispatch_item(store, room2, item, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["fired", "fired"])
        self.assertFalse(room2.invoked("graphics.lowerThird.set", [True]))
        self.assertEqual(_statuses(store, item), ["fired"] * 3)

    def test_unreachable_room_leaves_cue_armed(self):
        store = Store()
        item = _armed_item(store, ("corevideo.pro", "stream.start", ""))

        class Down(FakeRoom):
            def invoke(self, action, args):
                from volt.rooms import LiveError
                raise LiveError("core unreachable")

        results = dispatch_item(store, Down([obs("idle")]), item, sleep=_no_sleep)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(_statuses(store, item), ["armed"])


if __name__ == "__main__":
    unittest.main()
