"""The device adapter contract (docs/superpowers/specs/2026-11-01-device-control-design.md).

Adapters declare what a command is and how its effect shows in the device's own state; the executor (executor.py) is the only code
that sends. A cue is fired only when `ActionSpec.observed` says the state shows it, never on a reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from volt.rooms import LiveError


@dataclass(frozen=True)
class Observation:
    """One reading of the device's own state."""
    at: float                      # monotonic seconds
    state: dict                    # normalised, adapter-defined (e.g. {"program": 3, "preview": 5})
    detail: str = ""


class DeviceError(LiveError):
    """A refusal, timeout, unreachable device or bad greeting."""


class SafeModeError(DeviceError):
    """Safe mode is on: nothing was sent."""


class InterlockError(DeviceError):
    """An interlock refused the action: nothing was sent."""


class OutcomeUnknown(DeviceError):
    """Bytes may have reached the device and nothing it shows proves the action."""


class NotSent(DeviceError):
    """Raised by a Session when it failed BEFORE any byte of the command was written, so a retry cannot double-send."""


class CommandRefused(DeviceError):
    """The device answered with an explicit refusal (for example a 4xx reply): the command did not run, so the outcome is known."""


class BadInput(DeviceError):
    """The cue's payload does not parse. Nothing was sent; the cue is wrong, not the device."""


@dataclass(frozen=True)
class ActionSpec:
    command: str                   # "atem.program", "caspar.play"
    parse: Callable[[str], dict]   # payload -> params; ValueError says what is wrong
    perform: Callable[["Session", dict], None]
    observed: Callable[[Observation, Observation, dict], Optional[str]]
    # (baseline, now, params) -> None when the state proves it, else why not yet
    idempotent: bool               # may the same bytes be sent twice with the same effect?
    on_air: bool                   # does it change what viewers see or hear?
    prepare: Optional[Callable[["Session", dict], None]] = None
    prepared: Optional[Callable[[Observation, dict], Optional[str]]] = None
    interlocks: tuple = ()         # callables (adapter, params) -> Optional[str]: a reason to refuse


class Session(Protocol):
    def observe(self) -> Observation: ...   # read the device's state now; never changes it
    def close(self) -> None: ...


class Adapter(Protocol):
    kind: str                      # the cue's `room`
    ACTIONS: dict                  # command -> ActionSpec

    def device_names(self) -> list: ...
    def device_of(self, params: dict) -> str: ...
    def open(self, name: str) -> Session: ...   # bounded connect; raises DeviceError
