"""Safe mode, the registry, the event log and the poller (device-control spec, tasks 2). Fakes on loopback sockets only."""

import os
import types
import unittest
from unittest import mock

from tests.devicekit import FakeTcpServer
from tests.test_adapter_contract import CTX, ToyAdapter, device, spec
from tests.test_dispatch import _armed_item, _statuses
from tests.test_hyperdeck import FakeDeck
from tests.test_mos_send import FakeDevice
from tests.test_operator_api import Api
from tests.test_rooms import _no_sleep, obs
from volt import building, devices, hyperdeck, mos_send, roomreg, safety, serve
from volt.adapters import executor
from volt.adapters.base import SafeModeError
from volt.dispatch import dispatch_item
from volt.roomreg import COREVIDEO, RoomRegistry
from volt.rooms import FakeRoom
from volt.store import Store


def toy(srv_kw=None):
    srv = device(**(srv_kw or {}))
    ad = ToyAdapter(srv.port)
    ad.ACTIONS = {"toy.set": spec()}
    return srv, ad


class SafeMode(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_safe_mode_refuses_every_send_path(self):
        safety.set(self.store, True, "switcher misbehaving", "alice")
        srv, ad = toy()
        self.addCleanup(srv.close)
        out = executor.execute(ad, "t", spec(), {"n": 5}, CTX, store=self.store)
        self.assertEqual(out.status, "refused")
        sp = spec()
        sp = type(sp)(sp.command, sp.parse, sp.perform, sp.observed, True, False, prepare=sp.perform,
                      prepared=lambda now, p: None)
        self.assertEqual(executor.prepare(ad, "t", sp, {"n": 5}, CTX, store=self.store).status, "refused")
        self.assertEqual((srv.connections, srv.lines), (0, []))
        # the MOS sender connects to nothing
        mos_dev = FakeDevice()
        sender = mos_send.MosSender([mos_send.Device("d", "127.0.0.1", mos_dev.port)])
        sender.step(self.store)
        self.assertEqual((mos_dev.connections, mos_dev.messages), (0, []))
        self.assertEqual(mos_send.status(self.store)[0]["state"], "safe")
        # a HyperDeck handler sends no command
        deck = FakeDeck()
        room = hyperdeck.HyperDeckRoom({"A": ("127.0.0.1", deck.port)})
        cue = types.SimpleNamespace(id="c1", payload="A")
        with self.assertRaises(SafeModeError):
            hyperdeck.HANDLERS["deck.play"](self.store, room, cue, {"attempts": 1, "interval": 0, "sleep": _no_sleep})
        self.assertEqual(deck.commands, [])
        rows = devices.events(self.store)
        self.assertTrue(all(r["outcome"] == "refused" for r in rows), rows)
        self.assertIn("hyperdeck:A", {r["device"] for r in rows})

    def test_safe_mode_still_observes_and_polls(self):
        safety.set(self.store, True, "", "alice")
        srv, ad = toy()
        self.addCleanup(srv.close)
        devices.poll(self.store, RoomRegistry({"toy": ad}))
        st = devices.status(self.store)
        self.assertEqual([(d["kind"], d["name"], d["state"]) for d in st], [("toy", "t", "up")])
        self.assertEqual(st[0]["observed"], {"v": 0})
        self.assertEqual(srv.count("SET"), 0)

    def test_poll_marks_an_unreachable_device_down(self):
        srv, ad = toy({"refuse_connections": 99})
        self.addCleanup(srv.close)
        devices.poll(self.store, RoomRegistry({"toy": ad}))
        self.assertEqual(devices.status(self.store)[0]["state"], "down")

    def test_corevideo_cue_unaffected_by_safe_mode(self):
        safety.set(self.store, True, "", "alice")
        safety.check_send(self.store, COREVIDEO, None)       # never refused
        item = _armed_item(self.store, (COREVIDEO, "overlay.take", "on"))
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        results = dispatch_item(self.store, room, item, sleep=_no_sleep, attempts=2)
        self.assertEqual([r.status for r in results], ["fired"])

    def test_safe_mode_read_each_send_not_cached(self):
        srv, ad = toy()
        self.addCleanup(srv.close)
        run = lambda n: executor.execute(ad, "t", spec(), {"n": n}, CTX, store=self.store).status  # noqa: E731
        safety.set(self.store, True, "", "a")
        self.assertEqual(run(3), "refused")
        safety.set(self.store, False, "", "a")
        self.assertEqual(run(4), "fired")
        safety.set(self.store, True, "", "a")
        self.assertEqual(run(5), "refused")
        self.assertEqual(srv.count("SET"), 1)

    def test_refused_cue_is_advisory_and_take_proceeds(self):
        srv, ad = toy()
        self.addCleanup(srv.close)
        safety.set(self.store, True, "", "a")
        item = _armed_item(self.store, ("toy", "toy.set", "5"), (COREVIDEO, "overlay.take", "on"))
        room = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])
        results = dispatch_item(self.store, RoomRegistry({"toy": ad, COREVIDEO: room}), item, sleep=_no_sleep, attempts=2)
        self.assertEqual([(r.status, r.advisory) for r in results], [("failed", True), ("fired", False)])
        self.assertIn("safe mode", results[0].detail)
        self.assertEqual(srv.lines, [])
        self.assertEqual(_statuses(self.store, item), ["armed", "fired"])

    def test_every_send_has_an_event_row(self):
        srv, ad = toy()
        self.addCleanup(srv.close)
        executor.execute(ad, "t", spec(), {"n": 5}, CTX, store=self.store)            # fired
        executor.execute(ad, "t", spec(), {"n": 9}, {**CTX, "attempts": 1}, store=self.store, gate=lambda k, s: None)
        safety.set(self.store, True, "", "a")
        executor.execute(ad, "t", spec(), {"n": 6}, CTX, store=self.store)            # refused
        rows = devices.events(self.store)
        self.assertEqual(sorted(r["outcome"] for r in rows), ["fired", "fired", "refused"])
        self.assertTrue(all(r["device"] == "toy:t" and r["command"] == "toy.set" for r in rows))


class Registry(unittest.TestCase):
    def test_nothing_configured_nothing_registered_nothing_shown(self):
        store = Store()
        self.addCleanup(store.close)
        self.assertEqual(roomreg.from_env({}).names(), [])
        devices.poll(store, roomreg.from_env({}))
        self.assertEqual(devices.status(store), [])
        with mock.patch.dict(os.environ, {}, clear=False):
            for var in ("VOLT_CASPAR_HOSTS", "VOLT_HYPERDECK_HOSTS"):
                os.environ.pop(var, None)
            self.assertEqual([c for c in building.cue_commands() if c["room"] == "casparcg"], [])

    def test_configured_true_for_device_only_env(self):
        env = {"VOLT_CASPAR_HOSTS": "gfx=127.0.0.1"}
        self.assertTrue(roomreg.configured(env))
        self.assertEqual(roomreg.from_env(env).names(), ["casparcg"])

    def test_cue_commands_lists_only_configured_kinds(self):
        with mock.patch.dict(os.environ, {"VOLT_CASPAR_HOSTS": "gfx=127.0.0.1"}):
            rooms = {c["room"] for c in building.cue_commands()}
        self.assertIn("casparcg", rooms)
        self.assertIn(COREVIDEO, rooms)


class Route(Api):
    def test_safe_mode_route_role_and_audit(self):
        self.assertEqual(serve.ROUTE_ROLES["act_device_safe_mode"], "operator")
        s, p = self.post("/api/devices/safe-mode", {"on": True, "reason": "camera 2 cable"})
        self.assertEqual(s, 200, p)
        self.assertTrue(p["safeMode"]["on"])
        self.assertTrue(safety.is_on(self.store))
        s, g, _ = self.call("GET", "/api/devices")
        self.assertEqual((s, g["safeMode"]["reason"], g["devices"]), (200, "camera 2 cable", []))
        audit = self.store.conn.execute("SELECT action, target_kind FROM audit").fetchall()
        self.assertIn(("safe_mode", "device"), [(r["action"], r["target_kind"]) for r in audit])
        s, p = self.post("/api/devices/safe-mode", {"on": "yes"})
        self.assertEqual(s, 422)
        s, p = self.post("/api/devices/safe-mode", {"on": False})
        self.assertFalse(p["safeMode"]["on"])

    def test_state_carries_devices_and_safe_mode(self):
        s, state, _ = self.call("GET", "/api/state")
        self.assertEqual(s, 200)
        self.assertEqual((state["devices"], state["safeMode"]["on"]), ([], False))


if __name__ == "__main__":
    unittest.main()
