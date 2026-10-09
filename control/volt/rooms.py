"""Room adapters. A room runs the cues. An ack is not evidence; an observation is."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

# Closed vocabulary from CoreVideoPro contracts/lifecycle.schema.json. `starting`
# and `live` are retired aliases for `preparing` and `producing`.
KNOWN_STATES = {
    "idle", "requested", "preparing", "producing", "starting", "live",
    "stopping", "finalizing", "completed", "failed", "interrupted",
}


class LiveError(Exception):
    pass


@dataclass
class Observation:
    state: str
    health: str
    finalized: bool
    session_id: Optional[str]
    artifact_path: Optional[str]
    stale: bool = False
    error: Optional[str] = None

    def usable(self) -> bool:
        """Fresh, in the known vocabulary, and not reporting failure."""
        return not self.stale and self.state in KNOWN_STATES and self.health != "failed"


class FakeRoom:
    """Plays a scripted run of observations. The last one repeats."""

    def __init__(self, script: list[Observation]):
        self._script = list(script)
        self._at = 0
        self.calls: list[tuple[str, list]] = []

    def invoke(self, action: str, args: list) -> None:
        self.calls.append((action, args))

    def invoked(self, action: str, args: list) -> bool:
        return (action, args) in self.calls

    def observe(self) -> Observation:
        item = self._script[min(self._at, len(self._script) - 1)]
        self._at += 1
        return item


class CoreVideoProRoom:
    """CoreVideo Pro's HTTP control API: POST /invoke and GET /snapshot.

    Wire shape taken from CoreVideoPro source (HttpControlRouter, MediaCore.cpp
    recording node). Not yet exercised against a live core.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8011", token: Optional[str] = None, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LiveError(f"core unreachable: {exc}") from exc

    def invoke(self, action: str, args: list) -> None:
        result = self._request("POST", "/invoke", {"action": action, "args": args})
        if not result.get("ok"):
            raise LiveError(f"{action} refused: {result.get('error')}")

    def observe(self) -> Observation:
        env = self._request("GET", "/snapshot")
        if not env.get("available"):
            return Observation("unknown", "unknown", False, None, None, stale=True,
                               error=env.get("reasonCode"))
        recording = (env.get("snapshot") or {}).get("recording") or {}
        life = recording.get("lifecycle")
        if not isinstance(life, dict):
            # Absent means unknown (an older core), never healthy.
            return Observation("unknown", "unknown", False, None, recording.get("artifactPath"),
                               stale=bool(env.get("stale")))
        return Observation(
            state=str(life.get("state", "unknown")),
            health=str(life.get("health", "unknown")),
            finalized=bool(life.get("finalized", False)),
            session_id=life.get("sessionId"),
            artifact_path=recording.get("artifactPath"),
            stale=bool(env.get("stale")),
            error=life.get("error"),
        )
