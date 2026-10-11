# Cues

A cue is a room, a command, and a payload on the item.

CoreVideo Pro is the first room. Its commands come from the typed control contract in CoreVideoPro: show input take, overlay take, audio snapshot, record arm, record start, stream start. vMix and OBS are other rooms behind the same record.

A take arms the cue. Dispatch sends it to that room's adapter and waits for the acknowledgement, then the snapshot. A failed cue stays armed and the rundown does not advance.

See [rooms](../rooms.md).

## show-input.take

Put a source on Program and fire only when the core shows it there.

**Payload.** A bare string is the source id (`zoom:2`, `media:clip-7`, `capture:hdmi`, or bare). A JSON object may add `scene`, `slot` (integer) and `transition` (`cut`, the default, or `fade`).

**Sequence.**

1. Read `GET /snapshot` for a baseline. If the envelope is unavailable or stale, or `programFrame.deliverySequence` is missing, fail before sending anything.
2. Run the configured pre-take actions, a list of `{action, args}` templates. Placeholders `{scene}`, `{source}` and `{slot}` are filled from the payload. The default is `[scene.select {scene}]`, which is right only when the scene already routes the slot.
3. Send `transport.take` with no args. Its reply is an ack and nothing more.
4. Poll `GET /snapshot` with the same attempts, interval and sleep as the other handlers.

`input.assign [slot, sourceId]` alone is not enough. It edits a slot and puts nothing on Preview or Program. No control action routes a slot into a scene yet: [CoreVideoPro #861](https://github.com/iamfatness/CoreVideoPro/issues/861). When it lands, only the configured action list changes (for example `input.assign` then the new route action then `scene.select`). The handler and its evidence do not.

**Evidence** (all must hold, on two consecutive polls, each with a newer `deliverySequence`):

- envelope available and not stale
- `programFrame.sceneIdAttribution == "live"` (not `holding`, not `unknown`)
- `deliverySequence` greater than the baseline from before the take
- `videoSources[]` has an entry whose `sourceId` or `participantId` matches the cue's source. Ids may be bare or prefixed `zoom:`, `media:`, `capture:`. A bare target matches either form. A prefixed target matches the identical id or a bare id equal to its bare part. `kind` is not used to tell `zoom:5` from `media:5`.
- hard cut: every baseline source that is not the target is gone from Program
- fade: `GET /state` `takeTransition.status == "completed"`; outgoing sources are allowed to remain until then

The two-poll rule catches a take that lands and then rolls back. `/state` `ActiveSceneId` and `takeTransition.status` alone are never proof, because a cut completes before Program changes. The cut rule is strict by design: a target scene that legitimately keeps another baseline source on Program fails until the cue names a different scene. Choosing a fade avoids it.

**Failure** raises a specific detail and the existing rule applies: the cue stays `armed`, later cues do not run, and a take rewinds the rundown. Details: `no usable program observation before the take; nothing sent`, `program observation unavailable`, `program observation stale`, `scene attribution not live (<state>)`, `delivery sequence not advanced since before the take`, `source '<id>' not on program`, `outgoing source '<id>' still on program`, `fade not finished (takeTransition '<status>')`, `delivery stalled while confirming`, and `timed out, <last reason>`.

### Built

`volt/show_input.py`, wired in `dispatch.py` (`dispatch_item` and `take_item` take `show_input_actions`). `rooms.py` has `ProgramObservation` and `ProgramSource`, `CoreVideoProRoom.program_observed()`, and a scriptable `FakeRoom(program=[...])` (stale, holding attribution, outgoing still present during a fade, roll-back). `tests/test_show_input.py` covers every failure above, the happy path, dispatch and take, and a stub HTTP core.

### Unproven against a live core

- `videoSources[].kind` values for capture sources (and whether `capture:` ids appear in `sourceId` or `participantId`). Matching avoids `kind` for this reason.
- Fade behaviour: that `takeTransition.status` reads `completed` only after Program shows the incoming source alone, and the exact status strings.
- operationId correlation: the take reply carries no id we match against the snapshot, so a take made by someone else during the poll window could satisfy the evidence. Add correlation when the core exposes it.
- That `scene.select` then `transport.take` shows the slot's source for a scene that routes it. No live run has happened.
- How a transition mode is set. `transport.take` takes no args, so `fade` only changes the evidence required, not the transition the core performs.
- The 2-poll confirmation window against the real snapshot cadence and `deliverySequence` rate.
- The route action itself (#861).
