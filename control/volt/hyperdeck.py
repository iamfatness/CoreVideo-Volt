"""A second room kind: Blackmagic HyperDeck disk recorders over the Ethernet protocol (docs/specs/hyperdeck.md).

The protocol is TCP port 9993, one text command per line (`play`, `stop`, `record`, `goto: clip id: 3`, `transport info`), answered
by a numbered block: `200 ok`, a failure `1xx`, or a multi-line information block (`208 transport info:` ... a blank line).
The deck greets a new connection with a `500 connection info:` block. This follows the HyperDeck Ethernet Protocol as published by
Blackmagic (protocol version 1.x); the version a deck reports in its greeting is kept in the client for the log.

A cue is `fired` only when the deck's reported transport state shows what the cue asked for, never on `200 ok`. A deck that does not
answer, refuses, or cannot be reached is a failed cue (a `LiveError`), never a hang: every socket call has a timeout.

UNVALIDATED against a real deck: tested against a fake deck that speaks the same text protocol. Clip numbering, timing and behaviour
on a deck that is recording or has no disk are not proven.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from typing import Callable, Optional

from volt.rooms import LiveError

ROOM = "hyperdeck"
DEFAULT_PORT = 9993
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
DECK_COMMANDS = ("deck.play", "deck.stop", "deck.goto", "deck.record.start", "deck.record.stop")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ---- configuration -------------------------------------------------------------------------------------------

def parse_hosts(text: Optional[str]) -> dict:
    """`name=host[:port],name2=host2` -> {name: (host, port)}. Empty or unset is no decks. A bad entry is a ValueError."""
    out: dict = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, addr = part.partition("=")
        host, _, port = addr.strip().rpartition(":") if ":" in addr else (addr.strip(), "", "")
        if not sep or not _NAME.match(name.strip()) or not host:
            raise ValueError(f"VOLT_HYPERDECK_HOSTS entry {part!r}: write it as name=host or name=host:port")
        if port and not (port.isdigit() and 0 < int(port) < 65536):
            raise ValueError(f"VOLT_HYPERDECK_HOSTS entry {part!r}: the port must be a number")
        if name.strip() in out:
            raise ValueError("VOLT_HYPERDECK_HOSTS names must be unique")
        out[name.strip()] = (host, int(port) if port else DEFAULT_PORT)
    return out


def parse_payload(text: str):
    """A deck cue's payload: JSON `{"deck": "A", "clip": 3}` (clip is a number or a name, optional), or the shorthand `A` or `A/3`.
    Returns (deck, clip or None). A ValueError says what is wrong."""
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            data = json.loads(t)
        except ValueError:
            raise ValueError("the payload is not valid JSON") from None
        if not isinstance(data, dict) or set(data) - {"deck", "clip"}:
            raise ValueError('the payload may have only "deck" and "clip"')
        deck, clip = data.get("deck"), data.get("clip")
    else:
        deck, _, clip = t.partition("/")
        clip = clip or None
    if not isinstance(deck, str) or not _NAME.match(deck.strip() if isinstance(deck, str) else ""):
        raise ValueError("name the deck: the payload is {\"deck\": \"A\", \"clip\": 3} or just A")
    if clip is not None:
        if isinstance(clip, bool) or not isinstance(clip, (int, str)):
            raise ValueError("clip is a clip number or a clip name")
        if isinstance(clip, str):
            clip = clip.strip()
            if not clip:
                clip = None
            elif clip.isdigit():
                clip = int(clip)
        if isinstance(clip, int) and clip < 1:
            raise ValueError("clip numbers start at 1")
    return deck.strip(), clip


# ---- the protocol --------------------------------------------------------------------------------------------

@dataclass
class Reply:
    code: int
    text: str            # the first line after the code
    fields: dict         # `key: value` lines of an information block, keys lower-cased
    lines: list          # every other line of the block


class DeckError(LiveError):
    pass


class HyperDeck:
    """One connection to one deck. Not shared: every cue opens its own and closes it."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, connect=None, timeout: float = READ_TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        try:
            self.sock = (connect or (lambda: socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)))()
        except OSError as exc:
            raise DeckError(f"deck {host}:{port} is not reachable: {exc}") from None
        self.buf = b""
        self.version = ""
        greeting = self._read_block()
        if greeting.code != 500:
            self.close()
            raise DeckError(f"deck {host}:{port} did not greet like a HyperDeck (got {greeting.code} {greeting.text})")
        self.version = greeting.fields.get("protocol version", "")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _line(self) -> str:
        self.sock.settimeout(self.timeout)
        while b"\r\n" not in self.buf and b"\n" not in self.buf:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                raise DeckError(f"deck {self.host}:{self.port} did not answer in {self.timeout:g} s") from None
            except OSError as exc:
                raise DeckError(f"deck {self.host}:{self.port} connection failed: {exc}") from None
            if not data:
                raise DeckError(f"deck {self.host}:{self.port} closed the connection")
            self.buf += data
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", errors="replace").rstrip("\r")

    def _read_block(self) -> Reply:
        head = self._line()
        m = re.match(r"^(\d{3})\s*(.*)$", head)
        if not m:
            raise DeckError(f"deck {self.host}:{self.port} sent something that is not a reply: {head[:60]!r}")
        code, text = int(m.group(1)), m.group(2)
        fields, lines = {}, []
        if text.endswith(":"):                       # an information block runs to the next blank line
            text = text[:-1]
            while True:
                line = self._line()
                if line == "":
                    break
                key, sep, value = line.partition(":")
                if sep and not key.strip().isdigit():
                    fields[key.strip().lower()] = value.strip()
                else:
                    lines.append(line)
        return Reply(code, text, fields, lines)

    def command(self, text: str) -> Reply:
        """Send one command and return its reply. A 1xx code is a refusal: DeckError with the deck's own words. Asynchronous
        notices (5xx) that arrive in between are skipped."""
        self.sock.settimeout(self.timeout)
        try:
            self.sock.sendall(text.encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise DeckError(f"deck {self.host}:{self.port} connection failed: {exc}") from None
        while True:
            reply = self._read_block()
            if 500 <= reply.code < 600:
                continue
            if 100 <= reply.code < 200:
                raise DeckError(f"deck {self.host}:{self.port} refused {text.split(':')[0]!r}: {reply.code} {reply.text}")
            return reply

    # -- what the cues use --
    def transport(self) -> dict:
        return self.command("transport info").fields

    def clips(self) -> list:
        """[(id, name)] for the clips on the deck's current disk."""
        out = []
        for line in self.command("clips get").lines:
            m = re.match(r"^(\d+):\s*(.+?)\s+\d{2}:\d{2}:\d{2}[:;]\d{2}\s+\d{2}:\d{2}:\d{2}[:;]\d{2}\s*$", line)
            if m:
                out.append((int(m.group(1)), m.group(2)))
        return out

    def clip_id(self, clip) -> int:
        if isinstance(clip, int):
            return clip
        for cid, name in self.clips():
            if name == clip:
                return cid
        raise DeckError(f"deck {self.host}:{self.port} has no clip named {clip!r}")


# ---- the room and its cue handlers ---------------------------------------------------------------------------

class HyperDeckRoom:
    """The `hyperdeck` room: the decks the environment names. `open(name)` connects to one."""

    def __init__(self, hosts: dict, connect: Optional[Callable] = None, timeout: float = READ_TIMEOUT):
        self.hosts, self._connect, self.timeout = dict(hosts), connect, timeout

    def open(self, name: str) -> HyperDeck:
        if name not in self.hosts:
            raise DeckError(f"no deck named {name!r} is configured (VOLT_HYPERDECK_HOSTS has: {', '.join(sorted(self.hosts)) or 'none'})")
        host, port = self.hosts[name]
        connect = (lambda: self._connect(host, port)) if self._connect else None
        return HyperDeck(host, port, connect=connect, timeout=self.timeout)


def _poll(check: Callable[[], bool], ctx: dict) -> bool:
    for _ in range(ctx["attempts"]):
        if check():
            return True
        ctx["sleep"](ctx["interval"])
    return False


def _target(room: HyperDeckRoom, cue):
    try:
        deck, clip = parse_payload(cue.payload)
    except ValueError as exc:
        raise DeckError(f"{cue.command}: {exc}") from None
    return room.open(deck), deck, clip


def _play(store, room, cue, ctx) -> None:
    deck, name, clip = _target(room, cue)
    with deck:
        if clip is not None:
            deck.command(f"goto: clip id: {deck.clip_id(clip)}")
        deck.command("play")
        if not _poll(lambda: deck.transport().get("status") == "play", ctx):
            raise DeckError(f"deck {name}: never reported playing")


def _stop(store, room, cue, ctx) -> None:
    deck, name, _clip = _target(room, cue)
    with deck:
        deck.command("stop")
        if not _poll(lambda: deck.transport().get("status") in ("stopped", "preview"), ctx):
            raise DeckError(f"deck {name}: never reported stopped")


def _goto(store, room, cue, ctx) -> None:
    deck, name, clip = _target(room, cue)
    if clip is None:
        raise DeckError("deck.goto needs a clip: the payload is {\"deck\": \"A\", \"clip\": 3} or A/3")
    with deck:
        cid = deck.clip_id(clip)
        deck.command(f"goto: clip id: {cid}")
        if not _poll(lambda: str(deck.transport().get("clip id", "")) == str(cid), ctx):
            raise DeckError(f"deck {name}: never reported clip {cid}")


def _record_start(store, room, cue, ctx) -> None:
    deck, name, clip = _target(room, cue)
    with deck:
        deck.command(f"record: name: {clip}" if isinstance(clip, str) else "record")
        if not _poll(lambda: deck.transport().get("status") == "record", ctx):
            raise DeckError(f"deck {name}: never reported recording")


def _record_stop(store, room, cue, ctx) -> None:
    deck, name, _clip = _target(room, cue)
    with deck:
        deck.command("stop")
        if not _poll(lambda: deck.transport().get("status") != "record", ctx):
            raise DeckError(f"deck {name}: still reports recording")


HANDLERS = {"deck.play": _play, "deck.stop": _stop, "deck.goto": _goto,
            "deck.record.start": _record_start, "deck.record.stop": _record_stop}
