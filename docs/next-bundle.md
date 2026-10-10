# Next bundle

State on `main` plus PR #4: store hardened, room adapter, live record, cue dispatch, playout and as-run. 63 tests. Nothing has run against a live CoreVideo core, and nothing can drive any of it except tests.

Ordered. Each piece lists what done means.

## 1. Take means on air, with its cues

`go` moves the rundown. `dispatch_item` sends cues. Nothing joins them.

- Add `take_item(store, room, show_id)`: `go`, then `dispatch_item` for the new item.
- **Decision needed:** when a cue fails, does the item stay on air with the cue armed and a failure flag, or does the take roll back? The playout spec says the rundown does not advance on a failed cue. That reads as rollback.
- Done when: a failed cue is visible on the item, replaying the take resumes at the failed cue, and as-run in is written only after the decision's rule is met.

## 2. Live proof against a real core

The wire shapes come from CoreVideoPro source, not from a running core.

- Run record start, observe, stop, wrap against `127.0.0.1:8011`. Then stream start to a local SRT sink, and the lower third.
- Check the `/invoke` args format (`[true]` is assumed), the `outputSenders` snapshot key, `lowerThirdOnAir` casing, and the artifact path on a finished recording.
- Needs the owner to approve launching a core. CoreVideoPro's notes authorize control-API tests in the designated test meeting.
- Done when: a recorded clip is minted, finalized, wrapped and checksummed on disk, and the notes in `docs/rooms.md` say "run live" with the date.

## 3. CI and repo hygiene (small)

- GitHub Actions: run the tests with ffmpeg installed on Windows and Linux.
- `.gitattributes` for line endings. Git warns about LF to CRLF on every new file.
- Delete merged branches.
- Done when: a PR shows a green check.

## 4. An operator surface that can act

`serve.py` is read-only. The session can list, search and seek, and it cannot take, skip, start a recording, set rights, or publish.

- Rule from CoreVideoPro: design the screens first and show them, then code. The OHG Show tab was rejected for being bolted on.
- Mockups before any endpoint: rundown with take, skip and as-run; record state with the lifecycle; rights clear/hold.
- Then POST endpoints over the functions that already exist, with a bearer token off loopback.
- Done when: the owner has approved the screens and an operator can run a rehearsal show without a terminal.

## 5. show-input.take

The most important cue and the one with no adapter.

- Likely `input.assign` then `transport.take`, but the evidence field for what is on Program is not found yet. Find it in the snapshot (`programFrameHealth`, rendered sources) or add it in CoreVideoPro.
- Done when: the cue fires only when the observed Program source matches the cue.

## 6. The workers the docs promise

Each is a job on an existing asset. Idempotency and the job table are already in place.

- Speech worker that writes transcript word spans. Today `index_words` is fed by hand.
- Deliverable transcodes: house, OTT, social. Publish URLs are placeholders.
- Postgres as the running store. `schema.sql` exists and a test keeps it matched, but `Store` is SQLite only.
- Hold in playout, and a clock that fires on a schedule.
- Done when: a recorded clip becomes searchable text and three files with real URLs.

## Not now

MOS export, hardware playout, archive tier, a second NLE. Those stay on the roadmap.

## Suggested order

1 and 3 together this week, 2 as soon as a core is available, then 4 (screens first), 5, 6.
