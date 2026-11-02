# Volt: newsroom scripts and workflow, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

Plan: [2026-11-01-newsroom-scripts.md](../plans/2026-11-01-newsroom-scripts.md). House style follows [the remaining-build design](2026-10-31-remaining-build-design.md).

## Why

A rundown in a newsroom is a list of stories, and the story is a script that people write, argue over, approve and read aloud. Volt's rundown knows the second half of that (what airs, when, with which media and cues) and almost none of the first.

**What Volt has today** (read from the code, 2026-11-01): an item has a `slug` and a `script` text column. `POST /api/shows/{id}/items` and `POST /api/items/{id}/rename` set them (script limit 20000 characters, `ready` items only). Nothing in the operator UI edits the script at all (`operator.js` never reads `item.script`), and `/rundown` does not return it. MOS export sends it as the story body (`mos.script_text`). The Show screen shows front time, drift and back time, all from media length. There is no revision history, no status other than the playout status (`ready`, `taking`, `on air`, `aired`, `skipped`, `killed`), no record of who approved anything, no prompter output, no way to bring in wire copy, no templates, and deleting a show deletes its items.

**What newsroom systems have.** ENPS, Avid iNEWS, Dalet Galaxy and Octopus are generally known for: a story editor with formatting and instruction markup; word count and read time against an anchor's pace; story status and approval, with the approver recorded; a teleprompter feed; wire and feed intake into an inbox or "wire room"; floated (backup) stories that sit in the rundown without airing; rundown and story templates; and a searchable story archive. **This is general knowledge of those products, not an audit of any of them.** Exact feature names, behaviours and file formats differ per product and are not claimed here; the first customer's own practice is the reference, and several items below are marked unvalidated for that reason.

**What Volt lacks, in one line each:** an editor; read time; a status and approval workflow with an attributable approver; a prompter output; wire intake; floating; templates; an archive that outlives the show.

## Goals

1. A producer can write and revise a story's script in the operator UI, see word count and read time as they type, and see who changed it and when.
2. A story has an editorial status (`draft`, `ready`, `approved`, `killed`) separate from its playout status, a recorded approver, and an approval that stops being true when the text changes.
3. A prompter can show the show's approved, airable scripts in rundown order, following what is on air, from a plain browser page, with notes and crew instructions never shown to the talent.
4. Wire copy from RSS, Atom, NewsML-style XML or configurable JSON lands in an inbox; a human turns an inbox entry into a draft story. Nothing from a wire airs without a person.
5. A story can be floated (kept in the rundown, ignored by timing and by Go) and brought back.
6. A producer can create a show or a story from a template, and find a past story by words, slug, status or date.
7. Existing behaviour is unchanged for anyone who does not turn the new things on.

## Non-goals

- Real-time multi-user co-editing, presence, locks, comments and groups. Sign-in will be OpenID Connect and per-user identity and groups are specified separately (the identity and collaboration spec, written in parallel; this document assumes a verified user name per request and an optional group list, and links to that spec once it exists). Here, concurrent edits are handled by an optimistic revision check only.
- Anything on a physical prompter or a real newsroom system: ENPS, iNEWS, Dalet or Octopus interchange, MOS `roStorySend` of edited text, prompter vendor protocols. See "What the tests will and will not prove".
- AI drafting, summarising or translation of scripts (a separate, pluggable, local-first track).
- Rich text. A script is plain text with two small conventions (below).
- Remote control of prompter scroll speed from the desk in this release.
- A wire licence or rights engine. Volt records where copy came from; whether it may air is the customer's.

## Anti-goals

Failure even if it works:

- Anything that puts text or media on air without a person pressing the same buttons as today. Markup in a script never creates, fires or edits a cue. A wire story never becomes a rundown item without a human action. Approval is a gate that can only refuse a take, never cause one.
- An approval that outlives the words it approved.
- Notes meant for the crew reaching the talent's screen or an external system.
- A silent overwrite: two people save the same story and one loses work without being told.
- Network fetching driven by a request body (feed locations come from server configuration only).
- A new control that breaks the state language (icon, word, colour), the existing keys, or the same-origin CSP.

## Constraints

- Python standard library on the server; SQLite and Postgres both (`VOLT_DB`); a schema change is a migration with a forward test from historical shapes on both databases and a matching change to `schema.sql` and `store.SCHEMA` (a test compares them).
- Every new POST is classified in `ROUTE_ROLES` (a route missing there is admin-only, and a test fails); every successful write is audited with the token's user (`store.append_audit`, called by the POST handler on `ok: true`); new URL prefixes are added to `_AUDIT_KIND`.
- Roles stay `operator` and `admin`. "Approver" is a policy check behind one function (below), not a third role, until the identity spec lands groups.
- Operator UI: static files, same-origin CSP (no inline script or style, no `http(s)://`), no framework, icon sprite, dark and light themes from tokens covered by the contrast test, existing keys unchanged. A new colour state adds tokens to both theme sets and the contrast test.
- Tests use fakes and a frozen clock and never touch the network. A feed fetcher is injected.
- A network or parse failure never blocks the HTTP server, the clock or the runner's other jobs.

## The design

### Concepts

- **Story** is the item's editorial side: `slug`, `script`, `story_status`, revision history, approval. The item row stays the single source of truth; there is no separate story table.
- **Story status** is `draft`, `ready`, `approved` or `killed`. It is independent of `item.status`. A story killed here does not change `item.status`, and `item.status = 'killed'` (the existing playout kill) does not change the story status. The UI shows both and never uses one word for both: the playout kill reads "Killed from air", the editorial one "Story killed".
- **Revision** is an integer on the item (`script_rev`, starting at 1), bumped by every change to slug or script text. Saves carry the revision they were based on.
- **Read time** is `round(read_words / wpm * 60)` seconds, where `read_words` counts only the text the talent reads, and `wpm` is the show's pace (default 160, settable 60 to 400). 160 is a placeholder the customer sets, not a measured figure.

### Script markup

Plain text. Paragraphs are separated by a blank line. Two conventions, nothing else:

- `[...]` on a line is an **instruction** (for example `[SOT 0:23]`, `[CG: Mayor Ng]`, `[TWO-SHOT]`). It is shown dimmed in the editor preview and in a distinct style on the prompter, and is not counted as read words. It is display only: it never creates or fires a cue. An unclosed `[` is read text and is reported in `warnings`.
- A line starting with `//` is a **private note**. It is never sent to the prompter, the plain-text export or the MOS story body, and is not counted.

Example (the editor preview dims the bracketed and `//` lines; the prompter shows read text and instructions, never the note):

```
[CAM 2 - ANCHOR]
Council voted six to three last night to approve the new bridge.
// Check the vote count with Dana before 6pm
[SOT 0:23 - Mayor Ng]
The vote follows two years of debate over cost.
```

Read words here are the 21 in the two read lines (12 and 9); the instructions and the note count for nothing.

`volt/story.py` holds `parse_script(text) -> list[Block]` (`Block(kind: "read"|"instruction"|"note", text, para)`) and `script_stats(text, wpm) -> dict` (`words`, `readSeconds`, `instructions`, `notes`, `warnings`). A JavaScript twin in `operator.js` (`scriptStats`) gives live feedback; a shared fixture file drives a Python test and a node test so they cannot drift (the same method as the timecode twin). Words are whitespace-separated tokens, so a figure such as "$1.2 billion" counts two words while reading as four, and languages written without spaces are not counted correctly. These are stated limits, not bugs to fix here.

### Data model (a new migration)

One migration, forward-tested on SQLite and Postgres from every historical shape in `tests/historical_shapes.py`, adding:

- `item`: `story_status TEXT` (NULL reads as `draft`), `script_rev INTEGER` (NULL reads as 1), `approved_by TEXT`, `approved_at`, `approved_rev INTEGER`, `floated BOOLEAN`, `source_wire_id TEXT`.
- `show`: `read_wpm INTEGER` (NULL means the default).
- `story_rev (item_id, rev, slug, script, actor, at)`, primary key `(item_id, rev)`; the newest 50 per item are kept, older ones pruned in the same transaction as the save; the current text lives on `item`.
- `story_event (id, item_id, at, actor, kind, from_value, to_value, rev, note)`, append-only, kinds `status`, `float`, `import`, `template`. This duplicates the audit row on purpose: the audit table has no revision or structured detail, and "who approved revision 7" must be one indexed query.
- `wire_story (id, feed, external_id, headline, body, byline, published_at, received_at, state, item_id)`, unique `(feed, external_id)`, state `new`, `taken` or `dismissed`.
- `story_template (id, kind, name, body, created_by, created_at)`; `kind` is `story` or `show`; `body` is JSON.
- `story_archive (id, original_item_id, show_title, slug, script, story_status, approved_by, approved_at, aired_at, archived_at)`, written when a show is deleted.

Existing rows need no backfill: NULLs mean today's behaviour. Approval is `approved_rev == script_rev` with `story_status = 'approved'`.

### Status workflow

Allowed moves: `draft -> ready`, `ready -> draft`, `ready -> approved`, `approved -> ready`, any `-> killed`, `killed -> draft`. Anything else is 409 `bad_transition`. Approving requires `story.can_approve(principal, item)`, the one seam the identity spec fills with a group check; until then it returns true for any signed-in operator or admin, and a deployment can narrow it with `VOLT_APPROVERS` (comma-separated user names). A refusal is 403 `not_approver`. Changing the script text of an `approved` story sets it to `ready`, clears `approved_*`, and records a `status` event with the note "edited after approval". Approvals never carry across a copy, a template or a recurring clone: those start at `draft`, revision 1.

| From | To | Who | Effect |
|---|---|---|---|
| `draft` | `ready` | any operator | writer says it is finished |
| `ready` | `draft` | any operator | back to the writer |
| `ready` | `approved` | `can_approve` | records `approved_by`, `approved_at`, `approved_rev` |
| `approved` | `ready` | `can_approve` | clears the approval record |
| any | `killed` | any operator | story only; playout status untouched |
| `killed` | `draft` | any operator | revive; rev unchanged, approval cleared |

Every row also writes a `story_event`, so "who approved revision 7, and when" is one query, and the audit log gets its usual row.

Optional gate: with `VOLT_REQUIRE_APPROVAL=1`, `playout.advance` (Go), `rundown.take`, the scheduled take in the clock and any other caller of the take path refuse an item whose story is not `approved` at its current revision, with `StoryNotApproved` (a `PlayoutError`), HTTP 409 `story_not_approved`. Off by default. When the clock hits the gate it marks the schedule outcome failed with the reason, as it does for other refusals, and does not retry silently. A floated item is never taken.

### Floating

`item.floated` marks a backup story. A floated item keeps `item.status = 'ready'`, is passed over by `playout.advance` without being marked skipped, is left out of front time, back time, slot total and the prompter, and is omitted from the MOS export (a call, below). Floating is refused for items that are `on air`, `taking`, `aired`, `skipped` or `killed` (409 `not_floatable`). Un-floating puts it back at its position.

### Read time in the timing

An item with a media span keeps its media length as its planned length. An item without a span (an anchor read) uses its read time. Both are returned so the UI can show which one it used. This changes front time and back time only for span-less items, which today count as zero.

### API

All bodies reject unknown fields (`_unknown_keys`), errors use `{ok:false, code, reason}`. camelCase in payloads. "op" is the `operator` role.

| Route | Role | Notes |
|---|---|---|
| `GET /api/items/{id}/story` | any signed-in | `{slug, script, rev, storyStatus, approvedBy, approvedAt, approvedRev, floated, stats, revisions:[{rev, actor, at}], events:[...]}`; 404 `not_found` |
| `POST /api/items/{id}/script` | op | `{script?, slug?, baseRev}`; 200 `{rev, stats, storyStatus}`; 409 `stale_script` with `current:{rev, script, slug, actor, at}`; 409 `not_editable` for `aired`/`skipped`/`killed` items; 409 `story_killed`; 422 `bad_input` (over 20000 chars, empty slug). Saving identical text is a 200 no-op, same rev. |
| `POST /api/items/{id}/story-status` | op | `{status, baseRev, note?}`; 409 `stale_script`, 409 `bad_transition`, 403 `not_approver` |
| `POST /api/items/{id}/float` | op | `{floated: bool}`; 409 `not_floatable` |
| `POST /api/shows/{id}/reading-pace` | op | `{wpm: 60..400 \| null}` |
| `POST /api/items/{id}/copy` | op | `{show, position?}`; new item at `draft`, rev 1; 404 `not_found` |
| `GET /api/shows/{id}/prompter` | op ticket or any signed-in | feed below |
| `POST /api/shows/{id}/prompter-ticket` | op | `{ttlSeconds?}` (default 4 hours, max 12); returns `{url, expiresAt}` |
| `GET /api/shows/{id}/script?format=text` | any signed-in | plain text of the read blocks, no notes, for print or a text-pulling prompter |
| `GET /api/wire?state=new` | any signed-in | inbox list, newest first, paged |
| `POST /api/wire/{id}/take` | op | `{show, slug?, position?}`; creates a `draft` item whose first line is an instruction naming the source; 409 `already_taken`; the wire row records `item_id` |
| `POST /api/wire/{id}/dismiss` | op | 409 `already_taken` if taken |
| `GET /api/templates` | any signed-in | |
| `POST /api/templates` | op | `{kind, name, fromShow?|fromItem?|body?}` |
| `POST /api/templates/{id}/delete` | admin | |
| `POST /api/templates/{id}/instantiate` | op | story: `{show, position?}`; show: `{title}` creates a show |
| `GET /api/stories?q=&status=&show=&from=&to=&limit=&cursor=` | any signed-in | live items and `story_archive` together, each row marked `archived`; `q` matches slug and script with a case-insensitive, escaped `LIKE` |

`/rundown` items gain `storyStatus`, `scriptRev`, `words`, `readSeconds`, `floated`, `lengthSource` (`media`, `read`, `none`); the script text itself is not added to `/rundown` (it is large and polled every 2 seconds). `/rundown` also carries `readWpm` at the top level. `rename` stays and routes its script change through the same function as `/script`, with `baseRev` optional there so old callers keep working, but a rename that changes the script still bumps the revision and drops an approval.

The wire intake needs no public route to configure feeds: `VOLT_WIRE_FEEDS` is a list of `name=kind@url` (kinds `rss`, `atom`, `newsml`, `json`), JSON feeds take a mapping from the feed's field paths to `externalId`, `headline`, `body`, `byline`, `publishedAt` from a mapping file named in `VOLT_WIRE_MAPPINGS`. Nothing set means nothing fetched.

### Prompter feed

`GET /api/shows/{id}/prompter` returns `{show, version, wpm, current, items:[{id, slug, status, rev, blocks:[{kind, text, para}]}]}`. Included: items that are not killed (either sense), not floated, not `aired` or `skipped`, and, when `VOLT_REQUIRE_APPROVAL=1`, only `approved` ones. Notes are removed on the server. `version` is a hash of the content so the page repaints only on change. `current` is the on-air or taking item.

`/prompter?show=<id>` is a static page (`prompter.html`, `prompter.js`, `prompter.css` in the operator directory, added to `STATIC`) with large text, a mirror toggle, size and speed controls kept in `localStorage`, auto-scroll that holds still until started, and a jump to the current item. It polls the feed every 2 seconds. It authenticates with a **prompter ticket**: an HMAC ticket scoped to one show and one purpose, issued by `tickets.issue` with a new kind, so the prompter computer never holds an operator token. The ticket rides in the query string like a media ticket, so it can reach logs: it is short-lived and read-only. Instructions render as dimmed bracketed text, never as part of the scrolling read text's cadence. The page works in both themes.

### Wire intake

A periodic task in the runner (`runner.Periodic`, like the others, off when no feed is configured) fetches each feed with a timeout, a size cap and `http`/`https` only, hands the bytes to a parser, and upserts `wire_story` on `(feed, external_id)`; an updated item from the wire updates a `new` row and leaves a `taken` row alone (the story was copied at take time). Parsers are pure functions of bytes: `parse_rss`/`parse_atom`, `parse_newsml`, `parse_json(mapping)`. XML with a `DOCTYPE` or `ENTITY` is refused before parsing, because the standard library parser is not hardened against entity expansion. HTML in a body is reduced to text. The NewsML parser handles a documented minimal subset (headline, byline, body text), and the AP-style JSON path is a mapping, not a built-in schema, because Volt has no verified sample of any agency's payload. A feed that fails records its last error and next attempt; one bad feed does not stop the others.

### Templates

A `story` template is a slug pattern and a script skeleton (including instruction lines). A `show` template is a list of story templates, slot length and pace; cues are carried (as the recurrence clone does today) but media spans are not, since a span belongs to a past asset. Instantiating always produces `draft` stories. The existing recurring-show clone (`clock.materialise_recurring`) copies `script`; after this change it also resets `story_status`, `script_rev` and approval fields on the clone, and does not copy `floated`.

### Archive

`building.delete_show` writes each item into `story_archive` in the same transaction as the deletion. `GET /api/stories` is one query over live items and the archive, newest first, cursor-paged. Search is `LIKE`, which is adequate for thousands of stories and is not full text; the limit is stated, and a full-text index per backend is left for when it is measured to be slow. Copying a story out of the archive uses `POST /api/items/{id}/copy` with the archive row's id (`archive:<id>`) and always creates a draft.

### UI

New **Scripts** screen (`#scripts`) in the rail for the Producer and Operator workspaces (read and edit in both; workspaces are layouts, not permissions). Three regions:

1. **Story list** (left): rundown order, each row with number, slug, state chips (story status, floated, playout status) and `words` / read time. Filter by story status. Floated rows are grouped under a "Floated" heading and keep their position on un-float.
2. **Editor** (centre): slug field, a plain `<textarea>` (monospace, no contenteditable), a parsed preview beside it showing read text and dimmed instructions, a footer with words, read time, the revision, who saved it and when, and an unsaved chip. Buttons: Save, and the status control, which offers only the legal next moves for the current status (Mark ready, Approve, Take back to ready, Kill story, Revive). Approve needs two presses (the existing arming pattern). Save is also Ctrl+S inside the textarea only; that key is added to the keys table in `index.html` and the spec, and no existing key changes. Editing is disabled, with the reason in words, for `aired`, `skipped` and `killed` items.
3. **Side panel** (right) with three tabs (a tablist, arrow keys as in the Library): **Wire** (inbox rows with source, time, headline, Take into show, Dismiss), **Templates** (list, New from this story, Instantiate), **Archive** (search field and filters, results, Copy to this show).

Conflicts: a `stale_script` refusal keeps the editor's text, shows "Changed by <name> at <time>" with the other version's text, and offers Keep mine (re-save on top of the new revision) or Take theirs. It never overwrites silently and never discards the editor's text. The Show screen row gains the story chip, a floated marker, and the read-time length marker (`~0:42 read`) where a span-less item takes its length from the script. The Show screen's header adds the pace control to the build panel (read-only in the Operator layout, as the slot is).

State chips (icon, word, colour, all from the existing sprite and state map where possible; new sprite ids for a script, a wire item and a float marker): Draft (neutral), Ready (informational), Approved (ok), Story killed (fail), Floated (neutral), Unsaved (pending). Colours reuse existing tokens; if a new token is needed it is added to both themes and the contrast test in the same task.

Accessibility: the textarea has a visible label; chips carry their word; the live word count updates an `aria-live="polite"` region at most once per second; the conflict dialog traps focus and returns it; axe is run in both themes for the Scripts screen and the prompter page.

### Behaviour rules and edge cases

- Two saves at the same revision: the first wins, the second gets `stale_script` with the winner's text. A save based on the current revision with identical text changes nothing.
- A script over 20000 characters is 422 and the editor shows the count against the limit as it nears it.
- Approving a story whose item is killed from air, skipped or aired is refused (`not_editable`).
- Editing an on-air story is allowed: it bumps the revision, drops approval and updates the prompter on its next poll; it does not touch playout. (A call below.)
- Killing a story whose item is on air does not take it off air; the UI says so.
- Deleting a show archives its stories first; if the archive write fails the delete fails.
- A wire item taken twice, by two people at once, is one item: the take is a conditional update on `state = 'new'`.
- A duplicate external id from a feed updates, never duplicates. An empty or malformed feed leaves the inbox untouched and records the error.
- Prompter text is HTML-escaped by construction (text nodes only); script and wire text are never inserted as markup anywhere.
- Revision pruning never removes the revision an approval points to.
- Time stamps are UTC text like the rest of the schema.

## What the tests will and will not prove

**Will prove (with fakes):** markup parsing and counting against shared fixtures in Python and JavaScript; save, revision, conflict and pruning behaviour; every legal and illegal status transition; approval dropping on edit; the approval gate on Go, take and the scheduled take; floating's effect on Go, timing and the prompter; the prompter feed's content rules (notes absent, floated absent, order, `current`); ticket scope (a prompter ticket cannot read another show or any other route, and an expired one is refused); parsers against fixture files of each format; refusal of DOCTYPE feeds; inbox idempotence and the double-take race; template and clone semantics; archive-on-delete atomicity and search; the migration forward on both databases; `ROUTE_ROLES` and audit for each new POST; UI behaviour under node and axe in both themes.

**Will not prove:** that any of this matches how a real newsroom, ENPS, iNEWS, Dalet or Octopus works; that wire payloads from a real agency parse (fixtures are written from general knowledge of the formats and are unvalidated); that a physical prompter or its software can use the feed or page; that the read-time default matches any real anchor; word counts for languages written without spaces; behaviour under many simultaneous editors (optimistic checking only); search quality at archive scale. Each of these is worded as unvalidated in the shipped docs, and the first customer's rehearsal is where they get checked.

## Left to the builder

Column types and index definitions; the exact `LIKE` escaping per backend; the NewsML subset boundaries after reading the published NewsML-G2 documents (not this one); fixture contents; sprite artwork; where in the build panel the pace control sits; the diff view in the conflict dialog (a plain side-by-side is enough); wording of refusals within the existing `code`/`reason` pattern.

## Calls I made (for the owner to check)

- Story status is separate from playout status, rather than extending `item.status`, so existing playout code and tests keep their meaning.
- Approval is bound to a revision and falls back to `ready` when the text changes.
- The approval gate on air is an environment switch (`VOLT_REQUIRE_APPROVAL`), off by default, rather than a per-show setting. A per-show setting needs another route and UI; it can follow.
- `draft -> approved` directly is not allowed; the writer marks `ready`, then an approver approves.
- Editing is allowed on an on-air story (newsrooms do this live); it is blocked after `aired`.
- Floated items stay `ready`, are skipped silently by Go, and are left out of the MOS export. How real systems signal a floated story over MOS is not verified here.
- Span-less items use read time as their length; items with media keep the media length.
- `[...]` for instructions and `//` for private notes are my markup choice, not a copy of any product's.
- Feed locations are server configuration, not API input, to avoid server-side request forgery from a request body.
- A prompter ticket (HMAC, one show, hours) instead of giving the prompter an operator token.
- Archive is a snapshot at show deletion plus a search over live items, not a second store written continuously.
- Search is `LIKE`, not full text.
- Save is explicit (button and Ctrl+S in the editor), not autosave, so a revision means a deliberate save.
- The MOS story body drops private notes and keeps instruction lines; whether a given NCS wants instructions inline is unvalidated.

## Open questions

1. May a writer approve their own story? (This decides whether `story.can_approve` also compares against the last editor.) Until you say, any operator can approve any story.
2. Should the first customer run with `VOLT_REQUIRE_APPROVAL` on (an unapproved story cannot be taken) or only record approvals?
3. Which wire services and formats does the first customer actually receive, and under what licence terms may their copy be stored and edited in Volt?
4. How does the first customer's prompter get its text today (a browser page, a text or RSS pull, MOS from the newsroom system)? That decides whether the browser page is enough or a second output is needed.
