import unittest
from unittest import mock

from tests.test_dispatch import _armed_item, _statuses
from tests.test_rooms import _no_sleep, obs
from volt import dispatch, roomreg
from volt.dispatch import dispatch_item
from volt.roomreg import COREVIDEO, RoomRegistry, as_registry
from volt.rooms import FakeRoom
from volt.store import Store


class Registry(unittest.TestCase):
    def test_a_single_room_is_wrapped_as_corevideo(self):
        room = FakeRoom([obs("idle")])
        reg = as_registry(room)
        self.assertIs(reg.get(COREVIDEO), room)
        self.assertEqual(reg.names(), [COREVIDEO])

    def test_a_registry_passes_through_and_none_is_empty(self):
        reg = RoomRegistry({"deck": object()})
        self.assertIs(as_registry(reg), reg)
        self.assertFalse(as_registry(None))
        self.assertIsNone(as_registry(None).get(COREVIDEO))

    def test_from_env(self):
        self.assertFalse(roomreg.from_env({}))
        self.assertFalse(roomreg.configured({}))
        reg = roomreg.from_env({"VOLT_ROOM_URL": "http://127.0.0.1:8011", "VOLT_ROOM_TOKEN": "t"})
        self.assertEqual(reg.names(), [COREVIDEO])
        self.assertTrue(roomreg.configured({"VOLT_ROOM_URL": "http://127.0.0.1:8011"}))


class Routing(unittest.TestCase):
    def test_a_cue_goes_to_its_own_rooms_client_and_handler(self):
        store = Store()
        item = _armed_item(store, ("testdeck", "deck.play", "A"), (COREVIDEO, "overlay.take", "on"))
        seen = []
        deck, cv = object(), FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        handlers = {"testdeck": {"deck.play": lambda st, room, cue, ctx: seen.append((room, cue.payload))}}
        with mock.patch.dict(dispatch.ROOM_HANDLERS, handlers):
            results = dispatch_item(store, RoomRegistry({"testdeck": deck, COREVIDEO: cv}), item, sleep=_no_sleep, attempts=2)
        self.assertEqual([r.status for r in results], ["fired", "fired"])
        self.assertEqual(seen, [(deck, "A")])
        self.assertEqual(_statuses(store, item), ["fired", "fired"])

    def test_a_room_with_no_client_is_unsupported_and_names_it(self):
        store = Store()
        item = _armed_item(store, ("testdeck", "deck.play", "A"))
        with mock.patch.dict(dispatch.ROOM_HANDLERS, {"testdeck": {"deck.play": lambda *a: None}}):
            results = dispatch_item(store, RoomRegistry(), item, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["unsupported"])
        self.assertIn("testdeck", results[0].detail)
        self.assertEqual(_statuses(store, item), ["armed"])

    def test_an_unknown_room_or_command_is_unsupported_as_before(self):
        store = Store()
        item = _armed_item(store, ("elsewhere", "x.y", ""))
        results = dispatch_item(store, FakeRoom([obs("idle")]), item, sleep=_no_sleep)
        self.assertEqual([r.status for r in results], ["unsupported"])
        self.assertIn("no verified adapter for elsewhere/x.y", results[0].detail)


if __name__ == "__main__":
    unittest.main()
