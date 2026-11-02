"""The MOS sender: Volt as the newsroom computer (NCS) sending running orders to MOS devices (docs/specs/mos.md).

Off by default. `VOLT_MOS_DEVICES=name=host[:port],...` turns it on; nothing is sent otherwise. Per MOS 2.8.5 the NCS connects to the
device's *upper port* (10541) for running-order messages, frames each message as Unicode in big-endian byte order (UCS-2, written here
as UTF-16BE with no byte-order mark), wraps it in `<mos><mosID/><ncsID/><messageID/>...</mos>`, and waits for a `roAck` on the same
socket. A `heartbeat` is answered with a `heartbeat`.

How a change reaches the device: nothing hooks the code that edits a rundown. Each step the sender builds the message for every live
show, hashes it (ignoring the message id) and compares it with the hash the device last acknowledged; a difference is sent as a
`roReplace`. So a lost connection loses nothing, a restart resumes where the database says it was, and ids and hashes live in the
database. The first send of a show is a `roCreate`; a show that has gone is sent a `roDelete`.

A `roCreate` is not idempotent, so one that was sent and not acknowledged (timeout, dropped connection) is NOT resent blind: the show
becomes `unconfirmed` and waits for the desk (`resolve`: "created" if the device has it, "retry" to send it again). `roReplace`
and `roDelete` are retried freely.

UNVALIDATED against a real MOS device or newsroom system. It is tested against a fake device socket server only.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from volt import mos
from volt.store import Store, _now

log = logging.getLogger("volt.mos_send")

DEFAULT_PORT = 10541          # the MOS upper port: where a device accepts connections from the NCS
HEARTBEAT_EVERY = 30.0
ACK_TIMEOUT = 10.0
CONNECT_TIMEOUT = 3.0
MAX_BACKOFF = 60.0
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class MosProtocolError(Exception):
    pass


# ---- configuration -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Device:
    name: str          # also the device's MOS id (the `mosID` in every message)
    host: str
    port: int = DEFAULT_PORT


def parse_devices(text: Optional[str]) -> list:
    """`name=host[:port],name2=host2` -> [Device]. Empty or unset is no devices (the sender is off). A bad entry is a ValueError."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, addr = part.partition("=")
        host, _, port = addr.strip().rpartition(":") if ":" in addr else (addr.strip(), "", "")
        if not sep or not _NAME.match(name.strip()) or not host:
            raise ValueError(f"VOLT_MOS_DEVICES entry {part!r}: write it as name=host or name=host:port")
        if port and not (port.isdigit() and 0 < int(port) < 65536):
            raise ValueError(f"VOLT_MOS_DEVICES entry {part!r}: the port must be a number")
        out.append(Device(name.strip(), host, int(port) if port else DEFAULT_PORT))
    if len({d.name for d in out}) != len(out):
        raise ValueError("VOLT_MOS_DEVICES names must be unique")
    return out


# ---- framing -------------------------------------------------------------------------------------------------

def frame(xml_text: str) -> bytes:
    """A message as it goes on the wire: no XML declaration (the bytes are not UTF-8), big-endian UTF-16, no byte-order mark."""
    text = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml_text)
    return text.strip().encode("utf-16-be")


class Reader:
    """Collects bytes and hands back complete `<mos>...</mos>` messages."""

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while True:
            even = self.buf[: len(self.buf) - (len(self.buf) % 2)]
            text = even.decode("utf-16-be", errors="replace")
            end = text.find("</mos>")
            if end < 0:
                break
            end += len("</mos>")
            out.append(text[:end].strip())
            self.buf = self.buf[end * 2:]   # the BMP only, so a character is two bytes
        return out


def parse(message: str):
    try:
        root = ET.fromstring(message)
    except ET.ParseError as exc:
        raise MosProtocolError(f"the device sent a message that is not XML: {exc}") from None
    if root.tag != "mos":
        raise MosProtocolError("the device sent a message with no <mos> root")
    return root


def heartbeat_message(mos_id: str, ncs_id: str, message_id: int) -> str:
    return (f"<mos><mosID>{mos_id}</mosID><ncsID>{ncs_id}</ncsID><messageID>{int(message_id)}</messageID>"
            "<heartbeat><time>" + _now() + "</time></heartbeat></mos>")


def delete_message(mos_id: str, ncs_id: str, message_id: int, ro_id: str) -> str:
    root = ET.Element("mos")
    for tag, text in (("mosID", mos_id), ("ncsID", ncs_id), ("messageID", str(int(message_id)))):
        ET.SubElement(root, tag).text = text
    ro = ET.SubElement(root, "roDelete")
    ET.SubElement(ro, "roID").text = ro_id
    return ET.tostring(root, encoding="unicode")


# ---- the sender ----------------------------------------------------------------------------------------------

@dataclass
class _Conn:
    sock: socket.socket
    reader: Reader = field(default_factory=Reader)
    last_beat: float = 0.0


class MosSender:
    def __init__(self, devices: list, ncs_id: str = "volt.ncs", ack_timeout: float = ACK_TIMEOUT,
                 connect_timeout: float = CONNECT_TIMEOUT, heartbeat_every: float = HEARTBEAT_EVERY,
                 clock=time.monotonic, connect=None):
        mos._check_id("ncs_id", ncs_id)
        self.devices, self.ncs_id = list(devices), ncs_id
        self.ack_timeout, self.connect_timeout, self.heartbeat_every = ack_timeout, connect_timeout, heartbeat_every
        self.clock = clock
        self._connect = connect or (lambda d: socket.create_connection((d.host, d.port), timeout=self.connect_timeout))
        self.conns: dict = {}
        self.fails: dict = {}
        self.retry_at: dict = {}
        self.show_retry: dict = {}

    # -- state the desk reads --
    def _set_state(self, store: Store, name: str, state: str, detail: str = "") -> None:
        store.conn.execute(
            """INSERT INTO mos_device (device, state, detail, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (device) DO UPDATE SET state = excluded.state, detail = excluded.detail, updated_at = excluded.updated_at""",
            (name, state, detail[:300], _now()))
        store.conn.commit()

    def _next_id(self, store: Store, name: str) -> int:
        with store.transaction():
            store.conn.execute("INSERT INTO mos_counter (device, next_id) VALUES (?, 1) ON CONFLICT (device) DO NOTHING", (name,))
            n = store.conn.execute("SELECT next_id FROM mos_counter WHERE device = ?", (name,)).fetchone()["next_id"]
            store.conn.execute("UPDATE mos_counter SET next_id = ? WHERE device = ?", (n + 1, name))
        return n

    # -- the socket --
    def _close(self, name: str) -> None:
        conn = self.conns.pop(name, None)
        if conn:
            try:
                conn.sock.close()
            except OSError:
                pass

    def _read(self, conn: _Conn, timeout: float) -> list:
        conn.sock.settimeout(timeout)
        try:
            data = conn.sock.recv(65536)
        except socket.timeout:
            return []
        if not data:
            raise MosProtocolError("the device closed the connection")
        return conn.reader.feed(data)

    def _answer_heartbeats(self, conn: _Conn, dev: Device, store: Store, roots: list) -> None:
        for root in roots:
            if root.find("heartbeat") is not None:
                conn.sock.sendall(frame(heartbeat_message(dev.name, self.ncs_id, self._next_id(store, dev.name))))

    def _request(self, store: Store, conn: _Conn, dev: Device, message: str) -> Optional[str]:
        """Send one message and wait for its roAck. Returns None for OK, else the device's status text. Raises on timeout or a
        dropped connection (the caller decides what that means for the message)."""
        conn.sock.sendall(frame(message))
        deadline = time.monotonic() + self.ack_timeout     # real time: waiting for a socket is not scheduling
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MosProtocolError("no roAck before the timeout")
            roots = [parse(m) for m in self._read(conn, min(left, 1.0))]
            self._answer_heartbeats(conn, dev, store, roots)
            for root in roots:
                ack = root.find("roAck")
                if ack is not None:
                    status = (ack.findtext("roStatus") or "").strip()
                    return None if status.upper() == "OK" else (status or "the device refused the message")

    # -- one pass --
    def step(self, store: Store) -> None:
        for dev in self.devices:
            try:
                self._step_device(store, dev)
            except (OSError, MosProtocolError) as exc:
                self._close(dev.name)
                self._fail(store, dev.name, exc)
            except Exception:
                log.exception("mos sender: step failed for %s", dev.name)
                self._close(dev.name)

    def _fail(self, store: Store, name: str, exc: Exception) -> None:
        self.fails[name] = self.fails.get(name, 0) + 1
        self.retry_at[name] = self.clock() + min(MAX_BACKOFF, 2.0 ** self.fails[name])
        state = "refused" if isinstance(exc, ConnectionRefusedError) else "down"
        self._set_state(store, name, state, str(exc) or exc.__class__.__name__)

    def _step_device(self, store: Store, dev: Device) -> None:
        now = self.clock()
        conn = self.conns.get(dev.name)
        if conn is None:
            if now < self.retry_at.get(dev.name, 0.0):
                return
            self._set_state(store, dev.name, "connecting")
            conn = _Conn(self._connect(dev), last_beat=now)
            self.conns[dev.name] = conn
            self.fails[dev.name] = 0
            self._set_state(store, dev.name, "up")
        self._answer_heartbeats(conn, dev, store, [parse(m) for m in self._read(conn, 0.05)])
        if now - conn.last_beat >= self.heartbeat_every:
            conn.sock.sendall(frame(heartbeat_message(dev.name, self.ncs_id, self._next_id(store, dev.name))))
            conn.last_beat = now
        for show_id, title in self._live_shows(store, dev):
            self._sync_show(store, conn, dev, show_id, title)
        self._sync_deleted(store, conn, dev)

    # -- which shows, and what to send --
    def _live_shows(self, store: Store, dev: Device) -> list:
        rows = store.conn.execute(
            """SELECT id, title FROM show WHERE EXISTS (SELECT 1 FROM item WHERE item.show_id = show.id AND item.status IN ('ready', 'on air', 'taking'))
                  OR id IN (SELECT show_id FROM mos_sync WHERE device = ?) ORDER BY created_at, id""", (dev.name,)).fetchall()
        return [(r["id"], r["title"]) for r in rows]

    def _build(self, store: Store, dev: Device, show_id: str, title: str, tag: str, message_id: int):
        return mos.build_ro_create(show_id, title, mos.load_stories(store, show_id), dev.name, self.ncs_id, message_id=message_id, tag=tag)

    def _sync_show(self, store: Store, conn: _Conn, dev: Device, show_id: str, title: str) -> None:
        row = store.conn.execute("SELECT state, acked_hash FROM mos_sync WHERE device = ? AND show_id = ?", (dev.name, show_id)).fetchone()
        if row is not None and row["state"] == "unconfirmed":
            return                                      # waiting for the desk
        if self.clock() < self.show_retry.get((dev.name, show_id), 0.0):
            return
        digest = hashlib.sha256(self._build(store, dev, show_id, title, "roReplace", 0).content.encode("utf-8")).hexdigest()
        if row is not None and row["acked_hash"] == digest:
            return                                      # the device already has exactly this
        creating = row is None
        message = self._build(store, dev, show_id, title, "roCreate" if creating else "roReplace", self._next_id(store, dev.name)).content
        if creating:                                    # record the attempt first: a roCreate that gets no answer must not be forgotten
            store.conn.execute("INSERT INTO mos_sync (device, show_id, state, acked_hash, error, updated_at) VALUES (?, ?, 'creating', NULL, NULL, ?)",
                               (dev.name, show_id, _now()))
            store.conn.commit()
        try:
            refusal = self._request(store, conn, dev, message)
        except (OSError, MosProtocolError) as exc:
            if creating:
                store.conn.execute("UPDATE mos_sync SET state = 'unconfirmed', error = ?, updated_at = ? WHERE device = ? AND show_id = ?",
                                   (f"roCreate sent, no answer: {exc}", _now(), dev.name, show_id))
                store.conn.commit()
            raise
        if refusal is not None:
            if creating:                                # an explicit refusal: nothing was created, so it is safe to try again later
                store.conn.execute("DELETE FROM mos_sync WHERE device = ? AND show_id = ?", (dev.name, show_id))
            else:
                store.conn.execute("UPDATE mos_sync SET error = ?, updated_at = ? WHERE device = ? AND show_id = ?", (refusal[:300], _now(), dev.name, show_id))
            store.conn.commit()
            self.show_retry[(dev.name, show_id)] = self.clock() + 15.0
            self._set_state(store, dev.name, "up", f"{show_id}: {refusal}")
            return
        store.conn.execute("UPDATE mos_sync SET state = 'created', acked_hash = ?, error = NULL, updated_at = ? WHERE device = ? AND show_id = ?",
                           (digest, _now(), dev.name, show_id))
        store.conn.commit()
        self._set_state(store, dev.name, "up")

    def _sync_deleted(self, store: Store, conn: _Conn, dev: Device) -> None:
        for row in store.conn.execute("SELECT show_id, state FROM mos_sync WHERE device = ?", (dev.name,)).fetchall():
            if row["state"] != "created" or store.conn.execute("SELECT 1 FROM show WHERE id = ?", (row["show_id"],)).fetchone():
                continue
            refusal = self._request(store, conn, dev, delete_message(dev.name, self.ncs_id, self._next_id(store, dev.name), row["show_id"]))
            if refusal is not None:
                self._set_state(store, dev.name, "up", f"{row['show_id']}: {refusal}")
                continue
            store.conn.execute("DELETE FROM mos_sync WHERE device = ? AND show_id = ?", (dev.name, row["show_id"]))
            store.conn.commit()


# ---- the desk's side -----------------------------------------------------------------------------------------

def resolve(store: Store, device: str, show_id: str, action: str) -> None:
    """The desk's answer for a show whose roCreate got no acknowledgement. 'created': the device has it (the next change goes as a
    roReplace). 'retry': it does not, send the roCreate again. KeyError when there is nothing unconfirmed."""
    row = store.conn.execute("SELECT state FROM mos_sync WHERE device = ? AND show_id = ?", (device, show_id)).fetchone()
    if row is None or row["state"] != "unconfirmed":
        raise KeyError(f"{device}:{show_id}")
    if action == "retry":
        store.conn.execute("DELETE FROM mos_sync WHERE device = ? AND show_id = ?", (device, show_id))
    elif action == "created":
        store.conn.execute("UPDATE mos_sync SET state = 'created', acked_hash = NULL, error = NULL, updated_at = ? WHERE device = ? AND show_id = ?",
                           (_now(), device, show_id))
    else:
        raise ValueError("action must be created or retry")
    store.conn.commit()


def status(store: Store) -> list:
    """What the Show screen shows: each device's connection state and the shows waiting on the desk."""
    devices = []
    for d in store.conn.execute("SELECT device, state, detail FROM mos_device ORDER BY device").fetchall():
        unconfirmed = [r["show_id"] for r in store.conn.execute(
            "SELECT show_id FROM mos_sync WHERE device = ? AND state = 'unconfirmed' ORDER BY show_id", (d["device"],)).fetchall()]
        devices.append({"name": d["device"], "state": d["state"], "detail": d["detail"] or "", "unconfirmed": unconfirmed})
    return devices
