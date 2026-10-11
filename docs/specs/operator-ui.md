# Operator UI

Static page at `/operator` (`control/session/operator/`: `index.html`, `operator.css`, `operator.js`). No build, no framework, no CDN. Served by `serve.py` with a same-origin-only Content-Security-Policy. The shell is open without a token so it can ask for one; every data route stays gated.

Run it against a fake room, no core needed: `cd control && python -m tests.operator_harness held --port 8791`, then open `/operator`.

## Data

- `GET /api/state?show=<id>` (new, read-only, token-gated). One poll every 2 s (6 s when the tab is hidden). Returns `shows`, `show`, `rundown` (the `/rundown` shape `{hold, items}`, unchanged), `cues` (each with `failure: {status, detail}` or `null`, the last dispatch failure kept until the cue fires) and `media` per item, `assets` (cleared, legal hold, `blockedBy`, file, checksum), `spans` with `versions`, and `room` (the room's recording lifecycle, or why it is unreachable). No show given, or an unknown one, means the newest.
- The page parses the rundown in one function, `parseRundown`, so a change of shape is one edit.
- Actions are the A2 POST endpoints. The page shows the state the server returns and, on refusal, the stable `code` and `reason`. Nothing is shown optimistically. A poll that began before an action finished is discarded.
- A failed cue stays `armed` on the server (taking again resumes there), so the page overlays "Failed" from the last take response until the next take succeeds.

## Screens

Show (take bar, rundown with as-run, cues, room), Record (start, lifecycle steps, stop and wrap), Rights (clear, legal hold), Publish (per-version status and URLs). `#show`, `#record`, `#rights`, `#publish`.

## Keys (Show screen)

| Key | Does |
| --- | --- |
| `Space`, `T` | TAKE. Space applies only when no button, link or field has focus (a focused control keeps its own Space; `Esc` drops focus; a mouse click drops focus after it acts). Ignored on key repeat, in a text field, within 1.5 s of the last take, while any sequenced request runs, while held, with no next item, or with no live connection. |
| `H` | Hold, or release when held. |
| Up, Down | Move between rundown rows (selection follows). |
| `S` | Skip the selected ready row. |
| `X` | Kill the selected ready row. A second `X` within 4 s confirms. |
| `1` to `4` | Switch screen. |
| `?` | List the keys. |

Take has no confirmation dialog: the guards above are the accident resistance, and the server refuses a second take while one runs. Kill and lifting a legal hold ask for a second press (button text changes to "Confirm ...", clears after 4 s or on Escape).

## Token

Entered in the Token dialog, kept in `sessionStorage` only (gone when the tab closes), sent as `Authorization: Bearer`. A 401 opens the dialog.

## State is never colour-only

Every status carries a glyph and a word. Focus rings are 2 px. `prefers-reduced-motion` stops the one spinner.
