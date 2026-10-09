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

The wire shape comes from CoreVideoPro source. It has not been run against a live core yet.
