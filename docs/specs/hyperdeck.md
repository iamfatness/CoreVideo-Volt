# HyperDeck rooms

Status: built (`volt/hyperdeck.py`; room kind `hyperdeck`). Blackmagic HyperDeck disk recorders over their Ethernet protocol, driven by cues like any other room.

## Configuration

`VOLT_HYPERDECK_HOSTS=A=10.0.0.9,B=deck2:9994` (`name=host[:port]`, default port 9993). Names are what a cue's payload uses. Nothing set means no deck is configured; `volt run` starts the clock when any room is configured (CoreVideo Pro or a deck). The cue picker offers the deck commands only when a deck is configured; a deck cue stored without one is accepted and reported `unsupported` when taken.

## Cues

Room `hyperdeck`, commands in the existing dotted style: `deck.play`, `deck.stop`, `deck.goto`, `deck.record.start`, `deck.record.stop`. The payload names the deck and optionally a clip, as JSON `{"deck": "A", "clip": 3}` (a clip number or a clip name) or the shorthand `A`, `A/3`, `A/intro.mov`. `deck.play` with a clip goes to it first; `deck.goto` needs one; `deck.record.start` with a name records to that clip name.

## Fired means observed

A cue is `fired` only when the deck's `transport info` shows what was asked: `status: play` after `deck.play`, `stopped` or `preview` after `deck.stop`, the clip id after `deck.goto`, `record` after `deck.record.start`, anything but `record` after `deck.record.stop`. A `200 ok` alone is never enough; dispatch polls with the same attempts and interval as the CoreVideo handlers. A deck that refuses (a `1xx` reply, with its own words in the cue's failure detail), does not answer within 3 seconds, closes the connection, cannot be reached, is not a HyperDeck (no `500` greeting), or is not named in the configuration is a **failed** cue, never a hang. Every cue opens its own short connection and closes it.

## A deck cue never rewinds a take

A deck cannot be un-played, so (owner ruling 2026-10-31) a failed or unsupported cue in a room other than CoreVideo Pro is **advisory**: it is recorded (`fail_status`, `fail_detail`) and shown red on the Show screen, the item stays on air, and the cues after it still run. A failed CoreVideo Pro cue still rewinds the take as before ([playout.md](playout.md)).

## Protocol notes

One text command per line (`play`, `stop`, `record`, `goto: clip id: 3`, `transport info`, `clips get`); the deck answers with a numbered block (`200 ok`, `208 transport info:` fields, `205 clips info:` lines, or a `1xx` failure) and greets a connection with `500 connection info:` (its protocol version is kept for the log). Asynchronous `5xx` notices that arrive between a command and its reply are skipped. This follows the HyperDeck Ethernet Protocol as published by Blackmagic (protocol 1.x).

## What the tests prove, and what they do not

Proved against a fake deck on a loopback socket: parsing, framing (including a notice in the middle of a reply), the fired-on-observation rule (a deck that says `200 ok` and never plays is not fired), refusals with the deck's words, timeouts instead of hangs, unreachable and unknown decks, clip lookup by name, and that a failed deck cue leaves the take, the on-air item and the later cues alone. **Not proved:** behaviour on a real deck: clip numbering across disks, timing, a deck that is recording or has no disk, other models and protocol versions.
