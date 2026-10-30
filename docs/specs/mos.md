# MOS export of the rundown

Status: built (`volt/mos.py`, `GET /api/shows/{show}/mos`, `volt mos export`). Export only: Volt opens no socket and sends nothing.

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
| `mosID` | this media object server: `--mos-id` / `mos_id`, default `volt.mos` |
| `ncsID` | the receiving system: `--ncs-id` / `ncs_id`, default `ncs.unset` |
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

## Out of scope

A TCP sender or listener (ports 10540/10541), `roReplace`, `roMetadataReplace`, `roStorySend`, `mosObj` publishing, and any ack handling. Those need an NCS to test against.

## Tests

`tests/test_mos.py`: golden file (`tests/golden/mos_ro_create.xml`), well-formedness, hostile slugs, empty show, items without media, arithmetic at 25, 29.97, 30, 50, 59.94 and 60, the route (auth, query errors, 404), and the CLI. The database tests also run on Postgres with `VOLT_TEST_PG_DSN`.
