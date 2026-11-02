# Volt: editor integration (export verification, then an Adobe Premiere Pro panel), design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

## Why

Volt's answer to "I found the clip, now how do I cut it?" is a file. `POST /api/exports/nle` writes xmeml, CMX3600 EDL and FCPXML (`control/volt/nle.py`: `build_xmeml`, `build_edl`, `build_fcpxml`, `export`), and an editor imports it by hand. None of those three formats has ever been opened in a real editor. The operator page says so on purpose: `const FORMAT_VERIFIED = false;` in `control/volt/session/operator/operator.js` (pinned by a test in `control/tests/test_operator_ui.py`), and `docs/specs/shot-spans.md` ends its "Manual import check" with "Result: not yet recorded." The roadmap lists "a second NLE" and the import check as open. There is no panel inside any editor.

Asset-management products put a panel inside the editor so that the editor never leaves the timeline: search, preview a proxy, drag a clip or its in and out onto a sequence, write markers back, and swap proxies for hi-res at the end. Iconik, Dalet, Avid MediaCentral and Cantemo are the names the owner gave. **Honest caveat: what those products do is from general knowledge, not from an audit.** I have not installed them, read their current documentation, or compared feature lists. Treat the paragraph above as the shape of the job, not as a verified feature matrix. The only claims in this document that are checked are the ones about Volt's own code, which I read on 2026-11-01.

The two halves belong together. A panel that drops a proxy onto a timeline relies on the same arithmetic as the exports (frame counts at a rational rate, source timecode start, drop-frame labels). Proving that arithmetic in a real editor first, with a repeatable check, tells the panel builder what Premiere does with a clip's start timecode before any panel code depends on it.

## Goals

1. Verify the three export formats in real editors, with a check a person runs once by hand and a script re-runs afterwards, and record the result in `docs/specs/shot-spans.md`.
2. Ship an Adobe Premiere Pro panel (UXP) that: signs in without a password typed into Premiere; searches and browses the Library with proxies; drops a proxy, or a clip's in and out, onto the timeline or into the project; writes Volt spans into the project as markers and sequence markers and reads markers back as spans; and relinks proxies to hi-res when the editor publishes.
3. Show the rights gate (cleared, legal hold, archived) in the panel on every clip, and have the server enforce it where a hi-res file would be handed out.
4. Keep the panel a client of Volt's HTTP API and nothing else. No database access, no file share assumed, no second source of truth.
5. Make every part that does not need Premiere testable without Premiere, and write every part that does as unvalidated until a person has run it.

## Non-goals

- DaVinci Resolve and Avid panels. Resolve's integration surface (workflow integration plugins, scripting) and Avid's (a CEP/other panel model or AAF/ALE/EDL interchange) are separate pieces of work. The export verification in this spec does open the three files in Resolve, because the exports already claim to serve it, and records the Avid position as "EDL only, untested" (see Calls I made).
- Identity. A separate spec covers OpenID Connect and per-user identity. This spec assumes a per-user identity exists (today: `volt_user` rows and `vtk_` tokens in `control/volt/users.py`; the roles are `operator` and `admin` only) and defines only the hand-off from a signed-in browser to the panel.
- Publishing from the panel. The panel never calls `act_publish`, never marks anything cleared, never sets or clears a legal hold, never touches a rundown, a cue or a room.
- Transcoding in Premiere, ingest from Premiere, or exporting a sequence back into Volt as media. The panel moves references and markers, not rendered output.
- Cloud-hosted multi-tenant Volt, or a marketplace listing as the first deliverable (see Packaging).
- A second span model. Markers become spans; there is no parallel marker store.

## Anti-goals

Failure even if it works: a panel token that can do anything an operator token can; a panel that can fire a take, publish or delete by accident; a hi-res path handed to a client for an asset that is archived, still being written, or (for publish) not cleared; a marker round trip that duplicates spans on every retry or silently deletes a Volt span because a marker vanished in Premiere; a claim that the formats or the panel work in Premiere that no person has watched happen; a CORS or CSP loosening on Volt's own pages to make the panel convenient; a panel that blocks Premiere's UI thread on a slow network.

## Constraints

- Stack as today: Python standard library control plane, SQLite and Postgres both, schema head 21 (`migrations.LATEST`, last entry `Migration(21, "mos sender tables")`), a new migration numbered with no gap and a historical-shape forward test on both databases.
- Every new POST is classified in `ROUTE_ROLES` (`control/volt/serve.py`; `tests/test_users.py` `test_every_post_route_has_an_explicit_role` fails otherwise) and audited. `do_POST` audits any 2xx result with `ok: true` through `_audit_fields`, which keys the kind off `_AUDIT_KIND`; a new route family needs a new entry there.
- Existing ruling kept: marking, detecting and exporting to an NLE are allowed on any asset (not cleared, legal hold) because they leave the original source untouched; publish and air stay blocked (`docs/specs/shot-spans.md`, open question 2, and the `HOLD_NOTE` / `NOT_CLEARED_NOTE` strings in `nle.py`). `operator_state._blocked_by(cleared, legal_hold, archived)` is the one place the words "legal hold", "not cleared" and "archived" come from; the panel uses its output verbatim.
- The server sends no CORS headers today and there is no `do_OPTIONS` (read in `serve.py`: `Handler` defines `do_GET` and `do_POST` only). Only `_static` sends a `Content-Security-Policy`. That is by design and stays the default.
- Tests use fakes and never the network or a real Premiere. The panel's JavaScript is tested under Node's built-in test runner against a fake of the Adobe API and a fake `fetch`; the Python side is tested with the existing `Api` harness (`tests/test_operator_api.py`) on loopback.
- UI added to Volt's own pages (the sign-in approval page) follows the state language (icon, word, colour), the same-origin CSP (no inline script or style, no external URL) and adds colour tokens to both theme sets and the contrast test.

## The design

### Part 1: export verification

**What exists.** Three builders and a stateless route. The exports carry `comments` = `asset <id> span <id>` on each xmeml clipitem, so a clip in an editor can be mapped back to a span (that is the return path the panel will also use for sequences made from exports). `VOLT_EXPORT_PATH_MAP` rewrites a server path prefix to what the editor sees (`nle.parse_path_map`, `nle.map_path`). The manual checklist (ten steps) already exists in `shot-spans.md`; the outcome section is empty.

**What is missing, and what this spec adds.**

1. A fixture set that any machine can regenerate: short ffmpeg `testsrc` clips with burnt-in timecode and known cut points at 25, 29.97 drop-frame and 59.94, start timecode `01:00:00:00` or `01:00:00;00`, minted as cleared assets in a throwaway database, with a one-frame span, a span crossing a drop-frame minute boundary, and a span with a hostile label (XML and quote characters; `nle.clean` strips control characters). A generator writes the three export files for each rate and an `expected.json` that says, per clip, the name, the source in and out frame counts, the timecode labels, the order and the sequence start.
2. A comparer that checks a dump of an imported sequence against `expected.json` and reports per-clip pass or fail. The dump is produced by the person or by a script inside the editor (see "Automating the check").
3. Editors and versions. Premiere Pro: the current release and the previous one, on the owner's actual OS. DaVinci Resolve (free edition is enough for import): current release. Final Cut Pro is optional and only if a Mac exists; FCPXML's claim is "Final Cut Pro, DaVinci Resolve" so Resolve covers half of it. Avid: no format Volt writes is one Avid Media Composer imports natively as far as I know (EDL via its own import is the exception, unverified), so the Avid result is recorded as "not tested, EDL only if ever".
4. A results table in `shot-spans.md` replacing "Result: not yet recorded": rows are editor, version, OS, format, step, pass or fail, what the editor had to fix by hand. `FORMAT_VERIFIED` is one boolean for all formats today. It should become per format (a map the page and the test read), so xmeml can be marked verified without implying the EDL or FCPXML are. That is a small change in `operator.js` and `test_operator_ui.py` and is part of the recording task, made only after a pass is on record.
5. Defects found are fixed in `nle.py` with a golden-file test that captures what the editor actually accepted, each in its own commit.

**Automating the check.** Manual first: a person imports each file, fills the table, and photographs or exports the evidence. Then automated: a tiny development-only Premiere panel command ("Dump active sequence") writes the sequence's tracks, clip names, in and out in frames, and media paths to JSON; a Python script (`tools/editor_check/compare_dump.py`) compares it with `expected.json`. The comparer is hermetic and tested with fake dumps. The dump command and the import itself need a real Premiere and are therefore unvalidated until run; the script cannot import the file for the person (no input injection, no UI scripting of the owner's desktop). For Resolve, its Python scripting API can create a project and import a timeline; I could not verify its current call names offline, so the Resolve dump is a stretch goal, listed in Left to the builder. What a green run proves: the editor read the file and the numbers match. What it does not: that an editor sees footage online on a different machine, or that audio channels play the right channels (those stay in the human checklist).

### Part 2: the Premiere panel

#### What Adobe's published APIs allow, and what I could not verify

I wrote this section from memory and from general knowledge of the platform. **I could not read Adobe's documentation while writing it.** Everything in this list is a claim to check, not a fact. The builder reads the pages below before writing code and corrects this spec where it is wrong.

Believed true, to verify:

- Premiere Pro has a UXP plugin model (panel plugins, a `manifest.json`, JavaScript, HTML/CSS with Adobe's Spectrum UXP widgets), loaded during development through the UXP Developer Tool (UDT). It replaced the older CEP/ExtendScript panels as the supported path; Adobe's published schedule for retiring CEP is a point to confirm.
- Which Premiere release first supported UXP panels in production (not only a prerelease), and the minimum version in the manifest's `host` entry. I believe it was 25.x; verify the exact number, because it sets the minimum supported Premiere version, which the owner must accept.
- The Premiere object model exposes: the active project; project items and bins; importing files into a project; sequences; inserting or overwriting a project item on a track at a time; setting a project item's in and out points; clip markers on a project item and sequence markers (create, read, edit, delete; name, comment, start, duration, colour or type); time as a tick type rather than frames or seconds; and some form of changing a project item's media path or attaching a proxy. Method names are not given here because I am not certain of them; every one must be taken from the reference, and a spike (Task 3) confirms each against a real Premiere.
- A UXP plugin can call `fetch` and open WebSockets to hosts listed in the manifest's network permissions, can read and write files in its own data folder and, with a declared permission, in user-chosen folders, can open a URL in the system browser, and has some secure storage for secrets. Whether `fetch` is subject to browser-style CORS is exactly what I do not know; see CORS below.
- Whether `<video>` playback works inside a UXP panel and with which codecs. If it does not, "browse with proxies" means thumbnails, a scrub strip of thumbs, and "open proxy in the system browser", not an inline player.
- What happens to panels on a Premiere upgrade, and whether a panel can run commands from a menu (needed for the development dump command).

Believed false or unknown, to check (each one changes the design):

- That a panel can listen on a local TCP port. I assume it cannot, which is why sign-in uses a device code and not a loopback redirect (below).
- That `fetch` can stream a large body to disk. If not, the panel downloads a proxy in `Range` chunks and appends to a file.
- Whether project-item and marker times are relative to the start of the media file or to the file's start timecode. This decides the whole frame mapping and is the first thing the spike measures.
- Whether a project item can carry custom metadata the panel can read back. I do not rely on it: the panel names downloaded files `volt-<assetId>-<role>.<ext>` and reads the Volt asset id from the media path. Premiere project-item names can be edited by the user; paths are more stable.

**Documentation pages the builder must read before writing code** (all on Adobe's developer site; check the exact current URLs from the site's navigation, I cannot confirm them offline):

1. "Premiere Pro UXP" landing page and its getting-started guide (developer.adobe.com/premiere-pro/uxp).
2. The Premiere Pro UXP API reference (the object model pages for Application, Project, ProjectItem, Sequence, markers, TickTime, and the import and insert methods) and its changelog (what changed per Premiere version).
3. "UXP manifest" reference, the version Premiere requires: `host`, `entrypoints`, `requiredPermissions` (network, localFileSystem, launchProcess, clipboard), `featureFlags`.
4. "Network" and "fetch" pages in the UXP documentation: allowed domains, protocols (http versus https, localhost), WebSocket, any statement on cross-origin handling.
5. "File system" and "Persistent file tokens" pages (the UXP `storage` module) and the secure-storage page.
6. The UXP Developer Tool guide: loading, debugging, watching and packaging a plugin.
7. The Creative Cloud developer distribution pages: packaging a `.ccx`, the Adobe Developer Console and Distribute portal, private distribution links, the Exchange marketplace listing requirements, and enterprise deployment of plugins (Admin Console, "Manage packages", the Unified Plugin Installer Agent / command-line installer). I recall these names; check which still exist.
8. Adobe's UXP samples repository for Premiere, as the source of known-good call shapes.

#### Packaging, signing and distribution (what the owner needs)

I believe: development needs only the UDT and a Premiere install, with no developer account. A packaged `.ccx` can be installed by double-clicking it on a machine with Creative Cloud. Wider distribution goes through the Adobe Developer Console and Distribute pages: a free Adobe developer registration; private distribution (a link to specific people or an organisation) versus a public Adobe Exchange listing, which involves review and is slow and not needed for a first customer. Enterprise deployment uses Creative Cloud's admin tooling to push a package to managed machines. I do not know whether any of these steps costs money, requires a paid Creative Cloud or Adobe Exchange publisher account, or requires code signing the owner controls (a certificate), as opposed to Adobe signing the package at submission. **This is the first open question for the owner**, and Task 3 ends with the answer written down: whoever builds the package must register with Adobe, so the owner should expect to need an Adobe ID with developer access and, for the news customer's managed fleet, a person with Admin Console rights. The package is built reproducibly by a script (zip of a fixed file list, version from `manifest.json`) so the same source gives the same `.ccx`.

#### Architecture

Four layers, in a new top-level directory `panel/premiere/` (outside `control/`, because it is JavaScript shipped to editors and not part of the Python package):

1. `adapters/ppro.js`: the only file that requires Adobe's modules. It exposes a small, narrow interface in Volt's terms: `activeProject()`, `importFiles(paths, binName)`, `findItemsByPathPrefix(prefix)`, `setItemInOut(item, inFrames, outFrames)`, `insertOnSequence(item, atFrames, track)`, `overwriteOnSequence(...)`, `readItemMarkers(item)`, `writeItemMarker(item, marker)`, `readSequenceMarkers(seq)`, `writeSequenceMarker(seq, marker)`, `changeMediaPath(item, path)`, `sequenceClips(seq)`, `frameRateOf(item)`. Times are integers (frames) at the item's rational rate across this interface; ticks never leave the adapter.
2. `adapters/fake_ppro.js`: an in-memory project with items, bins, sequences, markers and a per-item rate, behaving as the adapter contract says. Every core test runs against it.
3. `core/`: pure logic and no Adobe, DOM or global `fetch`: `api.js` (a client over an injected `fetch`: timeouts, `Range` resume, 401 handling), `auth.js` (the device-code state machine), `library.js` (query, paging, cache), `media.js` (download to cache with verification and eviction), `markers.js` (Volt span to marker and back, `clientRef` identity), `outbox.js` (persisted queue of marker writes), `rights.js` (verdict words), `frames.js` (rational frame and seconds arithmetic with BigInt).
4. `ui/`: the panel's HTML and a thin view layer that renders core state. UI is verified by a human in Premiere; the view layer takes state and returns DOM operations so a Node test can still check which buttons are disabled for which verdicts.

Not a framework app. No bundler is assumed; if the host needs one the builder adds it in Task 8 and records why.

#### Sign-in hand-off (device code, no password in Premiere)

The panel talks to Volt's HTTP API and holds one secret: a **panel token**, `vpt_` prefix, distinct from the `vtk_` user token. Flow, modelled on the OAuth device authorization pattern and independent of what the identity spec chooses for the browser sign-in (OIDC first, per the owner):

1. Panel: `POST /api/panel/device-code` with `{label}` (the machine or Premiere name). No Authorization header. Reply `{deviceCode, userCode, verifyUrl, expiresIn, interval}`. The device code is 256 bits and shown nowhere; the user code is short (eight characters from an alphabet without look-alikes), single use, valid ten minutes.
2. Panel opens `verifyUrl` in the system browser (or shows it as text and a copy button if opening a URL is not allowed) and shows the user code.
3. The person signs in on the Volt page in the browser by whatever the deployment uses (today a bearer-token session in the operator page; later OIDC), opens the approval page `/panel/approve?code=...`, sees the label, the requested scope and the user code, and presses **Approve** or **Deny**. Approval is an authenticated `POST /api/panel/approve` (operator role): it binds the grant to the signed-in user's name and records the audit row.
4. Panel polls `POST /api/panel/token` with `{deviceCode}` at the server's `interval`. Replies: `authorization_pending`, `slow_down`, `denied`, `expired`, or `{token, expiresAt, scope, user}` exactly once. The token is stored hashed (sha256, as `users.hash_token` does for user tokens) and the plaintext is never retrievable again.
5. The panel keeps the token in the host's secure storage if it has one, else the plugin data folder with a plain warning in the UI. It never logs it.

Scope and life: scope `panel` today (one scope; the field exists so the identity spec can narrow it). Lifetime: an absolute thirty days (no sliding renewal), with `last_used_at` updated at most once a minute so an admin can see which panels are live. Revocation: the user signs out in the panel (`POST /api/panel/revoke`, revokes the presenting token), a user revokes their own tokens or an admin revokes any from the approval page's "Panels" list (`GET /api/panel/tokens`, admin). Disabling a user (`users.disable`) must stop their panel tokens at once, so panel token lookup joins `volt_user` and refuses a disabled user, the same rule `users.authenticate` applies.

Loopback open mode: when no token and no users exist (the open loopback mode in `_authenticate`), the panel needs no token and talks to `http://127.0.0.1`. That is a development mode and the panel says so in its status line.

#### Server changes

**Schema 22** (one migration; new tables are created on a fresh database by `schema.sql`/`store.py` and by the migration's statements on an older one, as migrations 16 and 21 do; `Migration` entries with `columns=` for the added column):

- `panel_grant(device_hash TEXT PRIMARY KEY, user_code TEXT UNIQUE NOT NULL, state TEXT NOT NULL, label TEXT, user_name TEXT, scope TEXT NOT NULL, created_at, expires_at, last_poll_at)`; state is `pending`, `approved`, `denied`, `used`.
- `panel_token(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, user_name TEXT NOT NULL, scope TEXT NOT NULL, label TEXT, created_at, expires_at, last_used_at, revoked_at)`.
- `span.client_ref TEXT` plus a unique index on `(asset_id, client_ref)` (null values are distinct on both databases), the idempotency key for marker writes.

**Authentication.** `_bearer_principal` already distinguishes the legacy `VOLT_TOKEN` and `vtk_` user tokens. A token starting `vpt_` is looked up in `panel_token` (not revoked, not expired, user enabled) and yields a `Principal` with role `operator` at most and a new `scope = "panel"`. The role of a panel principal is `min(user role, operator)`: an admin's panel token is still an operator's. **Default deny by scope**: a panel principal may call only an explicit allowlist (below); every other route, GET or POST, returns 403 `forbidden_scope`. A test iterates `ROUTES` and the GET routes and fails when a panel token reaches anything not on the list.

Panel allowlist, GET: `/api/whoami`, `/api/panel/library`, `/api/panel/assets/{id}`, `/api/panel/assets/{id}/hires`, `/api/panel/rights`, `/api/media-ticket`, `/api/assets/{id}/spans`, `/api/assets/{id}/transcript`, `/api/assets/{id}/proxy`, `/api/spans/{id}/thumb`. POST: `act_panel_markers`, `act_panel_revoke`. Not on it: `/api/state`, `/rundown`, `/assets` (the legacy list), `/seek`, `/api/audit`, `/metrics`, every show, item, cue, record, publish, delete and user route, and `act_export_nle` (the panel builds its timeline itself; exporting is the web page's job).

**Open routes.** `do_POST` runs `_gate()` first, which authenticates every POST. Two routes must run before a token exists: `act_panel_device_code` and `act_panel_token`. They are the only unauthenticated POSTs in the system. They are classified `"open"` in `ROUTE_ROLES` (the role set becomes `operator`, `admin`, `open`, and the existing test that asserts the value set is amended to allow `open` for exactly those two names), take a 1 KB body cap, ignore unknown keys with 422, and are rate limited in process: at most 30 device codes per client address per hour and 200 unexpired pending grants overall, with 429 `rate_limited` beyond that; a poll faster than `interval` gets `slow_down`. The audit row for an open route uses actor `panel` and target the grant's user code. Everything else about them is a plain `ROUTE_ROLES` entry, so the explicit-role test still bites.

**Audit.** `_AUDIT_KIND` gains `"panel": "panel"`. Audited actions: `device_code`, `token` (issued), `approve` (with the label), `revoke`, `markers` (target the asset, detail counts). Panel token writes are attributed to the user's name, with the label in `detail`, so the audit page can tell a panel write from a desk write.

**Library for the panel** (`volt/panel.py`, new; reads only):

`GET /api/panel/library?q=&show=&camera=&rights=&after=&limit=` returns assets, not file lists:
`{ok, items: [{assetId, show, camera, createdAt, rate, start, durationFrames, open, hasProxy, tier, cleared, legalHold, blockedBy, thumbSpanId, hits: [{spanId, kind, label, tcIn, tcOut, frameIn, frameOut, text}]}], next}`. `q` matches transcript words (the same substring rule as `library.search`), span labels and the show and camera names; an empty `q` lists by `created_at` descending. Deleted assets (`deleted_at` set) never appear. `blockedBy` is `operator_state._blocked_by(...)` output verbatim. `limit` default 25, max 100; `after` is an opaque cursor like `media._encode_cursor`. The list carries no media URLs: tickets are short lived, so a list cached for ten minutes would hold dead links.

`GET /api/panel/assets/{id}` returns the header the existing spans route gives (`media.header`) plus `spans` of kinds `clip`, `shot`, `marker`, the proxy facts `{available, sizeBytes, filename, container, sha256|null}` and the hi-res summary `{tier, open, blockedBy}`. 404 for an unknown or deleted asset (note: the existing `media._asset_row` does not filter `deleted_at`, so today a ticketed `GET /api/assets/{id}/proxy` still serves a tombstoned asset's proxy; the new routes filter, and whether to tighten the shared route is for the owner, see Open questions).

Media for the panel is fetched with the existing ticket route: `GET /api/media-ticket?asset=&kind=proxy&ttl=` returns `{url, expires}` and the panel then requests `url` (a `Range`-capable path, `tickets.py` HMAC of `asset|kind|exp`). A panel token is itself a valid bearer for `/api/assets/{id}/proxy`, so the panel may use the bearer instead of a ticket when its fetch can send headers; the ticket exists for any component that cannot (a `<video>` element if playback works). Tickets stay at TTL 120 seconds by default.

`GET /api/panel/assets/{id}/hires?purpose=edit|publish` is the relink manifest: `{ok, assetId, filename, path, mappedPath, sha256, sizeBytes, rate, start, durationFrames}` where `mappedPath` applies `VOLT_EXPORT_PATH_MAP` through `nle.parse_path_map` and `nle.map_path`, exactly as the exports do. Refusals, in this order: 404 `not_found`; 409 `archived` when `essence.tier` is `archived` or `restoring` (the same code and wording `nle.resolve` uses, with the rehydrate hint); 409 `essence_open` when the hi-res is still being written; 409 `not_local` when `location` is not a local path (`s3://`); and for `purpose=publish` only, 409 `not_cleared` or 409 `legal_hold`. `purpose=edit` follows the existing export ruling (allowed, flagged by `blockedBy` in the body). Default `purpose` is `publish`, so a client that forgets the parameter gets the stricter rule.

`GET /api/panel/rights?assets=a,b,c` (at most 200 ids) is the pre-publish check: per asset `{assetId, blockedBy, canPublish, tier}`. Read-only, so a GET.

**Markers** (kind `marker`, origin `panel`):

`shots.MARK_KINDS` is `("shot", "clip")` and `media.ALL_KINDS` is `("shot", "clip", "transcript")`. `marker` is added to `ALL_KINDS` but not to `DEFAULT_KINDS` (so the Library and the existing spans route do not change for anyone not asking for it), not to `PUBLISH_KINDS` in `operator_state` (a marker cannot air), and `ORIGINS` gains `panel`. A marker is a span with `tc_in` and `tc_out`; a point marker is stored as a one-frame span and flagged `text = "point"`. Markers do not take part in the shot overlap rule (`overlaps_shot`). The builder reads `shots.create`, `shots.trim`, `shots.delete` and the in-use checks before wiring this and records what had to change.

`POST /api/assets/{id}/markers`, operator role, audited (`act_panel_markers`, the only panel write besides revoke):
`{markers: [{clientRef, spanId?, tcIn, tcOut?, name, comment?, color?}], dryRun?}` with at most 200 markers and a 32 KB body. Per marker, the server returns `{clientRef, spanId, result}` where result is `created`, `updated` (a marker whose `spanId` matches a `marker` or `clip` span of this asset and whose times or label changed, only for `marker` spans; Volt `clip` and `shot` spans are never edited from the panel and answer `read_only`), `unchanged`, or an error with `code`. The write is idempotent on `(assetId, clientRef)`: a retry after a lost response returns the same `spanId`s and creates nothing. Rights do not block (the marking ruling). Frames outside the essence are `outside_source`, a bad label is 422.

Nothing in this API deletes a span. A marker removed in Premiere shows in the panel as "removed in Premiere, kept in Volt" with a one-click "Remove from Volt" that opens the Volt page, because `act_span_delete` is an admin route and stays one.

#### Panel UX

Four areas in one panel, keyboard reachable, status never by colour alone (icon, word, colour, as in the operator pages).

1. **Account strip.** Server URL, user name, connection state (`Connected`, `Offline, using saved results`, `Signed out`, `Token revoked`), Sign in / Sign out. First run: server URL (https required unless loopback), then the device-code steps with the user code large and a Copy button.
2. **Library.** A search box (debounced 300 ms), filters for show, camera and rights, a results list of asset cards (thumb from the thumb ticket, show and camera, duration, rate, rights chip) with their matching hits listed beneath (transcript snippet, label, timecode). A rights chip uses the same words as `/api/state`: `Ready`, `Not cleared`, `Legal hold`, `Archived`. Cards for archived assets say "Hi-res archived, proxy available" and still offer the proxy. Paging by "More". An offline banner replaces the list's refresh with the cached page, marked with its age.
3. **Detail and actions.** Selecting a card or hit shows the proxy preview (inline player if the spike says it works; else a thumb strip and "Open proxy in browser") and the span list (clips, shots, markers). Buttons: **Add proxy to project** (download, import into bin `Volt/<show>`), **Insert at playhead** and **Overwrite at playhead** for the whole proxy or for a selected span's in and out (the item gets in and out points set, then is placed; the clip placed is the proxy), **Write spans as markers** (to the project item and, where the item is on the active sequence, to the sequence), **Read markers from Premiere** (diff view, then push). A blocked rights chip does not disable these on proxies (edit is allowed); it labels them "Not cleared: edit only".
4. **Publish.** "Check sequence": lists the Volt-linked media in the active sequence (by path convention), calls `GET /api/panel/rights`, and shows per asset `Can publish` or the blocked word. **Relink to hi-res** is enabled only for clips that can publish, calls the hires manifest per clip with `purpose=publish`, checks the path exists and the size matches, then switches the project item (by whichever call the spike shows works) and reports each clip's result. A clip that fails stays on its proxy and is named in the report. Nothing is relinked silently.

Behaviour rules:

- Every network call has a timeout (10 s for API calls, 30 s idle for a download) and runs off the UI thread's critical path; the panel never blocks Premiere on a request. A request in flight shows a spinner and can be cancelled.
- A download writes `volt-<assetId>-proxy.<ext>.part`, resumes by `Range` after a break, verifies `Content-Length` and, when the server gives a `sha256`, the hash, then renames. A partial file never reaches `importFiles`.
- The cache has a size cap (default 10 GB, a setting), evicts least recently used files not referenced by an open project item, and tells the user what it deleted. It never deletes a file Premiere has open.
- Marker writes go through the outbox: each is persisted with its `clientRef` before sending, retried with backoff (1 s doubling to 5 minutes) while the failure is a network error or a 5xx, dropped to "needs attention" on a 4xx. A signed-out or revoked panel keeps its outbox and offers it again after sign-in. The outbox survives Premiere restarts.
- `clientRef` is written into the Premiere marker's comment text as `volt:ref:<id>` before the first send, and the Volt span id as `volt:span:<id>` once known. That is how a marker is recognised on the way back, and why a user who edits the marker name does not break the link. If the user deletes the comment text, the marker is treated as a new one.
- A 401 anywhere moves the panel to `Signed out` and stops background work; it does not clear the outbox, the cache or the server URL.
- Frame arithmetic is integer: frames at the item's rational rate, converted to Premiere's tick time only inside the adapter, with a test that converts 29.97 drop-frame boundaries both ways. The panel never parses drop-frame labels; it uses `frameIn` and `frameOut` from the API.
- Mixed frame rates between a project item and its sequence are Premiere's business; the panel reports the item's rate and the sequence's and warns on a mismatch rather than conforming.
- Offline: browse the last cached library page and the detail of assets seen this session; everything that needs the server is disabled with the reason on the control. Slow network: results stream in by page; a search superseded by a newer one is cancelled.

#### CSP and CORS

Volt's `Content-Security-Policy` is sent only on its own static pages (`_static`): `default-src 'none'; script-src 'self'; ... connect-src 'self'`. It describes what Volt's pages may load and does not apply to a UXP panel, which is not a Volt page. The new approval page is a static page under the same policy and is allowed to be same-origin only. **No change to the policy is needed or wanted.**

CORS is separate and matters only if the panel's network stack enforces it. The server sends no `Access-Control-*` headers and has no `OPTIONS` handler, so a browser would refuse a cross-origin `fetch` carrying an `Authorization` header (it needs a preflight). My belief is that UXP's networking is governed by the manifest's domain allowlist and does not do browser-style CORS, but I could not verify that. Spike Task 3 settles it by pointing a stub server at the panel and logging the request headers (especially `Origin`) and whether a preflight `OPTIONS` arrives.

- If no preflight and no enforcement: nothing to add. Default.
- If the host does preflight: add an **opt-in** `VOLT_PANEL_ORIGINS` (exact origins, comma separated, no wildcard, empty by default). With it set, only the `/api/panel/*` routes, the ticketed media GETs and `/api/media-ticket` answer `OPTIONS` and add `Access-Control-Allow-Origin` for a listed origin, `Allow-Headers: Authorization, Content-Type`, `Max-Age 600`, and never `Allow-Credentials` (the panel uses a bearer header, not cookies). Every other route stays without CORS. Tests assert the headers appear for a listed origin only and that `/api/state` never gets them.

The existing JSON content-type rule in `_read_json` (only `application/json`, so a cross-site form cannot POST) stays; the panel always sends JSON.

#### Security analysis

- **Token theft.** A panel token on a stolen laptop is the main exposure. It is operator at most, scoped by an allowlist that excludes every destructive and on-air route, thirty-day absolute life, revocable by the user or an admin, and dies when the user is disabled. The token appears only in the `Authorization` header; ticketed media URLs carry a separate HMAC ticket with a 120 second life (access logging is opt-in and passes through `logs.redact`, but the builder confirms the redaction covers `ticket=`).
- **Device-code phishing.** Someone sends a person a link and a code and asks them to approve. The approval page shows the label the panel supplied, the requesting address and the user code, says "Approve only if you started this on your own computer", and records who approved what in the audit. The label is attacker-controlled text and is rendered as text, never HTML.
- **Open endpoints.** Two unauthenticated POSTs are new attack surface: they allocate rows. Rate limits, caps on pending grants, a ten-minute expiry and a purge of dead grants (the runner's existing retention loop is the home; the builder checks) keep the table bounded. The device code has 256 bits; the user code is eight characters so guessing is bounded by the ten-minute life and per-address limits, and approval needs a signed-in session, so a guessed user code gives an attacker nothing without the approval.
- **Rights.** The server, not the panel, refuses hi-res for archived and open essences and refuses publish-purpose hi-res for not-cleared or held assets. The panel hides and disables as a courtesy. What the server cannot do is stop an editor with file access from reading a hi-res file off the share, or from exporting a finished sequence made of proxies; the rights gate in this design governs what Volt hands out and what Volt publishes, and the document does not claim more.
- **Path exposure.** The hires manifest gives a mapped file system path to an authenticated operator-level principal. The existing exports already return `paths`; the manifest adds no new class of disclosure, and the panel does not send paths back to the server (markers carry frames and ids only).
- **Injection.** Marker names and comments from Premiere are untrusted text: length-limited by the same `LABEL_LIMIT` (200) as span labels, control characters stripped as `nle.clean` does, stored and rendered as text. Volt labels written into Premiere markers are written as text.
- **Replay and duplicates.** Marker writes are idempotent on `clientRef`; the device-code token reply is once only; a polled `used` grant answers `expired`.
- **Downgrade and TLS.** The panel refuses a non-loopback `http://` server URL. The server cannot see TLS behind a proxy so it does not enforce; `docs/tls.md` already documents the Caddy and nginx recipes.
- **Scope creep.** The allowlist test is the control: adding a route to the panel is a deliberate edit to one tuple and a test, never a default.

## What the tests will and will not prove

Will prove (hermetic, in CI):

- The fixture generator and the comparer: expected numbers, mismatches reported per clip, a deliberately wrong dump failing.
- The device-code state machine: pending, slow_down, deny, expire, approve, the token issued once; hashed storage; disabled user kills the token; revocation; the two open routes' limits; the `ROUTE_ROLES` and audit rules for every new route.
- Scope enforcement: a panel token reaches exactly the allowlist and nothing else, found by iterating the real routing tables.
- Library, detail, rights and hires responses, including each refusal in order, the deleted-asset filter, and `purpose=publish` strictness.
- Marker idempotence (a repeated batch creates nothing), `read_only` on clip and shot spans, marker kind staying out of publish and the default Library listing, migration 22 on SQLite and Postgres from a historical shape.
- The panel core against a fake Premiere and a fake `fetch`: sign-in, offline banner, resumable download with a corrupted chunk, outbox persistence across a simulated restart, marker round trips, frame conversion at 25, 29.97 drop-frame and 59.94 including minute boundaries, rights verdicts disabling Relink.
- The CORS opt-in: headers only for listed origins and only on the listed routes.

Will not prove (the unvalidated list, carried into every doc this work touches):

- That any export opens in Premiere, Resolve or Final Cut, until the recorded manual run. Even then the claim is that version on that OS.
- That Adobe's API behaves as the fake does. The fake is written from the reference and from the spike's recorded observations; where the two differ, Premiere wins and the fake changes.
- That UXP `fetch` has the CORS, header, TLS, streaming and size behaviour assumed; that markers and project-item times mean what the adapter assumes; that proxy swap or media-path change works on the user's project.
- Playback, UI layout, focus and keyboard behaviour inside Premiere, and the panel's memory and speed with thousands of results.
- Package installation, the Admin Console deployment, and Adobe's review, none of which can be done in CI.
- Real-network behaviour: latency, drops mid-download, a captive portal, proxies.

## Order and size

The plan is [2026-11-01-editor-integration.md](../plans/2026-11-01-editor-integration.md). Nine tasks, each its own branch and PR, suite green on SQLite and Postgres before the next starts.

| Order | Task | Size | Needs a real Premiere |
|---|---|---|---|
| 1 | Verification kit: fixtures, expected numbers, comparer | small | no |
| 2 | Manual import runs, results recorded, per-format `FORMAT_VERIFIED` | small, a person's half day | yes |
| 3 | Premiere UXP spike and the Adobe answers | small, same session as 2 | yes |
| 4 | Panel tokens, device-code routes, scope gate | medium | no |
| 5 | Panel library, detail, rights and hi-res manifest | medium | no |
| 6 | Markers as spans | medium | no |
| 7 | Approval page | small | no (browser axe scan only) |
| 8 | Panel core and fake Premiere | large | no |
| 9 | Premiere adapter, UI, package, live checklist | large | yes |

Tasks 4 to 8 can proceed while 2 and 3 wait for a person, except that 8 and 9 take their Adobe method names and time semantics from 3. If 3 shows UXP cannot do something this design needs (a listener, a media-path change, markers), stop and bring the finding to the owner before 8.

## Left to the builder

Exact column types and index names; the HTTP error codes beyond those listed (use the `code` and `reason` pattern of `ApiError`); every Adobe method and manifest field name (taken from the reference at build time and recorded); the approval page's layout within the redesign's rules; the bin structure and naming inside Premiere; whether the Resolve dump is worth writing; how the marker colour maps to Premiere's marker types; the cache directory layout; whether a bundler is needed.

## Calls I made, for the owner to check

- Your answers, recorded as fixed: first customer is a daily news or live-show team, a production-house MAM team second; Premiere Pro first, Resolve and Avid later and out of scope; sign-in is OpenID Connect first with identity in a separate spec; the panel speaks only Volt's HTTP API; the rights gate is visible in the panel and enforced by the server; tests use fakes, never the network or a real Premiere; anything needing a real Premiere is unvalidated; every new POST is classified and audited.
- Export verification comes first and gates nothing technically, but its Premiere run comes in the same session as the Task 3 spike, because the same person at the same machine answers both.
- Device code, not a loopback redirect, because I assume a UXP panel cannot listen on a port. If the spike shows it can, a loopback flow is allowed to replace steps 2 to 4, with the same token.
- A separate `vpt_` panel token with an allowlist, not a user token with a flag, so a leaked panel token cannot reach the desk's routes and so a panel can be revoked without rotating the person's own token.
- A panel token is at most `operator`, even for an admin.
- Edit is allowed on any asset (the existing export ruling); a hi-res handed out for publish needs cleared and not held. I extended the owner's export ruling to say "edit from a proxy or a hi-res is fine, publishing from a hi-res is not", because relinking to hi-res on publish is the first point where the editor's output can reach air.
- Markers are a new span kind, not clips. A marker that never airs should not sit in `PUBLISH_KINDS` or in the Library's default listing.
- Nothing deletes a span from the panel. Removal stays an admin act on the web page.
- Point markers are one-frame spans.
- Thirty-day panel tokens, ten-minute user codes, thirty device codes an hour per address, ten gigabytes of cache, 10 and 30 second timeouts: starting values, easy to change.
- `FORMAT_VERIFIED` becomes per format when the first pass is recorded.
- Avid is "not tested, EDL only if ever".
- No package for Exchange in the first release; private distribution only.
- The panel code lives at `panel/premiere/`, outside `control/`.

## Open questions

1. **Adobe registration and cost.** Will the owner register an Adobe developer account (free as far as I know) and tell me whether the news customer's IT can push a `.ccx` through Creative Cloud Admin Console, or should the first customers install by double-click? Task 3 will confirm whether any fee or certificate is involved, but the owner decides who holds the Adobe identity that publishes.
2. **Minimum Premiere version.** UXP panels need a Premiere release at least as new as the spike finds. Is the first customer on or willing to move to it? If they are on an older Premiere, this plan does not serve them, and the CEP path is a different project.
3. **Who runs the manual import check, and on which machines?** One person with Premiere (current and previous release) and Resolve, for about half a day.
4. **Tombstoned assets.** The existing `GET /api/assets/{id}/proxy` serves a deleted asset's proxy to anyone holding a ticket or token (read from `media._asset_row`, which does not filter `deleted_at`). Should that route refuse deleted assets too (it would also change the operator pages), or only the new panel routes?
5. **Does the news customer's Premiere machine see the media share?** If not, hi-res relink has nowhere to point and the feature degrades to "download hi-res through the server", which this spec does not include.
