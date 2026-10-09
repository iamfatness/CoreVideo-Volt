# Playout

Status: specified, not first. Ships after the rundown is boring.

## Job

The clock lives here. Items go to program in order. Secondary events ride the item. As-run writes back.

## In scope

- Software playout of the rundown to a program output.
- Next and take. Hold. Skip.
- Secondary events as timed cues on the item, dispatched by the cue spec.
- As-run in and out on the item.
- A hardware frame is an output adapter. It receives a conformed file and the asset ID. It is not the scheduler.

## Out of scope

200-channel origination. SCTE insertion as a product. A master-control switcher. Redundant chain design beyond a single program output and a recorded as-run.

## Acceptance

- Take advances the rundown and the as-run matches the clock.
- A killed item is skipped and the as-run says so.
- Restart recovers the on-air item from the rundown, not from a file mtime.

## Depends on

Rundown. A conformed essence. Cue dispatch, if secondary events fire the room.
