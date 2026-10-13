# Operator UI

Static page at `/operator` (`control/session/operator/`: `index.html`, `operator.css`, `operator.js`). No build, no framework, no CDN. Served by `serve.py` with a same-origin-only Content-Security-Policy. The shell is open without a token so it can ask for one; every data route stays gated.

Run it against a fake room, no core needed: `cd control && python -m tests.operator_harness held --port 8791`, then open `/operator`.

## Data

- `GET /api/state?show=<id>` (new, read-only, token-gated). One poll every 2 s (6 s when the tab is hidden). Returns `shows`, `show`, `rundown` (the `/rundown` shape `{hold, items}`, unchanged), `cues` (each with `failure: {status, detail}` or `null`, the last dispatch failure kept until the cue fires) and `media` per item, `assets` (cleared, legal hold, `blockedBy`, file, checksum), `spans` with `versions`, and `room` (the room's recording lifecycle, or why it is unreachable). No show given, or an unknown one, means the newest.
- The page parses the rundown in one function, `parseRundown`, so a change of shape is one edit.
- Actions are the A2 POST endpoints. The page shows the state the server returns and, on refusal, the stable `code` and `reason`. Nothing is shown optimistically. A poll that began before an action finished is discarded.
- A failed cue stays `armed` on the server (taking again resumes there), so the page overlays "Failed" from the last take response until the next take succeeds.

## Screens

Show (take bar, rundown with as-run, cues, room), Record (start, lifecycle steps, stop and wrap), Rights (clear, legal hold), Publish (per-version status and URLs), Library (shots and clips of one asset). `#show`, `#record`, `#rights`, `#publish`, `#library`.

### Library

Built from `docs/mockups/shot-spans.html` (slice 4 of `shot-spans.md`). Pick an asset (those with a proxy and a closed hi-res first; the choice and the selection live in `sessionStorage`). The page then has the asset facts, a player on the proxy with a timeline of shot segments and clip bars, transport and mark controls, a filterable table (thumbnail, name, origin, in, out, length, transcript snippet, state), the selection tray, and a span editor (rename, trim, attach to a ready rundown item, delete with a second press). Every write goes to the server and the list is read back; a refusal shows its stable code and reason beside the work (`overlaps_shot` offers "Add as clip"). Empty and error states from the mockup are built: no assets, no asset chosen, loading, load error with retry, no shots yet, no proxy, still recording, and an asset with no frame rate. The state is a glyph and a word, as elsewhere.

Detection, split, merge and indexing (slice 4b): the **Detection** group under the filter has a Detect button (it reads Re-detect once a run exists, Try again after a failure), a Settings disclosure (threshold, shortest shot in frames; blank means the default, a change is a new run), and a status line read back from the span list's `detect` job on the existing poll: queued (with how long, and after 20 s a note that a runner must be running), running (with how long and an indeterminate bar; there is no percentage because the scan reports none), failed (with the job's reason), or the last run. Re-detect is a two-press action: the warning beside it says detected shots are replaced and shots made by hand and shots in use are kept. A repeat of a finished run says so and queues nothing. The button is `aria-disabled` rather than `disabled` while a run is active or the asset is recording, so keyboard focus is not dropped. In the span editor a **Split and merge** group takes a timecode (typed, or from the playhead) and offers Merge with next only when the next shot starts exactly where this one ends and neither is in use; the reason it is off is written beside it. Opening an asset with spans that predate the frame columns queues the `span-frames` backfill once and shows a calm "Indexing" line (a card when the list would otherwise be empty) until it ends; spans that cannot be placed on the essence's timebase are counted and said, not retried. Legal hold and not-cleared do not disable any of it.

Not on this screen yet, by design: NLE export (the tray's button is disabled, slice 6). The tray is client state: a list kept in this tab, never a stored object.

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

Take has no confirmation dialog: the guards above are the accident resistance, and the server refuses a second take while one runs. Kill and lifting a legal hold ask for a second press (button text changes to "Confirm ...", clears after 4 s or on Escape).

## Token

Entered in the Token dialog, kept in `sessionStorage` only (gone when the tab closes), sent as `Authorization: Bearer`. A 401 opens the dialog.

## State is never colour-only

Every status carries a glyph and a word. Focus rings are 2 px. `prefers-reduced-motion` stops the one spinner.
