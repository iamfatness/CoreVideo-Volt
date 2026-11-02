"""The CasparCG adapter against a fake AMCP server on a loopback socket.

WHAT THIS PROVES: the AMCP framing (CRLF, 202/201/200/4xx replies) as read in the wiki, that a cue is fired only when INFO shows it
(never on a 202), that a bare PLAY is never sent twice, and that hostile payload text cannot inject a second command.
WHAT IT DOES NOT PROVE: the shape of a real server's INFO reply. The wiki does not document it, so the fake speaks a HYPOTHETICAL
layout named "fixture" that this file registers for the test run only; nothing is registered in production (volt/adapters/caspar.py).
"""

import re
import types
import unittest
from unittest import mock

from tests.devicekit import FakeTcpServer
from volt.adapters import caspar, executor
from volt.adapters.base import BadInput, CommandRefused, DeviceError, OutcomeUnknown
from volt.rooms import LiveError

CTX = {"attempts": 3, "interval": 0, "sleep": lambda s: None}
GATE = lambda kind, spec: None  # noqa: E731


def fixture_layout(text):
    """HYPOTHETICAL layout used only by these tests: <layer><fg>NAME</fg><bg>NAME</bg></layer>."""
    m = re.search(r"<fg>(.*?)</fg><bg>(.*?)</bg>", text)
    if not m:
        raise ValueError("not the fixture layout")
    return {"foreground": m.group(1) or None, "background": m.group(2) or None}


class FakeCaspar(FakeTcpServer):
    """An AMCP server holding one layer: fg and bg. Modes: ack_only (202 and no change), refuse (404), silent (no reply to PLAY)."""

    def __init__(self, ack_only=False, refuse=None, silent=(), apply_then_silent=(), **kw):
        super().__init__(self._on_line, greeting=b"", **kw)
        self.state.update(fg=None, bg=None)
        self.ack_only, self.refuse, self.silent, self.apply_then_silent = ack_only, refuse, set(silent), set(apply_then_silent)

    @staticmethod
    def _unquote(arg):
        return arg.strip('"')

    def _on_line(self, srv, line):
        word = line.split(" ")[0]
        st = self.state
        if word == "VERSION":
            return "201 VERSION OK\r\n2.3.3 LTS\r\n"
        if word == "INFO":
            return f"201 INFO OK\r\n<fg>{st['fg'] or ''}</fg><bg>{st['bg'] or ''}</bg>\r\n"
        if self.refuse and word in self.refuse:
            return self.refuse[word] + "\r\n"
        rest = line.split(" ", 2)[2] if line.count(" ") >= 2 else None
        if not self.ack_only and word not in self.silent:
            self._apply(word, self._unquote(rest) if rest else None)
        if word in self.apply_then_silent:
            self._apply(word, self._unquote(rest) if rest else None)
        if word in self.silent or word in self.apply_then_silent:
            return None
        return f"202 {word} OK\r\n"

    def _apply(self, word, arg):
        st = self.state
        if word == "LOADBG":
            st["bg"] = arg
        elif word == "PLAY":
            if arg:
                st["fg"], st["bg"] = arg, None
            else:
                st["fg"], st["bg"] = st["bg"], None
        elif word == "STOP":
            st["fg"] = None
        elif word == "CLEAR":
            st["fg"] = st["bg"] = None


class CasparCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(caspar.LAYOUTS, {"fixture": fixture_layout})
        patch.start()
        self.addCleanup(patch.stop)

    def serve(self, layout="fixture", **kw):
        srv = FakeCaspar(**kw)
        self.addCleanup(srv.close)
        return srv, caspar.CasparAdapter({"G": ("127.0.0.1", srv.port)}, layout=layout, timeout=0.3)

    def run_cmd(self, ad, command, payload, ctx=CTX):
        spec = ad.ACTIONS[command]
        return executor.execute(ad, "G", spec, spec.parse(payload), ctx, gate=GATE)


PAY = '{"server": "G", "channel": 1, "layer": 10, "clip": "NEWS/OPEN"}'
BARE = '{"server": "G", "channel": 1, "layer": 10}'


class Wire(CasparCase):
    def test_play_fired_only_when_info_shows_foreground(self):
        srv, ad = self.serve()
        out = self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(out.status, "fired", out.detail)
        self.assertEqual(srv.state["fg"], "NEWS/OPEN")
        self.assertIn("PLAY 1-10 NEWS/OPEN", srv.lines)

    def test_load_fired_when_background_shows(self):
        srv, ad = self.serve()
        self.assertEqual(self.run_cmd(ad, "caspar.load", PAY).status, "fired")
        self.assertEqual((srv.state["fg"], srv.state["bg"]), (None, "NEWS/OPEN"))

    def test_play_after_load_promotes_background(self):
        srv, ad = self.serve()
        self.run_cmd(ad, "caspar.load", PAY)
        out = self.run_cmd(ad, "caspar.play", BARE)
        self.assertEqual(out.status, "fired", out.detail)
        self.assertEqual(srv.state["fg"], "NEWS/OPEN")
        self.assertEqual(srv.lines.count("PLAY 1-10"), 1)

    def test_bare_play_with_nothing_loaded_is_failed(self):
        srv, ad = self.serve()
        out = self.run_cmd(ad, "caspar.play", BARE)
        self.assertEqual(out.status, "failed")
        self.assertIn("nothing is loaded", out.detail)

    def test_202_ok_but_info_unchanged_is_failed(self):
        srv, ad = self.serve(ack_only=True)
        out = self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(out.status, "failed")
        self.assertTrue(out.detail.startswith("timed out,"), out.detail)

    def test_4xx_reply_is_failed_with_server_words(self):
        srv, ad = self.serve(refuse={"PLAY": "404 PLAY ERROR"})
        out = self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(out.status, "failed")
        self.assertIn("404 PLAY ERROR", out.detail)
        self.assertEqual(srv.count("PLAY"), 1)               # a refusal is final: no resend, no recovery guess

    def test_bare_play_never_resent_after_timeout(self):
        srv, ad = self.serve(silent={"PLAY"})
        out = self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.detail, executor.UNKNOWN_TEXT)
        self.assertEqual(srv.count("PLAY"), 1)
        srv2, ad2 = self.serve(apply_then_silent={"PLAY"})
        self.assertEqual(self.run_cmd(ad2, "caspar.play", PAY).status, "fired")   # the state proves it: no resend either
        self.assertEqual(srv2.count("PLAY"), 1)

    def test_idempotent_stop_and_clear(self):
        srv, ad = self.serve()
        self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(self.run_cmd(ad, "caspar.stop", BARE).status, "fired")
        self.run_cmd(ad, "caspar.load", PAY)
        self.assertEqual(self.run_cmd(ad, "caspar.clear", BARE).status, "fired")
        self.assertEqual((srv.state["fg"], srv.state["bg"]), (None, None))

    def test_hostile_clip_names_cannot_inject_a_command(self):
        srv, ad = self.serve()
        for clip in ("A\r\nCLEAR 1", "A\nCLEAR 1", 'A" CLEAR 1', "A\x00B", "A\\B", " A"):
            with self.subTest(clip=clip):
                with self.assertRaises(ValueError):
                    ad.ACTIONS["caspar.play"].parse(f'{{"server": "G", "clip": {__import__("json").dumps(clip)}}}')
        self.assertEqual(srv.lines, [])
        with self.assertRaises(DeviceError):               # the session also refuses a line break, whatever called it
            caspar.CasparSession("G", "127.0.0.1", srv.port, "fixture").command("CLEAR 1\r\nCLEAR 2")
        self.assertEqual(srv.count("CLEAR"), 0)

    def test_a_clip_with_a_space_is_quoted(self):
        srv, ad = self.serve()
        self.run_cmd(ad, "caspar.play", '{"server": "G", "clip": "NEWS/LOWER THIRD"}')
        self.assertIn('PLAY 1-10 "NEWS/LOWER THIRD"', srv.lines)

    def test_unreachable_and_timeout_are_failed_not_hung(self):
        srv, ad = self.serve(silent={"INFO"})
        srv.on_line = lambda s, line: None                   # hears everything, answers nothing
        self.assertEqual(self.run_cmd(ad, "caspar.play", PAY).status, "failed")
        dead = caspar.CasparAdapter({"G": ("127.0.0.1", 1)}, layout="fixture", timeout=0.3)
        self.assertEqual(self.run_cmd(dead, "caspar.play", PAY).status, "failed")

    def test_info_for_unknown_version_layout_is_failed_not_guessed(self):
        for layout in (None, "some-other-version"):
            with self.subTest(layout=layout):
                srv, ad = self.serve(layout=layout)
                out = self.run_cmd(ad, "caspar.play", PAY)
                self.assertEqual(out.status, "failed")
                self.assertIn("layout", out.detail)
                self.assertEqual(srv.count("PLAY"), 0)       # failed closed before any command

    def test_info_that_does_not_match_the_layout_is_failed(self):
        srv, ad = self.serve()
        srv.on_line = lambda s, line: "201 INFO OK\r\n<totally><different/></totally>\r\n" if line.startswith("INFO") else "202 X OK\r\n"
        out = self.run_cmd(ad, "caspar.play", PAY)
        self.assertEqual(out.status, "failed")
        self.assertEqual(srv.count("PLAY"), 0)

    def test_poll_status_needs_no_layout(self):
        srv, ad = self.serve(layout=None)
        from volt import devices
        from volt.roomreg import RoomRegistry
        from volt.store import Store
        store = Store()
        self.addCleanup(store.close)
        devices.poll(store, RoomRegistry({"casparcg": ad}))
        st = devices.status(store)
        self.assertEqual((st[0]["kind"], st[0]["name"], st[0]["state"]), ("casparcg", "G", "up"))
        self.assertEqual(srv.count("PLAY"), 0)

    def test_handler_via_dispatch_shape_raises_liveerror_and_bad_input(self):
        srv, ad = self.serve(ack_only=True)
        handler = executor.handler_for(ad, ad.ACTIONS["caspar.play"])
        with self.assertRaises(LiveError):
            handler(None, ad, types.SimpleNamespace(id="c", payload=PAY), CTX)
        with self.assertRaises(BadInput):
            handler(None, ad, types.SimpleNamespace(id="c", payload="not json"), CTX)

    def test_parse_hosts(self):
        self.assertEqual(caspar.parse_hosts("G=10.0.0.5, H=gfx:5251"), {"G": ("10.0.0.5", 5250), "H": ("gfx", 5251)})
        self.assertEqual(caspar.parse_hosts(""), {})
        for bad in ("G", "=h", "G=h:x", "G=h,G=i"):
            with self.assertRaises(ValueError):
                caspar.parse_hosts(bad)


if __name__ == "__main__":
    unittest.main()
