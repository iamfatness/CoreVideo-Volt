"""The one place a device command is sent (docs/superpowers/specs/2026-11-01-device-control-design.md, "The executor").

Order: gate (safe mode, interlocks) -> connect -> baseline observe -> perform once -> poll `observed`. Reconnect rules:
before any byte was written, reconnect and try again, at most twice; after bytes may have gone, an idempotent action re-observes
and resends once, a non-idempotent one only observes and is never sent twice. Observation, not the reply, decides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from volt.adapters.base import (ActionSpec, BadInput, DeviceError, InterlockError, NotSent, OutcomeUnknown, SafeModeError)

RECONNECTS = 2
UNKNOWN_TEXT = "sent; no answer; the device does not show it; check it before taking again"


@dataclass
class Outcome:
    status: str                    # fired | failed | refused | interlocked | prepared
    detail: str = ""
    events: list = field(default_factory=list)   # (kind, text) pairs for the device event log

    @property
    def ok(self) -> bool:
        return self.status in ("fired", "prepared")


def _ctx(ctx: Optional[dict]) -> dict:
    ctx = dict(ctx or {})
    ctx.setdefault("attempts", 40)
    ctx.setdefault("interval", 0.25)
    ctx.setdefault("sleep", time.sleep)
    return ctx


def _open(adapter, device: str):
    """Connect, retrying a failed connect up to RECONNECTS more times (nothing has been sent)."""
    last: Optional[DeviceError] = None
    for _ in range(RECONNECTS + 1):
        try:
            return adapter.open(device)
        except DeviceError as exc:
            last = exc
    raise last  # type: ignore[misc]


def _poll(session, observed, baseline, params: dict, ctx: dict, on_air: bool) -> Optional[str]:
    """None when the device's state proves the action (on two consecutive reads for an on-air action), else why not."""
    need = 2 if on_air else 1
    streak, last = 0, "no reading"
    for i in range(ctx["attempts"]):
        reason = observed(baseline, session.observe(), params)
        if reason is None:
            streak += 1
            if streak >= need:
                return None
            last = "proved on one read, not yet on a second"
        else:
            streak, last = 0, reason
        if i + 1 < ctx["attempts"]:
            ctx["sleep"](ctx["interval"])
    return f"timed out, {last}"


def _gate(adapter, spec: ActionSpec, params: dict, store, gate) -> Optional[Outcome]:
    try:
        if gate is not None:
            gate(adapter.kind, spec)
        elif store is not None:
            from volt import safety
            safety.check_send(store, adapter.kind, spec)
        for check in spec.interlocks:
            reason = check(adapter, params)
            if reason:
                raise InterlockError(reason)
    except SafeModeError as exc:
        return Outcome("refused", str(exc), [("refused", str(exc))])
    except InterlockError as exc:
        return Outcome("interlocked", str(exc), [("interlocked", str(exc))])
    return None


def execute(adapter, device: str, spec: ActionSpec, params: dict, ctx: Optional[dict] = None, store=None, gate=None) -> Outcome:
    ctx = _ctx(ctx)
    refused = _gate(adapter, spec, params, store, gate)
    if refused is not None:
        return refused
    return _run(adapter, device, spec, spec.perform, spec.observed, spec.on_air, params, ctx)


def prepare(adapter, device: str, spec: ActionSpec, params: dict, ctx: Optional[dict] = None, store=None, gate=None) -> Outcome:
    """The preview step: runs `spec.prepare` (never an on-air command) and proves it with `spec.prepared`."""
    ctx = _ctx(ctx)
    if spec.prepare is None or spec.prepared is None:
        return Outcome("failed", f"{spec.command} has no prepare step", [])
    refused = _gate(adapter, spec, params, store, gate)
    if refused is not None:
        return refused
    outcome = _run(adapter, device, spec, spec.prepare, lambda base, now, p: spec.prepared(now, p), False, params, ctx)
    if outcome.status == "fired":
        outcome.status = "prepared"
    return outcome


def _fail(detail: str, events: list) -> Outcome:
    events.append(("failed", detail))
    return Outcome("failed", detail, events)


def _run(adapter, device, spec, send, observed, on_air, params, ctx) -> Outcome:
    events: list = []
    try:
        session = _open(adapter, device)
    except DeviceError as exc:
        return _fail(str(exc), events)
    baseline = None
    try:
        try:
            baseline = session.observe()          # no baseline, no transition: and nothing has been sent yet
        except DeviceError as exc:
            return _fail(str(exc), events)
        for attempt in range(RECONNECTS + 1):
            try:
                send(session, params)
                break
            except NotSent as exc:                 # failed before the first byte: reconnecting cannot double-send
                events.append(("retry", f"not sent ({exc}); reconnecting"))
                session.close()
                if attempt == RECONNECTS:
                    return _fail(f"{spec.command}: could not send after {RECONNECTS + 1} tries: {exc}", events)
                try:
                    session = _open(adapter, device)
                except DeviceError as err:
                    return _fail(str(err), events)
            except DeviceError as exc:             # bytes may have gone and the answer was lost
                return _recover(adapter, device, spec, send, observed, on_air, params, ctx, baseline, events, exc)
        events.append(("sent", spec.command))
        try:
            reason = _poll(session, observed, baseline, params, ctx, on_air)
        except DeviceError as exc:
            return _fail(f"sent; {exc}", events)
        if reason is None:
            events.append(("fired", spec.command))
            return Outcome("fired", "", events)
        return _fail(reason, events)
    finally:
        session.close()


def _recover(adapter, device, spec, send, observed, on_air, params, ctx, baseline, events, exc) -> Outcome:
    events.append(("unknown", f"{exc}; re-observing"))
    try:
        session = _open(adapter, device)
    except DeviceError as again:
        return _fail(f"sent; no answer, and the device cannot be reached to check: {again}", events)
    try:
        if observed_ok(session, observed, baseline, params, on_air):
            events.append(("fired", spec.command))
            return Outcome("fired", "", events)
        if not spec.idempotent:                    # a cut is a swap: never send it a second time
            return _fail(UNKNOWN_TEXT, events)
        events.append(("resend", spec.command))
        send(session, params)
        reason = _poll(session, observed, baseline, params, ctx, on_air)
        if reason is None:
            events.append(("fired", spec.command))
            return Outcome("fired", "", events)
        return _fail(reason, events)
    except DeviceError as err:
        return _fail(f"sent; {err}", events)
    finally:
        session.close()


def observed_ok(session, observed, baseline, params, on_air) -> bool:
    """One fresh read (two for an on-air action) that proves the action."""
    for _ in range(2 if on_air else 1):
        if observed(baseline, session.observe(), params) is not None:
            return False
    return True


def handler_for(adapter, spec: ActionSpec) -> Callable:
    """A cue handler with the shape dispatch uses: handler(store, client, cue, ctx). Raises a DeviceError (a LiveError) unless fired."""
    def handler(store, client, cue, ctx):
        try:
            params = spec.parse(cue.payload)
        except ValueError as exc:
            raise BadInput(f"{spec.command}: {exc}") from None
        outcome = execute(adapter, adapter.device_of(params), spec, params, ctx, store=store)
        if outcome.status == "fired":
            return
        cls = {"refused": SafeModeError, "interlocked": InterlockError}.get(outcome.status, DeviceError)
        if outcome.detail == UNKNOWN_TEXT:
            cls = OutcomeUnknown
        raise cls(outcome.detail)
    return handler
