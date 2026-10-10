"""Take: put the next item on air and send its cues, or leave the rundown where it was."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from volt.cues import arm_cues
from volt.dispatch import Result, dispatch_item
from volt.playout import advance, rewind
from volt.store import Store, _now


@dataclass
class TakeResult:
    advanced: bool
    item_id: Optional[str]
    results: list[Result] = field(default_factory=list)


def take_item(
    store: Store,
    room,
    show_id: str,
    owner: str = "desk",
    clock: Callable[[], str] = _now,
    attempts: int = 40,
    interval: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> TakeResult:
    """Advance the rundown, then dispatch the new item's cues.

    If any cue does not land, the rundown goes back to where it was. Cues that did
    fire stay fired, since they happened in the room, and the failed cue stays armed.
    Taking again resumes at that cue.
    """
    adv = advance(store, show_id, clock)
    if adv.target is None:
        return TakeResult(True, None)
    arm_cues(store, adv.target)
    results = dispatch_item(
        store, room, adv.target, owner=owner, attempts=attempts, interval=interval, sleep=sleep
    )
    if any(r.status != "fired" for r in results):
        rewind(store, adv)
        return TakeResult(False, adv.target, results)
    return TakeResult(True, adv.target, results)
