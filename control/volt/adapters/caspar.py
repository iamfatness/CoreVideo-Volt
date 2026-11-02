"""CasparCG over AMCP (docs/specs/casparcg.md). Commands: caspar.load, caspar.play, caspar.stop, caspar.clear.

Wire, as read in the CasparCG AMCP wiki page (see the spec page for the title, revision and what was and was not confirmed):
one command per line terminated by CRLF over TCP (default port 5250); replies start `202 <CMD> OK` (done), `201 <CMD> OK` (one data
line follows), `200 <CMD> OK` (data lines and a closing blank line follow), `400` (not understood), `401`-`404` (client errors),
`500`-`503` (server errors).

A cue is fired only when `INFO <channel>-<layer>` shows it, never on a `202`. The published wiki does not document the XML that
`INFO <channel>-<layer>` returns, so this module does NOT guess it: observation goes through a named layout parser (`LAYOUTS`), and
with none configured the adapter fails closed ("INFO layout not configured") before any command is sent. The layouts shipped are
none; a layout is added from a captured `INFO` reply of the customer's server version.

UNVALIDATED against a real CasparCG server: tested against a fake AMCP server only.
"""

from __future__ import annotations

import json
import re
import socket
import time
from typing import Callable, Optional

from volt.adapters.base import ActionSpec, CommandRefused, DeviceError, NotSent, Observation

KIND = "casparcg"
ENV = "VOLT_CASPAR_HOSTS"
DEFAULT_PORT = 5250
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CLIP = re.compile(r"^[A-Za-z0-9_./ -]{1,200}$")      # no quotes, backslashes or control characters: AMCP is line-based
_REPLY = re.compile(r"^(\d{3})\s+(.*)$")

# name -> function(text) -> {"foreground": str | None, "background": str | None}. Empty on purpose: see the module docstring.
LAYOUTS: dict = {}


# ---- configuration -------------------------------------------------------------------------------------------

def parse_hosts(text: Optional[str]) -> dict:
    """`name=host[:port],name2=host2` -> {name: (host, port)}. A bad entry is a ValueError."""
    out: dict = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, addr = part.partition("=")
        host, _, port = addr.strip().rpartition(":") if ":" in addr else (addr.strip(), "", "")
        if not sep or not _NAME.match(name.strip()) or not host:
            raise ValueError(f"{ENV} entry {part!r}: write it as name=host or name=host:port")
        if port and not (port.isdigit() and 0 < int(port) < 65536):
            raise ValueError(f"{ENV} entry {part!r}: the port must be a number")
        if name.strip() in out:
            raise ValueError(f"{ENV} names must be unique")
        out[name.strip()] = (host, int(port) if port else DEFAULT_PORT)
    return out


def _payload(text: str, need_clip: bool, allow_clip: bool = True) -> dict:
    """`{"server": "G", "channel": 1, "layer": 10, "clip": "NEWS/OPEN"}`. channel defaults to 1, layer to 10."""
    try:
        data = json.loads((text or "").strip())
    except ValueError:
        raise ValueError('the payload is JSON: {"server": "G", "channel": 1, "layer": 10, "clip": "NEWS/OPEN"}') from None
    if not isinstance(data, dict) or set(data) - {"server", "channel", "layer", "clip"}:
        raise ValueError('the payload may have only "server", "channel", "layer" and "clip"')
    server = data.get("server")
    if not isinstance(server, str) or not _NAME.match(server):
        raise ValueError('name the server: "server": "G"')
    out = {"server": server}
    for key, default, low, high in (("channel", 1, 1, 99), ("layer", 10, 0, 9999)):
        v = data.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError(f'"{key}" is a whole number from {low} to {high}')
        out[key] = v
    clip = data.get("clip")
    if clip is not None:
        if not allow_clip:
            raise ValueError("this command takes no clip")
        if not isinstance(clip, str) or not _CLIP.match(clip) or clip != clip.strip():
            raise ValueError('"clip" may use letters, digits, space and _ . / - only (no quotes, backslashes or line breaks)')
        out["clip"] = clip
    elif need_clip:
        raise ValueError('this command needs a "clip"')
    return out


def _clip_arg(clip: str) -> str:
    return f'"{clip}"' if " " in clip else clip


def _where(p: dict) -> str:
    return f"{p['channel']}-{p['layer']}"


# ---- the session ---------------------------------------------------------------------------------------------

class CasparSession:
    def __init__(self, name: str, host: str, port: int, layout: Optional[str], connect=None, timeout: float = READ_TIMEOUT):
        self.name, self.host, self.port, self.layout, self.timeout = name, host, port, layout, timeout
        try:
            self.sock = (connect or (lambda: socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)))()
        except OSError as exc:
            raise DeviceError(f"casparcg {name} ({host}:{port}) is not reachable: {exc}") from None
        self.buf = b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _line(self) -> str:
        self.sock.settimeout(self.timeout)
        while b"\n" not in self.buf:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                raise DeviceError(f"casparcg {self.name} did not answer in {self.timeout:g} s") from None
            except OSError as exc:
                raise DeviceError(f"casparcg {self.name} connection failed: {exc}") from None
            if not data:
                raise DeviceError(f"casparcg {self.name} closed the connection")
            self.buf += data
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", errors="replace").rstrip("\r")

    def command(self, text: str) -> list:
        """Send one command and return its data lines (empty for a 202). A 4xx/5xx reply is CommandRefused with the server's words."""
        if any(c in text for c in "\r\n\0"):
            raise DeviceError("refusing to send a command with a line break in it")
        self.sock.settimeout(self.timeout)
        try:
            self.sock.sendall(text.encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise NotSent(f"casparcg {self.name} connection failed: {exc}") from None
        head = self._line()
        m = _REPLY.match(head)
        if not m:
            raise DeviceError(f"casparcg {self.name} sent something that is not an AMCP reply: {head[:60]!r}")
        code = int(m.group(1))
        if code >= 400:
            raise CommandRefused(f"casparcg {self.name} refused {text.split(' ')[0]}: {head}")
        if code == 201:
            return [self._line()]
        if code == 200:
            lines = []
            while True:
                line = self._line()
                if line == "":
                    return lines
                lines.append(line)
        return []

    def observe(self) -> Observation:
        """The poller's reading: is the server there and what version it says (VERSION is read-only)."""
        lines = self.command("VERSION")
        return Observation(time.monotonic(), {"version": lines[0] if lines else ""}, lines[0] if lines else "")

    def observe_layer(self, channel: int, layer: int) -> Observation:
        if not self.layout or self.layout not in LAYOUTS:
            raise DeviceError(f"casparcg {self.name}: INFO layout {self.layout or '(none)'!s} is not configured; "
                              "the INFO reply format is not guessed, so nothing is sent")
        text = "\n".join(self.command(f"INFO {channel}-{layer}"))
        try:
            state = LAYOUTS[self.layout](text)
        except (ValueError, KeyError) as exc:
            raise DeviceError(f"casparcg {self.name}: INFO reply does not match layout {self.layout}: {exc}") from None
        return Observation(time.monotonic(), {"foreground": state.get("foreground"), "background": state.get("background")},
                           f"{channel}-{layer}")


class _LayerSession:
    """A session bound to the layer the action concerns, so observe() reads that layer. Built by the adapter per action."""

    def __init__(self, inner: CasparSession, channel: int, layer: int):
        self.inner, self.channel, self.layer = inner, channel, layer

    def observe(self) -> Observation:
        if self.channel is None:                      # the poller's session: no layer, just 'is it there'
            return self.inner.observe()
        return self.inner.observe_layer(self.channel, self.layer)

    def command(self, text: str) -> list:
        return self.inner.command(text)

    def close(self) -> None:
        self.inner.close()


# ---- the actions ---------------------------------------------------------------------------------------------

def _fg_is(clip):  # the foreground names the clip
    return lambda s: s.state.get("foreground") == clip


def _load(session, p):
    session.command(f"LOADBG {_where(p)} {_clip_arg(p['clip'])}")


def _load_observed(base, now, p):
    return None if now.state.get("background") == p["clip"] else f"background is {now.state.get('background')!r}"


def _loaded(now, p):
    return _load_observed(None, now, p)


def _play(session, p):
    session.command(f"PLAY {_where(p)} {_clip_arg(p['clip'])}" if p.get("clip") else f"PLAY {_where(p)}")


def _play_observed(base, now, p):
    want = p.get("clip") or base.state.get("background")
    if not want:
        return "nothing is loaded in the background to play"
    return None if now.state.get("foreground") == want else f"foreground is {now.state.get('foreground')!r}"


def _stop(session, p):
    session.command(f"STOP {_where(p)}")


def _stop_observed(base, now, p):
    return None if now.state.get("foreground") is None else f"foreground is {now.state.get('foreground')!r}"


def _clear(session, p):
    session.command(f"CLEAR {_where(p)}")


def _clear_observed(base, now, p):
    if now.state.get("foreground") is None and now.state.get("background") is None:
        return None
    return f"layer still shows {now.state.get('foreground')!r}/{now.state.get('background')!r}"


ACTIONS = {
    "caspar.load": ActionSpec("caspar.load", lambda t: _payload(t, True), _load, _load_observed, True, False,
                              prepare=_load, prepared=_loaded),
    # A bare PLAY of a clip restarts it, so play is never sent twice. With a clip it is the full action; with none it promotes the
    # background (the small action after a preview).
    "caspar.play": ActionSpec("caspar.play", lambda t: _payload(t, False), _play, _play_observed, False, True,
                              prepare=_load, prepared=_loaded),
    "caspar.stop": ActionSpec("caspar.stop", lambda t: _payload(t, False, allow_clip=False), _stop, _stop_observed, True, True),
    "caspar.clear": ActionSpec("caspar.clear", lambda t: _payload(t, False, allow_clip=False), _clear, _clear_observed, True, True),
}


class CasparAdapter:
    kind = KIND
    ACTIONS = ACTIONS

    def __init__(self, hosts: dict, layout: Optional[str] = None, connect: Optional[Callable] = None, timeout: float = READ_TIMEOUT):
        self.hosts, self.layout, self._connect, self.timeout = dict(hosts), layout, connect, timeout
        self._layer: dict = {}

    def device_names(self) -> list:
        return sorted(self.hosts)

    def device_of(self, params: dict) -> str:
        return params["server"]

    def open(self, name: str, channel: Optional[int] = None, layer: Optional[int] = None):
        if name not in self.hosts:
            raise DeviceError(f"no casparcg server named {name!r} is configured ({ENV} has: {', '.join(sorted(self.hosts)) or 'none'})")
        host, port = self.hosts[name]
        connect = (lambda: self._connect(host, port)) if self._connect else None
        return _LayerSession(CasparSession(name, host, port, self.layout, connect, self.timeout), channel, layer)

    def open_for(self, name: str, params: dict):
        return self.open(name, params.get("channel", 1), params.get("layer", 10))


def from_text(text: str) -> CasparAdapter:
    import os
    return CasparAdapter(parse_hosts(text), layout=os.environ.get("VOLT_CASPAR_INFO_LAYOUT") or None)
