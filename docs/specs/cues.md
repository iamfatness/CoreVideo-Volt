# Cues

A cue is a room, a command, and a payload on the item.

CoreVideo Pro is the first room. Its commands come from the typed control contract in CoreVideoPro: show input take, overlay take, audio snapshot, record arm, record start, stream start. vMix and OBS are other rooms behind the same record.

A take arms the cue. Dispatch sends it to that room's adapter and waits for the acknowledgement, then the snapshot. A failed cue stays armed and the rundown does not advance.

**Failures are remembered.** When a cue fails or is unsupported, the status and reason are stored on the cue (`cue.fail_status`, `cue.fail_detail`; migration 8). `status` stays `armed`, so replay is unchanged: fired cues are skipped, the failed one is retried, later cues wait. The record survives later takes and server restarts and is cleared when the cue fires (or by `cues.clear_failure`). `GET /api/state` reports it per cue as `failure: {status, detail}`, or `null`.

See [rooms](../rooms.md).

This cue track is parallel to the MAM layer in [foundation](foundation.md#room-track-parallel). Foundation keeps the `show-input.take` evidence on this page; a CoreVideo Pro route action (#861) only changes `show_input_actions`, in a later plan, not the handler. `record.arm` and `audio.snapshot` stay unsupported. Until a live Program take is recorded in rooms, the Unproven list below stays.

## show-input.take

Put a source on Program and fire only when the core shows it there.

**Payload.** A bare string is the source id (`zoom:2`, `media:clip-7`, `capture:hdmi`, or bare). A JSON object may add `scene`, `slot` (integer), `route` (defaults to `ohg-host`) and `transition` (`cut`, the default, or `fade`). The default pre-take needs `scene` and `slot`.

**Sequence.**

1. Read `GET /snapshot` for a baseline. If the envelope is unavailable or stale, or `programFrame.deliverySequence` is missing, fail before sending anything.
2. Run the configured pre-take actions, a list of `{action, args}` templates. Placeholders `{scene}`, `{source}`, `{slot}` and `{route}` are filled from the payload. The default is `input.assign [{slot}, {source}]` then `scene.route.set [{scene}, {route}, {slot}]`. `{route}` is `ohg-host` when the payload omits `route`.
3. Send `transport.take` with no args. Its reply is an ack and nothing more.
4. Poll `GET /snapshot` with the same attempts, interval and sleep as the other handlers.

`input.assign [slot, sourceId]` alone is not enough. It edits a slot and puts nothing on Preview or Program. The default therefore also sends `scene.route.set` (CoreVideo Pro [PR #953](https://github.com/iamfatness/CoreVideoPro/pull/953) / #861). Override with `VOLT_SHOW_INPUT_ACTIONS` if the installed core does not have that action yet. The handler and its evidence do not change.

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

`volt/show_input.py`, wired in `dispatch.py` (`dispatch_item` and `take_item` take `show_input_actions`). Serve and the clock pass `VOLT_SHOW_INPUT_ACTIONS` (JSON list of `{action, args}`; unset uses the default). `rooms.py` has `ProgramObservation` and `ProgramSource`, `CoreVideoProRoom.program_observed()`, and a scriptable `FakeRoom(program=[...])` (stale, holding attribution, outgoing still present during a fade, roll-back). `tests/test_show_input.py` covers every failure above, the happy path, dispatch and take, and a stub HTTP core. The operator cue picker labels `show-input.take` unverified (`not live-proven`) until a live Program take is recorded in rooms.md; a take still runs the adapter.

### Unproven against a live core

- `videoSources[].kind` values for capture sources (and whether `capture:` ids appear in `sourceId` or `participantId`). Matching avoids `kind` for this reason.
- Fade behaviour: that `takeTransition.status` reads `completed` only after Program shows the incoming source alone, and the exact status strings.
- operationId correlation: the take reply carries no id we match against the snapshot, so a take made by someone else during the poll window could satisfy the evidence. Add correlation when the core exposes it.
- That `input.assign` then `scene.route.set` then `transport.take` shows the slot's source on Program. No live run has happened.
- How a transition mode is set. `transport.take` takes no args, so `fade` only changes the evidence required, not the transition the core performs.
- The 2-poll confirmation window against the real snapshot cadence and `deliverySequence` rate.
- The route action itself ([CoreVideo Pro PR #953](https://github.com/iamfatness/CoreVideoPro/pull/953) / #861), until it is on the core used for a live Program take.
