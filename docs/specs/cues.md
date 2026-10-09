# Cues

Status: specified, not first. The room is CoreVideo. Volt does not become a switcher.

## Job

When an item fires, the room does what the item says. Input, PTZ preset, graphic, audio snapshot, replay.

## In scope

- Cue records on the item. Ordered. Each names a CoreVideo target.
- Dispatcher talks to the CoreVideo control API. The operator sees the result on the program monitor.
- Manual fire of one cue, before the clock exists, so the contract can be tested.
- On-air tally is red only when the director path is live. Amber means look. Red means air.

## Out of scope

Ross OverDrive, Grass Valley Ignite, and Vizrt Mosart as peers. They are the incumbents this crew is not buying. A later adapter can emit a cue ID. Volt is not in their latency path.
Lighting desks. A general show-control bus.

## Acceptance

- A cue for "camera 2, lower third" changes CoreVideo and nowhere else.
- A failed cue leaves the previous input up and marks the cue failed. It does not advance the rundown.
- Replay uses a span ID, not a path.

## Depends on

Rundown items. CoreVideo control API. Playout, for fired-by-clock. Manual fire can land earlier as a test.
