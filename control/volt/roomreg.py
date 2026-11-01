"""A registry of the rooms (devices and control surfaces) a cue can be sent to.

Today there is one room kind, `corevideo.pro`. A cue names its room, and dispatch looks the client up here, so a second kind
(a deck, for example) is a registration and a handler table, not a change to dispatch, take, the clock or the server.
Every caller may still pass a single room object: `as_registry` wraps it under the CoreVideo Pro name.
"""

from __future__ import annotations

import os
from typing import Optional

COREVIDEO = "corevideo.pro"


class RoomRegistry:
    def __init__(self, rooms: Optional[dict] = None):
        self._rooms = dict(rooms or {})

    def register(self, name: str, client) -> None:
        self._rooms[name] = client

    def get(self, name: str):
        """The client for a room name, or None when that room is not configured."""
        return self._rooms.get(name)

    def names(self) -> list:
        return sorted(self._rooms)

    def __bool__(self) -> bool:
        return bool(self._rooms)


def as_registry(room) -> RoomRegistry:
    """A registry as it is; a single room object as the CoreVideo Pro room; None as an empty registry."""
    if isinstance(room, RoomRegistry):
        return room
    return RoomRegistry({COREVIDEO: room} if room is not None else {})


def from_env(env=None) -> RoomRegistry:
    """The rooms the environment configures: CoreVideo Pro from VOLT_ROOM_URL (and VOLT_ROOM_TOKEN)."""
    env = os.environ if env is None else env
    reg = RoomRegistry()
    url = env.get("VOLT_ROOM_URL")
    if url:
        from volt.rooms import CoreVideoProRoom
        reg.register(COREVIDEO, CoreVideoProRoom(url, env.get("VOLT_ROOM_TOKEN") or None))
    return reg


def configured(env=None) -> bool:
    """True when the environment configures any room; `volt run` starts the clock only then."""
    env = os.environ if env is None else env
    return bool(env.get("VOLT_ROOM_URL"))
