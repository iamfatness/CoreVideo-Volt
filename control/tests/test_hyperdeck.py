"""The HyperDeck room against a fake deck that speaks the same text protocol over a loopback socket. Proves framing, the
fired-on-observation rule, refusals, timeouts, and that a failed deck cue never rewinds a take. It does NOT prove behaviour
on a real deck (clip numbering, timing, a deck that is recording or has no disk)."""
import socket
import threading
import unittest
from unittest import mock

from tests.test_dispatch import _armed_item, _statuses
from tests.test_operator_api import Api
from tests.test_rooms import _no_sleep, obs
from tests.test_take import _show
from volt import dispatch, hyperdeck, roomreg
from volt.cues import add_cue, arm, cues
from volt.dispatch import dispatch_item
from volt.hyperdeck import DeckError, HyperDeck, HyperDeckRoom, parse_hosts, parse_payload
from volt.playout import on_air
from volt.rooms import FakeRoom
from volt.roomreg import COREVIDEO, RoomRegistry
from volt.store import Store
from volt.take import take_item


class FakeDeck:
    """A deck with two clips. `plays_after` transport queries after `play`, the status becomes `play`; None means it never does."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.status, self.clip = "stopped", 1
        self.clips = [(1, "intro.mov"), (2, "bumper.mov")]
        self.commands = []
        self.refuse = {}            # command word -> reply line, e.g. {"play": "111 remote control disabled"}
        self.silent = set()         # command words that get no reply at all
        self.plays_after = 0
        self._pending = None
        self.greet = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        if self.greet:
            sock.sendall(b"500 connection info:\r\nprotocol version: 1.11\r\nmodel: Fake Deck\r\n\r\n")
        buf = b""
        while True:
            try:
                data = sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                self._handle(sock, line.decode())

    def _handle(self, sock, line):
        word = line.split(":")[0].strip()
        self.commands.append(line)
        if word in self.silent:
            return
        if word in self.refuse:
            sock.sendall(self.refuse[word].encode() + b"\r\n")
            return
        if word == "transport info":
            if self._pending is not None:
                self._pending -= 1
                if self._pending <= 0:
                    self.status, self._pending = "play", None
            sock.sendall(f"208 transport info:\r\nstatus: {self.status}\r\nspeed: 100\r\nclip id: {self.clip}\r\n\r\n".encode())
        elif word == "clips get":
            body = "".join(f"{i}: {n} 00:00:00:00 00:00:10:00\r\n" for i, n in self.clips)
            sock.sendall(f"205 clips info:\r\nclip count: {len(self.clips)}\r\n{body}\r\n".encode())
        elif word == "goto":
            self.clip = int(line.rsplit(":", 1)[1])
            sock.sendall(b"200 ok\r\n")
        elif word == "play":
            if self.plays_after is not None:
                self._pending = max(1, self.plays_after)
            sock.sendall(b"200 ok\r\n")
            sock.sendall(b"508 transport info:\r\nstatus: preview\r\n\r\n")   # an async notice in between: skipped
        elif word == "stop":
            self.status, self._pending = "stopped", None
            sock.sendall(b"200 ok\r\n")
        elif word == "record":
            self.status = "record"
            sock.sendall(b"200 ok\r\n")
        else:
            sock.sendall(b"100 syntax error\r\n")

    def close(self):
        self.server.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.deck = FakeDeck()
        self.addCleanup(self.deck.close)
        self.room = HyperDeckRoom({"A": ("127.0.0.1", self.deck.port)}, timeout=0.5)
        self.ctx = {"attempts": 5, "interval": 0, "sleep": _no_sleep}

    def cue(self, command, payload):
        return mock.Mock(command=command, payload=payload)

    def run_cue(self, command, payload):
        hyperdeck.HANDLERS[command](None, self.room, self.cue(command, payload), self.ctx)


class Parsing(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(parse_hosts(""), {})
        self.assertEqual(parse_hosts("A=10.0.0.9, B=deck2:9994"), {"A": ("10.0.0.9", 9993), "B": ("deck2", 9994)})
        for bad in ("A", "=h", "A=h:99999", "A=h:x", "A=h,A=i", "a b=h"):
            with self.assertRaises(ValueError, msg=bad):
                parse_hosts(bad)

    def test_payloads(self):
        self.assertEqual(parse_payload('{"deck": "A", "clip": 3}'), ("A", 3))
        self.assertEqual(parse_payload('{"deck": "A", "clip": "intro.mov"}'), ("A", "intro.mov"))
        self.assertEqual(parse_payload("A"), ("A", None))
        self.assertEqual(parse_payload("A/3"), ("A", 3))
        self.assertEqual(parse_payload("A/intro.mov"), ("A", "intro.mov"))
        for bad in ("", "{", '{"clip": 3}', '{"deck": "A", "x": 1}', '{"deck": "A", "clip": 0}', '{"deck": "A", "clip": true}', "[]", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                parse_payload(bad)


class Protocol(Base):
    def test_greeting_version_and_transport(self):
        with self.room.open("A") as d:
            self.assertEqual(d.version, "1.11")
            self.assertEqual(d.transport()["status"], "stopped")
            self.assertEqual(d.clips(), [(1, "intro.mov"), (2, "bumper.mov")])
            self.assertEqual(d.clip_id("bumper.mov"), 2)
            with self.assertRaises(DeckError):
                d.clip_id("nope.mov")

    def test_a_server_that_is_not_a_deck_is_refused(self):
        self.deck.greet = False
        silent = HyperDeckRoom({"A": ("127.0.0.1", self.deck.port)}, timeout=0.3)
        with self.assertRaises(DeckError):
            silent.open("A")

    def test_unreachable_and_unknown_decks_fail_fast_with_words(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        with self.assertRaises(DeckError) as ctx:
            HyperDeckRoom({"A": ("127.0.0.1", port)}).open("A")
        self.assertIn("not reachable", str(ctx.exception))
        with self.assertRaises(DeckError) as ctx:
            self.room.open("Z")
        self.assertIn("no deck named", str(ctx.exception))


class Cues(Base):
    def test_play_goes_to_the_clip_then_plays_and_is_fired_only_when_observed(self):
        self.deck.plays_after = 3          # the deck says 200 ok at once but only reports playing on the third look
        self.run_cue("deck.play", "A/2")
        self.assertEqual([c for c in self.deck.commands if not c.startswith("transport")], ["goto: clip id: 2", "play"])
        self.assertEqual(self.deck.status, "play")

    def test_play_that_never_shows_in_the_transport_state_is_not_fired(self):
        self.deck.plays_after = None
        with self.assertRaises(DeckError) as ctx:
            self.run_cue("deck.play", "A")
        self.assertIn("never reported playing", str(ctx.exception))

    def test_a_refusal_carries_the_decks_own_words(self):
        self.deck.refuse["play"] = "111 remote control disabled"
        with self.assertRaises(DeckError) as ctx:
            self.run_cue("deck.play", "A")
        self.assertIn("111 remote control disabled", str(ctx.exception))

    def test_a_deck_that_does_not_answer_times_out_instead_of_hanging(self):
        self.deck.silent.add("play")
        with self.assertRaises(DeckError) as ctx:
            self.run_cue("deck.play", "A")
        self.assertIn("did not answer", str(ctx.exception))

    def test_stop_goto_and_record(self):
        self.deck.status = "play"
        self.run_cue("deck.stop", "A")
        self.assertEqual(self.deck.status, "stopped")
        self.run_cue("deck.goto", "A/bumper.mov")
        self.assertEqual(self.deck.clip, 2)
        with self.assertRaises(DeckError):
            self.run_cue("deck.goto", "A")                    # a goto without a clip says so
        self.run_cue("deck.record.start", "A")
        self.assertEqual(self.deck.status, "record")
        self.run_cue("deck.record.stop", "A")
        self.assertEqual(self.deck.status, "stopped")

    def test_a_bad_payload_or_unknown_deck_is_a_failed_cue_with_a_reason(self):
        for payload in ("", "Z"):
            with self.assertRaises(DeckError):
                self.run_cue("deck.play", payload)


class Take(unittest.TestCase):
    """A deck cue that fails never rewinds the take and never stops the cues after it (owner ruling)."""

    def setUp(self):
        self.store = Store()
        self.deck = FakeDeck()
        self.addCleanup(self.deck.close)
        self.cv = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}])

    def show(self, *cue_specs):
        show, (item,) = _show(self.store, [[]])
        for room, command, payload in cue_specs:
            add_cue(self.store, item, room, command, payload)
        return show, item

    def registry(self, deck_ok=True):
        reg = RoomRegistry({COREVIDEO: self.cv})
        if deck_ok:
            reg.register(hyperdeck.ROOM, HyperDeckRoom({"A": ("127.0.0.1", self.deck.port)}, timeout=0.4))
        return reg

    def take(self, show, reg):
        return take_item(self.store, reg, show, sleep=_no_sleep, attempts=2, interval=0)

    def test_a_failed_deck_cue_leaves_the_item_on_air_red_and_the_later_cues_still_run(self):
        self.deck.refuse["play"] = "111 remote control disabled"
        show, item = self.show((COREVIDEO, "overlay.take", "on"), (hyperdeck.ROOM, "deck.play", "A"), (COREVIDEO, "overlay.take", "off"))
        self.cv = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": True}, {"lowerThirdOnAir": False}])
        out = self.take(show, self.registry())
        self.assertTrue(out.advanced)
        self.assertEqual(on_air(self.store, show), item)
        self.assertEqual([r.status for r in out.results], ["fired", "failed", "fired"])
        self.assertTrue(out.results[1].advisory)
        deck_cue = cues(self.store, item)[1]
        self.assertEqual((deck_cue.status, deck_cue.fail_status), ("armed", "failed"))
        self.assertIn("111", deck_cue.fail_detail)

    def test_a_deck_that_is_not_configured_is_unsupported_but_does_not_rewind(self):
        show, item = self.show((hyperdeck.ROOM, "deck.play", "A"))
        out = self.take(show, self.registry(deck_ok=False))
        self.assertTrue(out.advanced)
        self.assertEqual((out.results[0].status, out.results[0].advisory), ("unsupported", True))
        self.assertIn("not configured", out.results[0].detail)

    def test_a_good_deck_cue_fires(self):
        self.deck.plays_after = 1
        show, item = self.show((hyperdeck.ROOM, "deck.play", "A"))
        out = self.take(show, self.registry())
        self.assertEqual([r.status for r in out.results], ["fired"])
        self.assertEqual(cues(self.store, item)[0].status, "fired")

    def test_a_failed_corevideo_cue_still_rewinds_the_take(self):
        show, item = self.show((COREVIDEO, "overlay.take", "on"), (hyperdeck.ROOM, "deck.play", "A"))
        self.cv = FakeRoom([obs("idle")], states=[{"lowerThirdOnAir": False}])      # the lower third never shows
        out = self.take(show, self.registry())
        self.assertFalse(out.advanced)
        self.assertIsNone(on_air(self.store, show))


class Config(unittest.TestCase):
    def test_environment(self):
        reg = roomreg.from_env({"VOLT_HYPERDECK_HOSTS": "A=127.0.0.1"})
        self.assertEqual(reg.names(), ["hyperdeck"])
        self.assertTrue(roomreg.configured({"VOLT_HYPERDECK_HOSTS": "A=127.0.0.1"}))
        self.assertFalse(roomreg.configured({}))
        both = roomreg.from_env({"VOLT_ROOM_URL": "http://127.0.0.1:1", "VOLT_HYPERDECK_HOSTS": "A=h"})
        self.assertEqual(both.names(), ["corevideo.pro", "hyperdeck"])


class CueApi(Api):
    def setUp(self):
        super().setUp()
        self.show, (self.item,) = _show(self.store, [[]])

    def test_deck_commands_are_offered_only_when_a_deck_is_configured(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("VOLT_HYPERDECK_HOSTS", None)
            _, out, _ = self.call("GET", "/api/cue-commands")
            self.assertNotIn("hyperdeck", {c["room"] for c in out["commands"]})
        with mock.patch.dict("os.environ", {"VOLT_HYPERDECK_HOSTS": "A=127.0.0.1"}):
            _, out, _ = self.call("GET", "/api/cue-commands")
            self.assertEqual({c["command"] for c in out["commands"] if c["room"] == "hyperdeck"}, set(hyperdeck.DECK_COMMANDS))

    def test_a_deck_cue_is_stored_with_its_room_and_a_bad_payload_is_refused(self):
        status, out, _ = self.call("POST", f"/api/items/{self.item}/cues", {"room": "hyperdeck", "command": "deck.play", "payload": "A/2"})
        self.assertEqual((status, out["cue"]["room"]), (200, "hyperdeck"))
        for body in ({"room": "hyperdeck", "command": "deck.play", "payload": "{"}, {"room": "hyperdeck", "command": "deck.nope", "payload": "A"},
                     {"room": "elsewhere", "command": "x", "payload": ""}):
            status, out, _ = self.call("POST", f"/api/items/{self.item}/cues", body)
            self.assertEqual(status, 422, body)


if __name__ == "__main__":
    unittest.main()
