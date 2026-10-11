"""show-input.take: put a source on Program and fire only when the core shows it there.

CoreVideo Pro has no control action that routes a source into a scene (CoreVideoPro
issue #861). `input.assign` only edits a slot and does not touch Preview or Program.
So the steps that precede `transport.take` are configuration, a list of
{action, args} templates, and the evidence is the core's own programFrame.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from volt.rooms import LiveError, ProgramObservation

SCHEMES = ("zoom:", "media:", "capture:")

# Default for a scene that already routes the slot. `input.assign` alone is NOT enough.
DEFAULT_ACTIONS = [{"action": "scene.select", "args": ["{scene}"]}]

# Evidence must hold on this many consecutive polls, each on a newer delivered frame.
CONFIRMATIONS = 2

# Layer-id prefixes the core gives layers while a take is rendering.
TRANSITION_LAYERS = ("take-in:", "take-out:")


@dataclass
class ShowInput:
    source: str
    scene: Optional[str] = None
    slot: Optional[int] = None
    transition: str = "cut"  # cut | fade


def parse_payload(payload: str) -> ShowInput:
    """A bare string is a source id. A JSON object may add scene, slot and transition."""
    text = (payload or "").strip()
    if not text:
        raise LiveError("show-input.take: payload names no source")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LiveError(f"show-input.take: payload is not valid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise LiveError("show-input.take: payload JSON must be an object")
    else:
        data = {"source": text}
    source = str(data.get("source") or "").strip()
    if not source:
        raise LiveError("show-input.take: payload names no source")
    transition = str(data.get("transition") or "cut").lower()
    if transition not in ("cut", "fade"):
        raise LiveError(f"show-input.take: unknown transition {transition!r} (cut or fade)")
    slot = data.get("slot")
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int)):
        raise LiveError("show-input.take: slot must be an integer")
    scene = data.get("scene")
    return ShowInput(source, str(scene) if scene else None, slot, transition)


def _fill(arg, si: ShowInput):
    if not isinstance(arg, str):
        return arg
    if arg == "{slot}" and si.slot is not None:
        return si.slot
    values = {"{scene}": si.scene, "{source}": si.source,
              "{slot}": None if si.slot is None else str(si.slot)}
    for key, value in values.items():
        if key in arg:
            if value is None:
                raise LiveError(f"show-input.take: action needs {key[1:-1]} but the payload has none")
            arg = arg.replace(key, value)
    return arg


def plan_actions(si: ShowInput, templates: Optional[list]) -> list[tuple[str, list]]:
    out = []
    for step in templates if templates is not None else DEFAULT_ACTIONS:
        out.append((str(step["action"]), [_fill(a, si) for a in step.get("args", [])]))
    return out


def _bare(value: str) -> str:
    for scheme in SCHEMES:
        if value.startswith(scheme):
            return value[len(scheme):]
    return value


def _matches(target: str, source) -> bool:
    """Target against sourceId and participantId. Ids may be bare or scheme-prefixed.

    A bare target matches either form. A prefixed target matches the identical id,
    or a bare id equal to its bare part. The `kind` values are unproven, so they are
    not used to disambiguate `zoom:5` from `media:5`.
    """
    target_prefixed = target != _bare(target)
    for cand in (source.source_id, source.participant_id):
        if not cand:
            continue
        if cand == target:
            return True
        cand_prefixed = cand != _bare(cand)
        if not target_prefixed and _bare(cand) == target:
            return True
        if target_prefixed and not cand_prefixed and cand == _bare(target):
            return True
    return False


def _same_layer(a, b) -> bool:
    return (a.layer_id, a.source_id, a.participant_id) == (b.layer_id, b.source_id, b.participant_id)


def _blocker(si: ShowInput, base: ProgramObservation, o: ProgramObservation,
             transition_status: Callable[[], Optional[str]]) -> Optional[str]:
    """The first reason this observation is not proof, or None if it is."""
    if not o.available:
        return "program observation unavailable" + (f" ({o.error})" if o.error else "")
    if o.stale:
        return "program observation stale"
    if o.attribution != "live":
        return f"scene attribution not live ({o.attribution})"
    if o.delivery_sequence is None or o.delivery_sequence <= base.delivery_sequence:
        return "delivery sequence not advanced since before the take"
    if not any(_matches(si.source, s) for s in o.sources):
        return f"source {si.source!r} not on program"
    # Seen live (CoreVideo Pro, 2026-10-11): mid-transition Program carries the old scene's layers renamed
    # `take-out:<layer>` next to the new scene's `take-in:<layer>`. Layer ids change, so the same-layer test
    # below cannot see the outgoing sources; Program is not proof until the core has dropped those layers.
    if any(s.layer_id.startswith(TRANSITION_LAYERS) for s in o.sources):
        return "transition still rendering (take-in/take-out layers on program)"
    if si.transition == "cut":
        for s in base.sources:
            if _matches(si.source, s):
                continue
            if any(_same_layer(s, n) for n in o.sources):
                return f"outgoing source {s.source_id or s.participant_id!r} still on program"
    else:
        status = transition_status()
        if status != "completed":
            return f"fade not finished (takeTransition {status!r})"
    return None


def show_input_take(room, payload: str, attempts: int, interval: float, sleep,
                    actions: Optional[list] = None) -> None:
    """Run the configured actions, take, and return only once Program shows the source.

    Raises LiveError with the specific reason otherwise. A rollback after one good
    reading is caught because the evidence must hold on CONFIRMATIONS consecutive
    polls, each with a newer delivery sequence than the last.
    """
    si = parse_payload(payload)
    steps = plan_actions(si, actions)  # may raise before anything is sent
    base = room.program_observed()
    if not base.available or base.stale or base.delivery_sequence is None:
        raise LiveError("show-input.take: no usable program observation before the take; nothing sent")
    for action, args in steps:
        room.invoke(action, args)
    room.invoke("transport.take", [])

    def status() -> Optional[str]:
        return (room.state().get("takeTransition") or {}).get("status")

    reason = "no observation"
    streak, last_seq = 0, None
    for _ in range(attempts):
        o = room.program_observed()
        blocker = _blocker(si, base, o, status)
        if blocker is not None:
            streak, reason = 0, blocker
        elif streak and o.delivery_sequence <= last_seq:
            streak, reason = 1, "delivery stalled while confirming"
        else:
            streak += 1
            last_seq = o.delivery_sequence
            if streak >= CONFIRMATIONS:
                return
            reason = "awaiting confirmation on a newer delivered frame"
        sleep(interval)
    raise LiveError(f"show-input.take: timed out, {reason}")
