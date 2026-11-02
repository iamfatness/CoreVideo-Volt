"""The adapter contract and executor, against a toy line protocol on a loopback socket (tests/devicekit.py).

Toy wire: on connect the device says HELLO. `SET n` sets v (reply OK); `GET` answers `V <v>`. The adapter's observation is v, never the OK.
"""

import socket
import time
import types
import unittest

from tests.devicekit import DROP, FakeTcpServer
from volt.adapters import executor
from volt.adapters.base import (ActionSpec, BadInput, DeviceError, InterlockError, NotSent, Observation, OutcomeUnknown, SafeModeError)
from volt.rooms import LiveError

CTX = {"attempts": 3, "interval": 0, "sleep": lambda s: None}


class ToySession:
    def __init__(self, port, timeout):
        try:
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        except OSError as exc:
            raise DeviceError(f"toy not reachable: {exc}") from None
        self.sock.settimeout(timeout)
        self.buf = b""
        if self.line() != "HELLO":
            raise DeviceError("toy did not greet")

    def line(self) -> str:
        while b"\r\n" not in self.buf:
            try:
                data = self.sock.recv(1024)
            except OSError as exc:
                raise DeviceError(f"toy did not answer: {exc}") from None
            if not data:
                raise DeviceError("toy closed the connection")
            self.buf += data
        out, _, self.buf = self.buf.partition(b"\r\n")
        return out.decode()

    def send(self, text):
        try:
            self.sock.sendall(text.encode() + b"\r\n")
        except OSError as exc:
            raise NotSent(str(exc)) from None

    def observe(self):
        try:
            self.sock.sendall(b"GET\r\n")
        except OSError as exc:
            raise DeviceError(f"toy connection failed: {exc}") from None
        return Observation(time.monotonic(), {"v": int(self.line().split()[1])})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _set(session, params):
    session.send(f"SET {params['n']}")
    session.line()                        # the OK: read and then ignored as proof


def _parse(text):
    try:
        return {"n": int(text)}
    except ValueError:
        raise ValueError("write a number") from None


def _observed(base, now, params):
    return None if now.state["v"] == params["n"] else f"v is {now.state['v']}"


class ToyAdapter:
    kind = "toy"

    def __init__(self, port, timeout=0.3):
        self.port, self.timeout = port, timeout
        self.ACTIONS = {}

    def device_names(self):
        return ["t"]

    def device_of(self, params):
        return "t"

    def open(self, name):
        return ToySession(self.port, self.timeout)


def spec(idempotent=True, on_air=False, interlocks=()):
    return ActionSpec("toy.set", _parse, _set, _observed, idempotent, on_air, interlocks=interlocks)


def device(apply=True, drop_first_set=False, flip_back=False, silent_get=False, **kw):
    """A toy device. apply=False: it acks and changes nothing. drop_first_set: closes the connection on the first SET (applying it
    or not as `apply` says). flip_back: after a SET the second GET answers 0 again."""
    def on_line(srv, line):
        st = srv.state
        if line.startswith("SET"):
            st["sets"] = st.get("sets", 0) + 1
            if apply:
                st["v"], st["gets"] = int(line.split()[1]), 0
            if drop_first_set and st["sets"] == 1:
                return DROP
            return "OK\r\n"
        if line == "GET":
            if silent_get:
                return None
            st["gets"] = st.get("gets", 0) + 1
            v = 0 if flip_back and st.get("gets", 0) >= 2 else st.get("v", 0)
            return f"V {v}\r\n"
    srv = FakeTcpServer(on_line, **kw)
    srv.state.update(v=0, gets=0)
    return srv


class Contract(unittest.TestCase):
    def serve(self, **kw):
        srv = device(**kw)
        self.addCleanup(srv.close)
        return srv, ToyAdapter(srv.port)

    def run_set(self, adapter, **spec_kw):
        return executor.execute(adapter, "t", spec(**spec_kw), {"n": 5}, CTX, gate=lambda k, s: None)

    def test_ack_without_state_change_is_failed(self):
        srv, ad = self.serve(apply=False)
        out = self.run_set(ad)
        self.assertEqual(out.status, "failed")
        self.assertTrue(out.detail.startswith("timed out,"), out.detail)
        self.assertEqual(srv.count("SET"), 1)

    def test_proof_on_fresh_read_is_fired(self):
        _srv, ad = self.serve()
        self.assertEqual(self.run_set(ad).status, "fired")

    def test_on_air_needs_two_consecutive_proofs(self):
        _srv, ad = self.serve(flip_back=True)
        out = self.run_set(ad, on_air=True)
        self.assertEqual(out.status, "failed")
        self.assertTrue(out.detail.startswith("timed out,"))
        _srv2, ad2 = self.serve()
        self.assertEqual(self.run_set(ad2, on_air=True).status, "fired")

    def test_unreachable_is_failed_not_hung(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        started = time.monotonic()
        out = self.run_set(ToyAdapter(port))
        self.assertEqual(out.status, "failed")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_read_timeout_is_failed(self):
        _srv, ad = self.serve(silent_get=True)
        started = time.monotonic()
        out = self.run_set(ad)
        self.assertEqual(out.status, "failed")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_drop_before_send_reconnects_twice_then_fails(self):
        srv, ad = self.serve(refuse_connections=99)
        out = self.run_set(ad)
        self.assertEqual(out.status, "failed")
        self.assertEqual(srv.connections, 3)          # the first try and two reconnects
        self.assertEqual(srv.count("SET"), 0)

    def test_idempotent_drop_after_send_reobserves_then_resends_once(self):
        srv, ad = self.serve(apply=True, drop_first_set=True)
        out = self.run_set(ad, idempotent=True)       # the drop happened after the device applied it
        self.assertEqual(out.status, "fired")
        self.assertEqual(srv.count("SET"), 1)         # the state proved it: no resend
        srv2 = FakeTcpServer(lambda s, line: None)
        self.addCleanup(srv2.close)
        sets = []

        def on_line(s, line):                         # first SET is lost (not applied); the resend lands
            if line.startswith("SET"):
                sets.append(line)
                if len(sets) == 1:
                    return DROP
                s.state["v"] = int(line.split()[1])
                return "OK\r\n"
            return f"V {s.state.get('v', 0)}\r\n"
        srv2.on_line = on_line
        out = self.run_set(ToyAdapter(srv2.port), idempotent=True)
        self.assertEqual(out.status, "fired")
        self.assertEqual(len(sets), 2)

    def test_non_idempotent_drop_after_send_never_resends(self):
        srv, ad = self.serve(apply=False, drop_first_set=True)
        out = self.run_set(ad, idempotent=False)
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.detail, executor.UNKNOWN_TEXT)
        self.assertEqual(srv.count("SET"), 1)
        srv2, ad2 = self.serve(apply=True, drop_first_set=True)
        self.assertEqual(self.run_set(ad2, idempotent=False).status, "fired")
        self.assertEqual(srv2.count("SET"), 1)

    def test_interlock_refuses_before_any_byte(self):
        srv, ad = self.serve()
        out = self.run_set(ad, interlocks=(lambda adapter, params: "camera 1 is on program",))
        self.assertEqual(out.status, "interlocked")
        self.assertEqual((srv.connections, srv.lines), (0, []))

    def test_gate_refusal_sends_nothing(self):
        srv, ad = self.serve()

        def gate(kind, spec_):
            raise SafeModeError("safe mode is on; nothing sent")
        out = executor.execute(ad, "t", spec(), {"n": 5}, CTX, gate=gate)
        self.assertEqual(out.status, "refused")
        self.assertEqual((srv.connections, srv.lines), (0, []))

    def test_handler_for_has_dispatch_shape_and_raises_liveerror(self):
        srv, ad = self.serve(apply=False)
        handler = executor.handler_for(ad, spec())
        cue = types.SimpleNamespace(payload="5")
        with self.assertRaises(LiveError):
            handler(None, ad, cue, CTX)
        srv2, ad2 = self.serve()
        self.assertIsNone(executor.handler_for(ad2, spec())(None, ad2, cue, CTX))

    def test_handler_maps_refusals_to_their_error_types(self):
        srv, ad = self.serve()
        cue = types.SimpleNamespace(payload="5")
        with self.assertRaises(InterlockError):
            executor.handler_for(ad, spec(interlocks=(lambda a, p: "no",)))(None, ad, cue, CTX)
        srv2, ad2 = self.serve(apply=False, drop_first_set=True)
        with self.assertRaises(OutcomeUnknown):
            executor.handler_for(ad2, spec(idempotent=False))(None, ad2, cue, CTX)

    def test_parse_error_is_bad_input_not_failed(self):
        srv, ad = self.serve()
        with self.assertRaises(BadInput):
            executor.handler_for(ad, spec())(None, ad, types.SimpleNamespace(payload="x"), CTX)
        self.assertEqual(srv.connections, 0)


class Prepare(unittest.TestCase):
    def test_prepare_proves_with_prepared_and_never_marks_on_air(self):
        srv = device()
        self.addCleanup(srv.close)
        ad = ToyAdapter(srv.port)
        sp = ActionSpec("toy.pre", _parse, _set, _observed, True, True, prepare=_set,
                        prepared=lambda now, p: None if now.state["v"] == p["n"] else "not loaded")
        out = executor.prepare(ad, "t", sp, {"n": 4}, CTX, gate=lambda k, s: None)
        self.assertEqual(out.status, "prepared")


if __name__ == "__main__":
    unittest.main()
