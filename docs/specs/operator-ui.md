# Operator UI

Static page at `/` and `/operator` (`control/volt/session/operator/`: `index.html`, `operator.css`, `operator.js`). No build, no framework, no CDN. Served by `serve.py` with a same-origin-only Content-Security-Policy. The shell is open without a token so it can ask for one; every data route stays gated.

Run it against a fake room, no core needed: `cd control && python -m tests.operator_harness held --port 8791`, then open `/operator`.

## Data

- `GET /api/state?show=<id>` (new, read-only, token-gated). One poll every 2 s (6 s when the tab is hidden). Returns `shows`, `show`, `rundown` (the `/rundown` shape `{hold, items}`, unchanged), `cues` (each with `failure: {status, detail}` or `null`, the last dispatch failure kept until the cue fires) and `media` per item, `assets` (cleared, legal hold, `blockedBy`, file, checksum), `spans` with `versions` and `label` (the editorial spans, clips and shots, plus any span a rundown item uses or that already has a published version, whatever its kind; transcript words that nothing uses are not in it, since owner ruling on `shot-spans.md` question 9; words are found through `library.search` and the Library), and `room` (the room's recording lifecycle, or why it is unreachable). No show given, or an unknown one, means the newest.
- The page parses the rundown in one function, `parseRundown`, so a change of shape is one edit.
- Actions are the A2 POST endpoints. The page shows the state the server returns and, on refusal, the stable `code` and `reason`. Nothing is shown optimistically. A poll that began before an action finished is discarded.
- A failed cue stays `armed` on the server (taking again resumes there), so the page overlays "Failed" from the last take response until the next take succeeds.

## Screens

Show (take bar, rundown with as-run, cues, room), Record (start, lifecycle steps, stop and wrap), Rights (clear, legal hold), Publish (clips and shots with per-version status and URLs; a passage of speech is published by first marking it as a clip in the Library), Library (shots and clips of one asset). `#show`, `#record`, `#rights`, `#publish`, `#library`.

### Library

Built from `docs/mockups/shot-spans.html` (slice 4 of `shot-spans.md`). Pick an asset (those with a proxy and a closed hi-res first; the choice and the selection live in `sessionStorage`). The page then has the asset facts, a player on the proxy with a timeline of shot segments and clip bars, transport and mark controls, a filterable table (thumbnail, name, origin, in, out, length, transcript snippet, state), the selection tray, and a span editor (rename, trim, attach to a ready rundown item, delete with a second press). Every write goes to the server and the list is read back; a refusal shows its stable code and reason beside the work (`overlaps_shot` offers "Add as clip"). Empty and error states from the mockup are built: no assets, no asset chosen, loading, load error with retry, no shots yet, no proxy, still recording, and an asset with no frame rate. The state is a glyph and a word, as elsewhere.

Detection, split, merge and indexing (slice 4b): the **Detection** group under the filter has a Detect button (it reads Re-detect once a run exists, Try again after a failure), a Settings disclosure (threshold, shortest shot in frames; blank means the default, a change is a new run), and a status line read back from the span list's `detect` job on the existing poll: queued (with how long, and after 20 s a note that a runner must be running), running (with how long and an indeterminate bar; there is no percentage because the scan reports none), failed (with the job's reason), or the last run. Re-detect is a two-press action: the warning beside it says detected shots are replaced and shots made by hand and shots in use are kept. A repeat of a finished run says so and queues nothing. The button is `aria-disabled` rather than `disabled` while a run is active or the asset is recording, so keyboard focus is not dropped. In the span editor a **Split and merge** group takes a timecode (typed, or from the playhead) and offers Merge with next only when the next shot starts exactly where this one ends and neither is in use; the reason it is off is written beside it. Opening an asset with spans that predate the frame columns queues the `span-frames` backfill once and shows a calm "Indexing" line (a card when the list would otherwise be empty) until it ends; spans that cannot be placed on the essence's timebase are counted and said, not retried. Legal hold and not-cleared do not disable any of it.

**Ingest** (panel above the asset picker, `#lib-ingest`). One field, "Path of the file, as the server sees it", and an Ingest button; Enter in the field submits, Space types. It sends `POST /api/ingest` with the path only, so the server's folder allow-list (`VOLT_INGEST_ROOTS`) decides. The button is `aria-disabled` while the request runs (the server probes and checksums inside the request). A refusal is written in words with the server's reason and `code:`: `path_not_allowed` (403: not inside an allowed folder), `ingest_disabled` (503: no folder configured on the server), `not_found` (404: no such file, or a folder), `bad_input` (422: not an absolute path), `engine_missing` (503: no ffprobe), `disk_low` (507: the quarantine disk cannot take the copy; the server maps it to its own code, not to `not_found`), `unauthorized` (opens the Token dialog) and `network`. Success shows the asset id, whether it was new or already ingested, a quarantine warning when the file did not parse, and one row per follow-on job (Copy and check, Proxy, Transcript, plus Shot detection if one is queued), each a glyph and a word. Progress rides the existing 2 s state poll: `assets[].jobs` lists only unfinished jobs (queued, running, failed, with the error), and a job that is no longer listed is Done. The transcript row says it waits until the asset is cleared; the proxy row says it waits for a runner. "Open this asset below" selects the new asset in the picker. The panel has no keys of its own and none of the Library or Show keys act inside the field.

The tray is client state: a list kept in this tab, never a stored object. Its **Export to NLE** button opens the Export tab.

**Publish list.** The Publish screen lists clips and shots (state `spans` default), not transcript words, plus any span a rundown item uses or that already has a published version. Its filter ("Find a clip or shot by name or words") matches label, the words a clip was made from, camera and id. To publish a passage of speech, mark it as a clip in the Library (Transcript tab). A word-span used by a rundown item still appears.

**Selection to export** (third tab, slice 6; mockup tab of the same name). Left: the selection in export order with a verdict per span (Ready, Ready with legal hold, Ready not cleared, or both, each a glyph and a word), an information banner when not-cleared spans are in the selection ("can still be exported. An export leaves the original source untouched, so it is allowed. Publish and air stay blocked for these spans until they are cleared on the Rights screen"), an information banner when held spans are in the selection ("can still be exported. An export leaves the original source untouched, so it is allowed. Publish and air stay blocked for these spans until the hold is lifted on the Rights screen"; a held row reads "Ready, legal hold" with the same point in short), the server's warnings, and a preview of the first 20 lines of the file. Right: format (xmeml or EDL), media (hi-res or proxy), sequence name (also the file name, shown before saving), start timecode (empty means 01:00:00:00), what the server's path map did to each media path (first three, "rewritten" or "as stored"; an EDL says it carries none), and the Export button. A dry run (`dry_run: true`) is sent on opening the tab and after every change of selection or option (250 ms after typing stops; an older answer is dropped), and the page shows only what the server said. The button is `aria-disabled` (focus stays) until the dry run has answered, with the reason written under it. Export sends the same request with `dry_run: false` through `fetch` (so the bearer token travels), then saves `content` from a Blob; a success note gives the file name, clip count, length and a short sha256, and says Volt keeps no copy ("Save again" repeats the download). Every refusal is shown in words with its stable code and the server's reason: `essence_open`, `not_local`, `no_proxy`, `proxy_mismatch`, `outside_source`, `mixed_rates`, `unsupported_rate`, `not_found`, `bad_input`, `bad_path_map`, plus `unauthorized` and `offline` from the page itself, with a one-press fix where there is one (Use the proxy, Use the hi-res, Keep only the N fps spans, Remove the missing span). A permanent notice says the formats are **not checked in an NLE yet**; it comes off only when `FORMAT_VERIFIED` in the script is set to true after the manual import check in `shot-spans.md` has a recorded result. Differences from the mockup: the "Path as the NLE sees it" drop-down is read-only text, because the path map is a server setting and the API has no per-request choice; the unverified notice is added; the 500-span limit and the start timecode are surfaced. The tab has no keys of its own, and the shot list's keys stand down on it.

Marks are typed or taken from the playhead into In and Out fields (so an asset with no proxy can still be marked), then added as a clip or as a shot. The playhead is a frame count read from the video (`floor(currentTime x rate)`, seeks aim a quarter frame in) turned into a label by the page's twin of `volt/timecode.py` (`framesToTc`, drop-frame included). A test runs that code under node against the Python library on 25, 24000/1001, 30000/1001 (drop and non-drop) and 60000/1001, around the minute and ten-minute boundaries, and on labels that do not exist. The page assumes the proxy shares the hi-res's start and rate (as the spec does) and warns when the proxy's length differs from the source by more than two frames.

### Mark from transcript (slice 5)

A tab beside "Shots and clips" (`#lib-tabs`; arrow keys move between the two tabs, the shot list's keys stand down while this one shows). Built from the mockup's third tab: the asset facts, the transcript with the shot starts inline (a line of its own when a shot starts a sentence, an inline `Shot N` marker when it cuts through one), a word cursor and range, and the "New clip from words" panel with first and last word, count, handles before and after (`-1`, `+1`, `+5`), a snap checkbox (on by default, as in the mockup) whose caption says which cut each end moves to, an optional name, the resulting in, out and length, and Add clip.

The page sends `from_words` (the two word IDs), `handle_in`, `handle_out` and `snap` and never a time. The in, out and length shown are a preview from the same arithmetic as the server; the flash after adding reports what the server returned, and a preview that would leave the source says so and the server refuses it (`outside_source`). Words come from the transcript read (first 3000, then "Load more words"), which lists only transcript words, so a word re-kinded to a clip is not on this screen. A refusal that means the list is stale (`not_transcript`, `not_found`, `frames_pending`) offers "Reload transcript".

Keys on the transcript (focus it with Tab): Left and Right move the cursor, Up and Down jump a sentence, Home and End, Shift extends from the anchor, Space picks the word under the cursor as a one-word range (again to drop it), Enter adds the clip, Esc clears the selection and, with none, lets focus leave. Mouse: click, drag, Shift-click. Each cursor move is announced (word, timecode, words selected). Space never takes on this screen.

Deviations from the mockup: there are no In and Out thumbnails in the panel (the thumbnail route cuts a frame for an existing span, not for a range that is not a span yet; the clip's own thumbnail appears on the shot list after adding), and the mockup's worked example shows only the out snapping while the specified rule also moves the in back to its shot's start; the page follows the specified rule and its caption states what will move.

### Media and the token

`<img>` and `<video>` cannot send an `Authorization` header. What the page does:

- Thumbnails: always `fetch()` (header attached when there is a token) into a `blob:` URL, three at a time, cached by span and frame range, revoked when the asset changes or a trim replaces the frame. One path with or without a token, and a trim fetches the new frame instead of a browser-cached one (the thumb route sets `max-age=3600` and takes no query).
- Proxy, no token (loopback): native `<video src="/api/assets/{id}/proxy">`, streamed and seeked by byte range. Nothing changes for loopback use.
- Proxy, token set: fetched with the header into memory first, on an explicit "Load proxy" press (it is a whole-file download), with a progress figure, refused above 512 MB before any body is read (the length comes from the response header), and played from a `blob:` URL. A growing proxy (asset still recording) is a snapshot until "Reload proxy".

Why this and not the alternatives: it changes nothing on the server's auth surface, puts no token in a URL (URLs reach logs, history and referrers), and needs no service worker (which needs a secure context, and a LAN address over http is not one). The cost is real: with a token the operator waits for the whole proxy, and a large proxy cannot be played. If that bites, the next step is a short-lived signed URL for the proxy route; it is not built. The CSP gained `img-src blob:` and `media-src 'self' blob:` and nothing else.

### Building the show (Show screen)

Below the rundown, a small panel (`#build`) and, under the Cues panel, an add-cue form (`#cue-add`). No new screen. All of it is forms and buttons with labels; there is no single-key shortcut, and Space types inside a field and never takes (the take rule is unchanged, and none of this is on the Library screen).

- **New item**: a name field and Add item (Enter works). The item goes last.
- **Edit the selected row** (a ready row only; otherwise it says why it cannot be edited): Rename; a picker of the clips ticked in the Library's Selection tray, with Attach clip; Detach clip when one is attached; Remove item (armed: the button turns to Confirm remove for 4 s, like Kill). Kill stays on the row and keeps the item in the list.
- **Reorder** is the row's existing up and down buttons, now sending `version` (`orderVersion` from `/api/state`). If another operator changed the list the server answers `stale_order`, the page shows "The list changed" and the refreshed list; nothing moved.
- **Add a cue** (selected ready row): a command picker from `GET /api/cue-commands` (unverified commands are labelled "(no verified adapter)" and a warning appears under the picker), a payload field, Add cue. Each unfired cue has a Remove button.
- **New show**: a disclosure with a name field; the new show opens.
- As-run times in the rundown show milliseconds when the server has them.
- Not built in the page: deleting a show. (Ingest is on the Library screen, below.)

Checked with axe-core 4.10 in a real browser against the harness (`library` scenario): no violations on the Show screen with the panel and the add-cue form showing, and again after the New show disclosure was opened and used.

**Attach from the Library selection and the stale list, checked in a real browser** (headless Chromium against `python -m tests.operator_harness library --port 0`): tick a clip in the Library, open Show, add an item, select it, choose the clip, Attach clip (the row reads "clip in to out", banner "Attached."), Detach clip (the row reads "no media", banner "Detached."). For the stale case a second operator reorders through the API after the page loaded; the page's next arrow press is refused `stale_order`, the banner reads "The list changed." with the server's reason, "Nothing moved. The rundown shown is now the server's current order; press the arrow again if you still want the move." and `code: stale_order`, and the rundown already shows the other operator's order. axe 4.10.2 (loaded through a route fulfilled from the same origin, since the CSP blocks other scripts) reported no violations on the Show screen with an attached clip, on the Show screen with the stale banner showing, and on the Library's Selection to export and Mark from transcript tabs.

## Keys (Show screen)

| Key | Does |
| --- | --- |
| `Space`, `T` | TAKE. Space applies only when no button, link or field has focus (a focused control keeps its own Space; `Esc` drops focus; a mouse click drops focus after it acts). Ignored on key repeat, in a text field, within 1.5 s of the last take, while any sequenced request runs, while held, with no next item, or with no live connection. |
| `H` | Hold, or release when held. |
| Up, Down | Move between rundown rows (selection follows). |
| `S` | Skip the selected ready row. |
| `X` | Kill the selected ready row. A second `X` within 4 s confirms. |
| `1` to `5` | Switch screen (5 is Library). |
| `?` | List the keys. |

## Keys (Library screen)

Handled only when the Library is the open screen, and never when a field has focus. Space is not TAKE here, and none of the Show keys (T, H, S, X, arrows) do anything here.

| Key | Does |
| --- | --- |
| `K` | Play or pause. |
| `,` `.` | Step one frame back or forward (repeats when held). |
| `[` `]` | Previous or next shot start. |
| `I` `O` | Put the playhead in the In or Out field. |
| `Enter` | Add a clip from the fields, only when no button, link or field has focus (in a field it submits the form; on a button it presses it). |
| `Space` | On a row's name: add or remove the row from the selection. A checkbox keeps its own Space. |
| `Enter` on a name | Seek the player to the row and open it in the editor. |

The Export tab has no keys: Tab, arrows inside the radio groups, Space on the checkbox and Enter on the button are the browser's own.

Take has no confirmation dialog: the guards above are the accident resistance, and the server refuses a second take while one runs. Kill and lifting a legal hold ask for a second press (button text changes to "Confirm ...", clears after 4 s or on Escape).

## Token

Entered in the Token dialog, kept in `sessionStorage` only (gone when the tab closes), sent as `Authorization: Bearer`. A 401 opens the dialog.

## State is never colour-only

Every status carries a glyph and a word. Focus rings are 2 px. `prefers-reduced-motion` stops the one spinner.
