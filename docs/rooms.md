# Rooms

A cue names a room and a command. The dispatcher sends that command to the room adapter. CoreVideo Pro is the first room.

## CoreVideo Pro

The control contract lives in `iamfatness/CoreVideoPro`, `contracts/README.md`. The shell and other clients talk to the media core with typed commands. Existing clients already use HTTP, WebSocket, and OSC. The families Volt will call:

- Show inputs and take
- Scene, overlay, and lower third
- Audio route and snapshot
- Record arm and record start
- Stream start

Record start is also the MAM mint. The asset ID comes back with the recording session.

A cue payload names one of those commands and its arguments. Dispatch waits for the command acknowledgement. An acknowledgement is accepted, not proof the frame reached program. The adapter reads the following snapshot before it marks the cue fired.

## Other rooms

vMix, OBS, and any mixer with a developer API are another adapter behind the same cue. The rundown stores `room` and `command`. It does not store a CoreVideo method name.

The first adapter is CoreVideo Pro. The next adapter is whichever mixer the show is already on.

## Built

`control/volt/rooms.py` has the room interface, a scripted `FakeRoom`, and `CoreVideoProRoom`, which speaks `POST /invoke` and `GET /snapshot` on the control API (port 8011, optional bearer token). `control/volt/live.py` records against a room:

- `start_recording` sends `transport.record.set [true]`, then mints only once the snapshot shows `recording.lifecycle.state` producing, fresh, not failed, with an `artifactPath`. The asset key is `cv:<sessionId>`, so a replay does not mint twice.
- `finish_recording` sends `[false]`, then wraps only on `completed` with `finalized` true for the same session. `failed`, `interrupted` and unknown states never wrap.

Run live 2026-10-13 against CoreVideo Pro on `127.0.0.1:8011` (`bench/live_record_check.py`): `transport.record.set [true]` is the right shape, but the core refuses it with HTTP 422 ("Cannot set recording right now (the control is unavailable in the current state)") while the engine is off; `transport.engine.set [true]` first. Then `recording.lifecycle` went to `producing`/`healthy` with an `artifactPath` and a `corevideo-recording-...:<n>:1` session id, the asset minted only at that point, `[false]` ended it as `completed` and finalized, and the wrapped sha256 matched the file on disk (a 6.7 s 1080p60 H.264+AAC `Program.mp4`; the recording folder also held one iso per participant and a `manifest.json`). A core can refuse with 422, so `LiveError` on a refused invoke is the expected path when the engine is off.

Lower third, live: `graphics.lowerThird.set [true]` was accepted; `GET /state` `lowerThirdOnAir` went true as the phase moved `building-in` to `on-air`, and false after `[false]` through `building-out`.

Not run: `stream.start`. This core's saved stream destination was a platform RTMP endpoint, so starting it would have gone live; the stream cue stays unproven until it can be pointed at a local SRT listener (close the app, edit `production-output-preferences.json`, restore it after). Record and lower third have since been run through the cue dispatcher (below). The `outputSenders` snapshot key was not exercised, and `recording` is absent from the snapshot until the first session, so `observe()` reads `unknown` before it.

## Cue dispatch

`control/volt/dispatch.py` sends an item's armed cues to the room in order and marks each `fired` only when the room shows the effect. It stops at the first cue that does not land. That cue stays `armed`, and fired cues are skipped on replay.

| Cue command | Control action | Evidence |
|---|---|---|
| `record.start` | `transport.record.set [true]` | `recording.lifecycle` producing, fresh, with an artifact path. Mints the asset. |
| `stream.start` | `transport.stream.set [true]` | A sender in `outputSenders.senders[].lifecycle` producing and fresh |
| `overlay.take` | `graphics.lowerThird.set [on]` | `GET /state` `lowerThirdOnAir` matches |
| `show-input.take` | configurable steps (default `scene.select`), then `transport.take` | `GET /snapshot` `programFrame.videoSources[]` holds the source, attribution `live`, `deliverySequence` advanced |

`record.arm`, `audio.snapshot` and every other room are reported `unsupported` and stay armed. Each needs a control action and a piece of evidence that are known before it gets a handler. Same status as above: wire shapes come from source and have not run against a live core.

`show-input.take` reads Program through `CoreVideoProRoom.program_observed()` (`GET /snapshot`, `programFrame`). `input.assign` alone does not put anything on Program, and no control action routes a slot into a scene yet ([CoreVideoPro #861](https://github.com/iamfatness/CoreVideoPro/issues/861)), so the steps before `transport.take` are configuration. Rules, failure details and the list of unproven live assumptions are in [cues](specs/cues.md#show-inputtake).

### Live run through the dispatcher (2026-10-13)

`control/bench/live_cue_check.py` takes three rundown items against a real core (engine on, throwaway database, no stream). Take 1 (`record.start` then `overlay.take on`) fired both cues; the core showed producing/healthy and `lowerThirdOnAir` true. Take 2 (`overlay.take off`) fired and the core showed it off. Take 3 (`record.arm`, no adapter) came back `unsupported`, the rundown rewound to item 2 and the cue stayed armed. The recording was then wrapped and its sha256 matched the file on disk. One run, one rig, 1080p60 H.264. Still not run: `stream.start` and the `outputSenders` key.

