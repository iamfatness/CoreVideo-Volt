# MOS export of the rundown

Status: the export is built (`volt/mos.py`, `GET /api/shows/{show}/mos`, `volt mos export`) and sends nothing. The sender is built too (`volt/mos_send.py`, see [The sender](#the-sender)) and is off unless `VOLT_MOS_DEVICES` is set.

## Conformance, stated plainly

The output **structurally follows MOS 2.8.5 `roCreate` as documented**. It is **unvalidated against a newsroom computer system (NCS) and against the MOS XSD/DTD**. The 2.8.5 specification (mosprotocol.com, "MOS Protocol Version 2.8.5 Final") embeds its DTD and XSD in sections 8 and 9, but only the part of the document before section 3.6 could be read when this was built, so the full declarations were never obtained. What was read, and what the element order relies on:

- `roCreate (roID, roSlug, roChannel?, roEdStart?, roEdDur?, roTrigger?, macroIn?, macroOut?, ...)`
- `story (storyID, storySlug?, storyNum?, mosExternalMetadata*, item*)`
- `item (itemID, itemSlug?, objID, mosID, mosAbstract?, objPaths?, itemChannel?, itemEdStart?, ...)`
- `mosExternalMetadata (mosScope?, mosSchema, mosPayload)`

Nothing here claims conformance. Until a real NCS (or the XSD) accepts a file, treat the format as a draft. The tests check well-formedness, exact output (a golden file), element order against the lines above, escaping and arithmetic; none of them is a schema validation.

Unknowns to settle against an NCS: the unit of `itemEdDur` (the spec text read does not state it; Volt writes frames at the item's timebase, and says so in the metadata), the exact element order after `itemEdStart`, whether `roEdDur` should be `HH:MM:SS` (its examples are), and whether a receiver wants `objTB`/`objDur` on the item rather than in a mosObj it has fetched. Volt does not fetch or send mosObj messages.

## Mapping

| MOS | Volt |
| --- | --- |
| `mosID` | in MOS this is the MOS device's id. The export's defaults (`--mos-id`, default `volt.mos`; `--ncs-id`, default `ncs.unset`) were written with the roles the wrong way round: for a file meant for a device, pass the device's id as `--mos-id` and Volt's own id as `--ncs-id`. The sender does this itself (device name as `mosID`, `VOLT_MOS_NCS_ID` as `ncsID`). |
| `ncsID` | the newsroom computer system's id: Volt, when Volt is the sender |
| `messageID` | always `1` (nothing is sent, so there is no sequence) |
| `roID`, `roSlug` | show id, show title |
| `roEdDur` | `HH:MM:SS`: the exact sum of every attached span (`frames / rate`, as a rational), rounded half up once. Hours may exceed 99. Items with no media or no usable rate add nothing. |
| `story` | one per rundown item, in position order; killed and skipped items are included (their status says so) |
| `storyID`, `storySlug`, `storyNum` | item id, slug, position |
| story `mosExternalMetadata` (scope `STORY`) | Volt namespace: `status`, `script` (if any), `cues` (only with `cues=1` / `--cues`) |
| `item` | only when a span is attached: `itemID` = `<item id>.1`, `itemSlug` = span label (else camera + tc in), `objID` = span id, `mosID`, `itemEdStart` = 0, `itemEdDur` = span length in frames |
| item `mosExternalMetadata` (scope `OBJECT`) | Volt namespace: `objTB`, `objDur`, `rate` (exact, `30000/1001`), `tcIn`, `tcOut` |

Everything Volt-specific lives under `urn:corevideo:volt:mos:1` inside `mosExternalMetadata`, so a receiver that ignores external metadata sees only standard elements. Cues are left out unless asked for.

## Durations and timebase

The span length is `frame_out - frame_in` (half open, as the NLE export), or, for an older span with no stored frames, the difference of its timecode labels on the essence's timebase (drop-frame honoured). `objTB` is the rate as people write it, at most three decimals: `25`, `29.97`, `30`, `50`, `59.94`, `60`, `23.976`. The exact rate is in `rate`. Arithmetic is exact (`Fraction`), so 107892 frames at 29.97 is one hour, not 59:56. A span whose essence has no frame rate is listed without a duration and a warning names it.

## Safety

Text is cleaned before it is written: characters illegal in XML (NUL, most C0 controls, lone surrogates, U+FFFE/U+FFFF) are dropped, slugs fold whitespace runs, scripts keep line breaks, and the XML library escapes `& < >` and attribute quotes. A hostile slug cannot add elements. `mos_id` and `ncs_id` are restricted to letters, digits and `_ . : -`. The export reads only; it writes no row, no audit entry, and moves no media.

## The sender

Volt is the NCS here: it connects out to a MOS device and sends running orders. Read from the MOS 2.8.5 text (2026-11-01): the device accepts NCS connections on its **upper port, 10541** (the default here, overridable per device); the lower port 10540 is where an NCS accepts connections from devices, and 10542 carries the object-list family, neither of which the sender uses. Messages are Unicode, high-order byte first (UCS-2, written as UTF-16BE with no byte-order mark and no XML declaration), wrapped in `<mos><mosID/><ncsID/><messageID/>…</mos>`; the NCS waits for a `roAck` on the same socket; a `heartbeat` is answered with a `heartbeat`.

**Turn it on** with `VOLT_MOS_DEVICES=name=host[:port],name2=host2` (the name is also the device's MOS id) and optionally `VOLT_MOS_NCS_ID` (default `volt.ncs`). Nothing set means nothing is sent. The runner steps the sender every two seconds; an unreachable device costs at most the 3-second connect timeout per backoff interval (2, 4, 8 … 60 s).

**What it sends.** `roCreate` the first time a show is sent, then `roReplace` (the same body) whenever the show's message changes, and `roDelete` for a show that has gone; `heartbeat` every 30 seconds. A show is live while any item is ready, on air or taking, and stays synced after that. Nothing hooks the code that edits a rundown: each step the sender builds the message for each live show, hashes it (ignoring the message id) and compares it with the hash the device last acknowledged, so a lost connection loses nothing and a restart resumes from the database (`mos_sync`, `mos_counter` and `mos_device`; migration 21). Message ids come from a persisted per-device counter.

**Acknowledgements.** A `roAck` with `roStatus` OK is success; anything else is a refusal. A refused `roCreate` created nothing, so it is retried after 15 seconds; a refused `roReplace` keeps the device's text and is retried. **A `roCreate` that is sent and gets no answer (timeout or a dropped connection) is never resent blind:** the show becomes `unconfirmed`, is shown on the Show screen's room panel, and waits for the desk: "It was created" (the next send is a `roReplace` that aligns the device) or "Send it again" (`POST /api/mos/{device}:{show}/resolve` with `{action: created|retry}`).

**Not sent:** `roMetadataReplace`, `roStorySend` (the complete body of a story, an optional Profile 4 message) and the roReq/roList reconciliation; a whole-show `roReplace` carries every change. The legacy roStory* messages are dropped by newer MOS versions and are not used.

**Unvalidated:** tested against a fake device socket server only (framing, acknowledgement matching, change detection, restart and reconnect, the unconfirmed rule). No real MOS device or newsroom system has accepted these messages.

## Tests

`tests/test_mos.py`: golden file (`tests/golden/mos_ro_create.xml`), well-formedness, hostile slugs, empty show, items without media, arithmetic at 25, 29.97, 30, 50, 59.94 and 60, the route (auth, query errors, 404), and the CLI. The database tests also run on Postgres with `VOLT_TEST_PG_DSN`.
