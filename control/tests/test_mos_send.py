"""The MOS sender against a fake device (a loopback socket server). Proves framing, acknowledgement matching, change detection, restart
and reconnect behaviour and the no-blind-roCreate rule. It does NOT prove acceptance by any real MOS device or newsroom system."""
import socket
import threading
import unittest
import xml.etree.ElementTree as ET

from tests.test_operator_api import Api
from tests.test_take import _show
from volt import mos_send, serve
from volt.mos_send import Device, MosSender, Reader, frame, parse_devices
from volt.store import Store


def ack(ro_id, status="OK"):
    return f"<mos><mosID>d</mosID><ncsID>n</ncsID><messageID>0</messageID><roAck><roID>{ro_id}</roID><roStatus>{status}</roStatus></roAck></mos>"


class FakeDevice:
    """Accepts connections, records each message it is sent, and answers by `reply(kind, root)`: an XML string, or None for silence."""

    def __init__(self, reply=None):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.messages = []        # (kind, root) in arrival order
        self.reply = reply or (lambda kind, root: ack(root.findtext(f"{kind}/roID")) if kind.startswith("ro") else None)
        self.connections = 0
        self.drop_after = None    # close the socket after reading this many messages on a connection
        self.lock = threading.Lock()
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            self.conns.append(sock)
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        reader, seen = Reader(), 0
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            for text in reader.feed(data):
                root = ET.fromstring(text)
                kind = [c.tag for c in root if c.tag not in ("mosID", "ncsID", "messageID")][0]
                with self.lock:
                    self.messages.append((kind, root))
                seen += 1
                if self.drop_after is not None and seen >= self.drop_after:
                    sock.close()
                    return
                out = self.reply(kind, root)
                if out:
                    sock.sendall(frame(out))

    def push(self, text):
        for s in self.conns:
            try:
                s.sendall(frame(text))
            except OSError:
                pass

    def kinds(self):
        with self.lock:
            return [k for k, _ in self.messages if k != "heartbeat"]

    def close(self):
        self.server.close()
        for s in self.conns:
            try:
                s.close()
            except OSError:
                pass


def other_show(store, title):
    from volt.rundown import add_item, open_show
    show = open_show(store, title)
    add_item(store, show, "only item")
    return show


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.show, self.items = _show(self.store, [[], []])
        self.dev = FakeDevice()
        self.addCleanup(self.dev.close)
        self.t = [0.0]
        self.cfg = Device("gfx", "127.0.0.1", self.dev.port)
        self.sender = self.new_sender()

    def new_sender(self, **kw):
        kw.setdefault("ack_timeout", 0.4)
        return MosSender([self.cfg], ncs_id="volt.ncs", clock=lambda: self.t[0], **kw)

    def step(self, advance=0.0):
        self.t[0] += advance
        self.sender.step(self.store)

    def sync_row(self):
        return self.store.conn.execute("SELECT * FROM mos_sync WHERE device = 'gfx' AND show_id = ?", (self.show,)).fetchone()

    def device_state(self):
        r = self.store.conn.execute("SELECT state FROM mos_device WHERE device = 'gfx'").fetchone()
        return r["state"] if r else None


class Wire(unittest.TestCase):
    def test_frame_is_big_endian_unicode_with_no_declaration_or_bom(self):
        raw = frame('<?xml version="1.0" encoding="UTF-8"?>\n<mos><mosID>é</mosID></mos>')
        self.assertEqual(raw[:2], "<".encode("utf-16-be"))
        self.assertNotIn(b"\xff\xfe", raw[:2])
        self.assertNotIn("xml version", raw.decode("utf-16-be"))
        self.assertEqual(raw.decode("utf-16-be"), "<mos><mosID>é</mosID></mos>")

    def test_reader_splits_messages_across_chunks_and_odd_boundaries(self):
        a, b = frame("<mos><x>1</x></mos>"), frame("<mos><x>2</x></mos>")
        r = Reader()
        out = []
        blob = a + b
        for i in range(0, len(blob), 7):                  # an odd chunk size splits characters
            out += r.feed(blob[i:i + 7])
        self.assertEqual(out, ["<mos><x>1</x></mos>", "<mos><x>2</x></mos>"])

    def test_parse_devices(self):
        self.assertEqual(parse_devices(""), [])
        self.assertEqual(parse_devices(None), [])
        self.assertEqual(parse_devices("gfx=10.0.0.5, play=host:10555"), [Device("gfx", "10.0.0.5", 10541), Device("play", "host", 10555)])
        for bad in ("nohost", "=h", "a=h:99999", "a=h:x", "a=h,a=i", "a b=h"):
            with self.assertRaises(ValueError, msg=bad):
                parse_devices(bad)


class Syncing(Base):
    def test_first_send_is_a_roCreate_with_the_right_envelope_then_nothing_until_a_change(self):
        self.step()
        self.assertEqual(self.dev.kinds(), ["roCreate"])
        kind, root = self.dev.messages[0]
        self.assertEqual((root.findtext("mosID"), root.findtext("ncsID"), root.findtext("messageID")), ("gfx", "volt.ncs", "1"))
        self.assertEqual(root.findtext("roCreate/roID"), self.show)
        self.assertEqual(self.sync_row()["state"], "created")
        self.assertEqual(self.device_state(), "up")
        self.step(1.0)
        self.step(1.0)
        self.assertEqual(self.dev.kinds(), ["roCreate"])        # unchanged: nothing more is sent

    def test_a_change_is_sent_as_a_roReplace_with_the_next_message_id(self):
        self.step()
        self.store.conn.execute("UPDATE item SET slug = 'Renamed' WHERE id = ?", (self.items[1],))
        self.store.conn.commit()
        self.step(1.0)
        self.assertEqual(self.dev.kinds(), ["roCreate", "roReplace"])
        root = self.dev.messages[-1][1]
        self.assertEqual(root.findtext("messageID"), "2")
        self.assertIn("Renamed", ET.tostring(root, encoding="unicode"))

    def test_a_restart_resumes_the_counter_and_does_not_resend(self):
        self.step()
        self.sender = self.new_sender()                           # a new process, the same database
        self.step()
        self.assertEqual(self.dev.kinds(), ["roCreate"])
        self.store.conn.execute("UPDATE item SET slug = 'Later' WHERE id = ?", (self.items[0],))
        self.store.conn.commit()
        self.step(1.0)
        self.assertEqual(self.dev.messages[-1][1].findtext("messageID"), "2")

    def test_a_deleted_show_is_sent_a_roDelete_and_forgotten(self):
        self.step()
        self.store.conn.execute("DELETE FROM cue WHERE item_id IN (SELECT id FROM item WHERE show_id = ?)", (self.show,))
        self.store.conn.execute("DELETE FROM item WHERE show_id = ?", (self.show,))
        self.store.conn.execute("DELETE FROM show WHERE id = ?", (self.show,))
        self.store.conn.commit()
        self.step(1.0)
        self.assertEqual(self.dev.kinds(), ["roCreate", "roDelete"])
        self.assertEqual(self.dev.messages[-1][1].findtext("roDelete/roID"), self.show)
        self.assertIsNone(self.sync_row())

    def test_a_show_with_nothing_left_to_play_is_not_created_but_a_synced_one_stays_synced(self):
        done = other_show(self.store, "Done show")
        self.store.conn.execute("UPDATE item SET status = 'aired' WHERE show_id = ?", (done,))
        self.store.conn.commit()
        self.step()
        self.assertEqual([m[1].findtext("roCreate/roID") for m in self.dev.messages], [self.show])


class Refusals(Base):
    def test_an_error_roAck_to_a_roCreate_is_not_a_created_show_and_is_retried_later(self):
        self.dev.reply = lambda kind, root: ack(self.show, "ERROR: bad slug")
        self.step()
        self.assertIsNone(self.sync_row())                        # an explicit refusal: nothing exists on the device
        self.assertEqual(self.device_state(), "up")
        self.step(1.0)
        self.assertEqual(self.dev.kinds(), ["roCreate"])          # not before the pause
        self.dev.reply = lambda kind, root: ack(self.show)
        self.step(20.0)
        self.assertEqual(self.dev.kinds(), ["roCreate", "roCreate"])
        self.assertEqual(self.sync_row()["state"], "created")

    def test_an_error_roAck_to_a_roReplace_keeps_the_error_and_retries(self):
        self.step()
        self.dev.reply = lambda kind, root: ack(self.show, "ERROR: locked")
        self.store.conn.execute("UPDATE item SET slug = 'X' WHERE id = ?", (self.items[0],))
        self.store.conn.commit()
        self.step(1.0)
        self.assertIn("locked", self.sync_row()["error"])
        self.dev.reply = lambda kind, root: ack(self.show)
        self.step(20.0)
        self.assertEqual(self.dev.kinds(), ["roCreate", "roReplace", "roReplace"])
        self.assertIsNone(self.sync_row()["error"])


class Unconfirmed(Base):
    def test_a_roCreate_with_no_answer_is_never_resent_blind(self):
        self.dev.reply = lambda kind, root: None                  # a silent device
        self.step()
        self.assertEqual(self.sync_row()["state"], "unconfirmed")
        self.assertEqual(self.device_state(), "down")
        for _ in range(4):
            self.step(30.0)
        self.assertEqual(self.dev.kinds(), ["roCreate"])          # one send, however long we wait
        self.assertEqual(mos_send.status(self.store)[0]["unconfirmed"], [self.show])

    def test_the_desk_can_say_it_was_created_or_ask_for_a_retry(self):
        self.dev.reply = lambda kind, root: None
        self.step()
        self.dev.reply = lambda kind, root: ack(self.show)
        mos_send.resolve(self.store, "gfx", self.show, "created")
        self.step(30.0)
        self.assertEqual(self.dev.kinds(), ["roCreate", "roReplace"])   # taken to have it: the next send is a roReplace that aligns it
        self.assertEqual((self.sync_row()["state"], bool(self.sync_row()["acked_hash"])), ("created", True))
        # a second show: retry resends the roCreate
        other = other_show(self.store, "Second show")
        self.dev.reply = lambda kind, root: None
        self.step(30.0)
        self.assertEqual(self.store.conn.execute("SELECT state FROM mos_sync WHERE show_id = ?", (other,)).fetchone()["state"], "unconfirmed")
        self.dev.reply = lambda kind, root: ack(other)
        mos_send.resolve(self.store, "gfx", other, "retry")
        self.step(30.0)
        self.assertEqual(self.store.conn.execute("SELECT state FROM mos_sync WHERE show_id = ?", (other,)).fetchone()["state"], "created")

    def test_resolve_needs_an_unconfirmed_show_and_a_known_action(self):
        with self.assertRaises(KeyError):
            mos_send.resolve(self.store, "gfx", self.show, "created")
        self.dev.reply = lambda kind, root: None
        self.step()
        with self.assertRaises(ValueError):
            mos_send.resolve(self.store, "gfx", self.show, "maybe")


class Connection(Base):
    def test_a_connection_that_drops_during_a_roReplace_reconnects_and_resends(self):
        self.step()
        self.dev.drop_after = 1                                    # the next connection dies after one message
        self.dev.conns[0].close()
        self.store.conn.execute("UPDATE item SET slug = 'Y' WHERE id = ?", (self.items[0],))
        self.store.conn.commit()
        self.step(1.0)                                             # the dead socket fails the send
        self.assertEqual(self.device_state(), "down")
        self.dev.drop_after = None
        self.step(10.0)                                            # after the backoff it reconnects
        self.assertEqual(self.device_state(), "up")
        self.assertEqual(self.dev.kinds()[-1], "roReplace")
        self.assertEqual(self.dev.connections, 2)
        self.assertEqual(self.sync_row()["state"], "created")

    def test_nothing_listening_is_refused_and_does_not_block(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        sender = MosSender([Device("gfx", "127.0.0.1", port)], clock=lambda: self.t[0], connect_timeout=0.3)
        sender.step(self.store)
        self.assertIn(self.device_state(), ("refused", "down"))   # Windows reports a closed local port as a timeout
        calls = []
        sender._connect = lambda d: calls.append(d) or (_ for _ in ()).throw(ConnectionRefusedError())
        sender.step(self.store)                                    # inside the backoff: no attempt at all
        self.assertEqual(calls, [])
        self.t[0] += 5.0
        sender.step(self.store)
        self.assertEqual(len(calls), 1)

    def test_a_device_heartbeat_is_answered_and_we_send_our_own(self):
        self.step()
        self.dev.push("<mos><mosID>gfx</mosID><ncsID>volt.ncs</ncsID><messageID>9</messageID><heartbeat><time>t</time></heartbeat></mos>")
        self.step(1.0)
        import time
        time.sleep(0.2)
        self.assertTrue(any(k == "heartbeat" for k, _ in self.dev.messages))
        before = sum(1 for k, _ in self.dev.messages if k == "heartbeat")
        self.step(40.0)                                            # past the interval: our own heartbeat
        time.sleep(0.2)
        self.assertGreater(sum(1 for k, _ in self.dev.messages if k == "heartbeat"), before)

    def test_off_by_default(self):
        self.assertEqual(parse_devices(""), [])
        sender = MosSender([], clock=lambda: 0.0)
        sender.step(self.store)                                    # nothing configured: nothing happens
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mos_device").fetchone()[0], 0)


class MosApi(Api):
    def setUp(self):
        super().setUp()
        self.show, _ = _show(self.store, [[]])
        self.store.conn.execute("INSERT INTO mos_device (device, state, detail, updated_at) VALUES ('gfx', 'up', '', 'x')")
        self.store.conn.execute("INSERT INTO mos_sync (device, show_id, state, updated_at) VALUES ('gfx', ?, 'unconfirmed', 'x')", (self.show,))
        self.store.conn.commit()

    def test_state_carries_the_devices_and_the_resolve_route_works(self):
        _, state, _ = self.call("GET", "/api/state")
        self.assertEqual(state["mos"], [{"name": "gfx", "state": "up", "detail": "", "unconfirmed": [self.show]}])
        status, out, _ = self.call("POST", f"/api/mos/gfx:{self.show}/resolve", {"action": "retry"})
        self.assertEqual((status, out["ok"]), (200, True))
        _, state, _ = self.call("GET", "/api/state")
        self.assertEqual(state["mos"][0]["unconfirmed"], [])
        self.assertEqual(serve.ROUTE_ROLES["act_mos_resolve"], "operator")

    def test_bad_resolve_requests(self):
        status, out, _ = self.call("POST", f"/api/mos/gfx:{self.show}/resolve", {"action": "maybe"})
        self.assertEqual((status, out["error"]["code"]), (422, "bad_input"))
        status, out, _ = self.call("POST", "/api/mos/gfx:nope/resolve", {"action": "retry"})
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))
        status, out, _ = self.call("POST", "/api/mos/nocolon/resolve", {"action": "retry"})
        self.assertEqual((status, out["error"]["code"]), (422, "bad_input"))


if __name__ == "__main__":
    unittest.main()
