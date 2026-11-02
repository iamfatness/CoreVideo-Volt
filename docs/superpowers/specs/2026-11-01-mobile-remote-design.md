# Volt: mobile and remote access, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01. Gap 8 of [the competitive roadmap](2026-11-01-competitive-roadmap.md).

## Why

Volt's desk is a desktop browser page. It is tested at 1920, 1366 and 768 wide ([operator-ui.md](../../specs/operator-ui.md), [ui-slice-1-contrast.md](../plans/ui-slice-1-contrast.md)), it polls `GET /api/state` every 2 s (6 s when the tab is hidden, `POLL_MS` and `POLL_HIDDEN_MS` in `operator.js`), it keeps the bearer token in `sessionStorage`, and the same-origin CSP in `serve.py` (`_static`) allows no manifest and no service worker. A producer who is not at the desk cannot use it: the viewport meta is the bare minimum, the rail and header are built for a pointer, and nothing in the page is sized for a thumb.

Most rivals offer a phone or tablet client, or at least a client that works over the internet: a producer approving stories from the car park, a director watching the rundown from the floor, an editor in another city checking whether a clip is cleared. That is the gap. **Caveat: the competitor claims are from general knowledge, not an audit of any product** (the roadmap says the same). Before building, re-check what ENPS, iNEWS, Dalet, Octopus and Ross actually offer on phones. The design below does not copy any of them; it follows what a producer on a phone needs and what the code already guarantees.

What the code gives us, read on 2026-11-01:

- The desk is static files; `GET /api/state` already returns everything a screen needs (`operator_state.build`: `shows`, `show`, `rundown`, `taking`, `cues`, `media`, `assets`, `spans`, `room`, `mos`).
- Every write is a JSON `POST` classified in `ROUTE_ROLES` and audited. `act_hold`, `act_release`, `act_skip`, `act_take`, `act_kill` are all `operator`.
- There is no `ETag`, no `Cache-Control` and no `304` on `/api/state` (`_json` sets only content type and length). Every poll rebuilds the payload, and **calls `room.observe()` on the room each time** (`operator_state.room_state` through `_operator_state`). Ten phones polling means ten observations per 2 s against the core.
- `/api/state` carries `assets[].file` (a server path) and `room.artifactPath`. That is fine on a LAN desk and wrong for an outside viewer.
- No rate limit exists anywhere in `serve.py`. Failed auth is logged with `client=` which behind a proxy is always `127.0.0.1` ([tls.md](../../tls.md)).
- Roles are `operator` and `admin` only (`users.ROLES`). There is no read-only role.
- The thumbnail and proxy routes do not check rights (`serve.py`, `THUMB_ROUTE` and `READ_ROUTES` branches); rights gate air and publish, not browsing.

## Goals

1. **A phone layout of the desk** for a producer: read the rundown, see what is on air and what is next, hold and release, skip, approve a story when the newsroom spec lands, see room and device state, see failures.
2. **A tablet layout** that is the Producer workspace made touch-safe: two columns, build forms available, same safety rules.
3. **Touch safety equal to the desk's.** Two-press arming, no accidental TAKE, and on a touch device no TAKE or Kill at all.
4. **Installable**: manifest, service worker, offline shell, with a stated list of what is never cached.
5. **Remote access that is safe to put on the internet** behind the TLS recipe: rate limits, a trusted-proxy rule for the client address, a read-only `viewer` role, a media rule that respects the rights gate, and a list of what changes when Volt is hosted.
6. **Light on a bad connection**: conditional state requests, a lite state view, poll back-off, and a freshness label so a slow link is never mistaken for a quiet show.
7. **Alerts**: in-page alerts now; a stated, honest path to push.
8. **Accessible on a phone**: zoom, orientation, reduced motion, target size, and a repeatable browser check at phone sizes with axe-core.

## Non-goals

- A native iOS or Android app. The owner has kept the desk as static files with no framework and no build step; a PWA is the mobile client.
- Running the show from a phone. TAKE, go, kill, record start and stop, clearing rights, legal hold, publish, delete and user management stay on the desk.
- Library work on a phone (marking, trimming, detection, export). Precision work needs a pointer and a larger screen.
- Offline editing, an offline queue of writes, or any local copy of the rundown.
- A live program picture from CoreVideo Pro. The verified room API (`CoreVideoProRoom.observe`, `observe_program`, `senders_observed`) reports state, not pixels; see "Remote view of the program".
- Web Push in this slice (see "Alerts"). It needs a third-party push service and a cryptography dependency, and nothing can validate it here.
- Multi-server or hosted-tenancy work (the scale spec, gap 4), and the OIDC flow itself (the identity spec, gap 1).

## Anti-goals

Failure even if it works:

- A mobile screen where a stray tap, a double-tap, a scroll that ends on a button, or a re-render under a finger takes, kills, records, clears rights or publishes.
- A cached or stale rundown that looks live. On a phone, "last known" must never be presented as "now".
- A token, a state payload or a media URL in the service-worker cache, a URL, a log line or `localStorage`.
- A `viewer` who can reach a path, a number or a picture that an uncleared or legally held asset forbids.
- A rate limit that lets one attacker lock out the real producer.
- A phone layout that changes a desk key, the desk's arming, or the 2 s / 6 s desk poll.
- A claim that push, iOS install or a real phone's ergonomics work, when no one has tried.

## Constraints

Owner decisions already made, recorded here as the owner's:

- **First customer** is a daily news or live-show team; the production-house MAM track is second. So the phone is a producer's tool first.
- **Sign-in is OpenID Connect first** (its own spec). This design assumes per-user identity and server sessions will exist and refers to them; it does not build them.
- **The operator UI stays static files under a same-origin CSP** with no framework and no build step.
- **Brand and state language** per [brand-guide.md](../../brand-guide.md): icon, word and colour; dark and light themes with measured contrast. Red is for a live path only.
- **Desk keys and two-press arming must not change.**
- **Every new POST is classified in `ROUTE_ROLES` and audited. Tests use fakes and never the network.** Anything that needs a real phone or a push provider is written as unvalidated.

From the code and the roadmap:

- The CSP today is `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. A manifest needs `manifest-src` and a worker needs `worker-src`; with `default-src 'none'` both are blocked until named. No `unsafe-*` is added.
- `Handler` is HTTP/1.0 (`BaseHTTPRequestHandler`), one connection per request; keep-alive and HTTP/2 to the phone come from the TLS proxy.
- Volt answers loopback only with no token (`bad_host`); a proxy needs `VOLT_TOKEN` or a user row ([tls.md](../../tls.md)).
- The static test `test_references_only_same_origin_assets` requires every `src` and `href` in `index.html` to start with `/operator/`, `#` or `data:`, forbids `https?://` in the page files, and `test_token_lives_in_session_storage_only` allows `localStorage` only for `volt.workspace` and `volt.theme`.
- Rules for every gap, from the roadmap: off by default where it exposes; the rights gate is never bypassed; honest about proof; migrations forward on both databases (this design needs none); every new screen axe-clean at desktop and phone sizes.

## The design

### Form factor: how the page decides

Three layouts, chosen in the page by `matchMedia`, never by user-agent:

| Layout | Rule | What it is |
|---|---|---|
| Desk | width above 1000 px, or a fine pointer above 820 px | Today's page, unchanged |
| Tablet | 600 to 1000 px and a coarse pointer | Producer workspace, two columns, 44 px targets |
| Phone | under 600 px and a coarse pointer | Cards, bottom tab bar, 48 px targets |

A touch device (`(pointer: coarse)`) is in **touch mode** whatever its width. Touch mode never renders TAKE, GO, Kill, Record start or stop, Clear, Legal hold, Publish or Delete. They are not hidden by CSS; the builder functions do not emit them (the desk already hides the build forms by CSS in the Operator workspace, which is weaker and is left alone). A touch-screen control room can opt in per tab with a header toggle, **Desk controls on this device**, held in `sessionStorage` (gone with the tab) and shown in an armed state; it re-enables the desk's own controls with the desk's own guards. This is a UX choice, not security; the server side is below. A narrow desktop window (a fine pointer at 500 px) gets the phone layout's geometry but keeps the desk's controls and keys, because a keyboard user can already satisfy the guards.

The viewport meta gains `viewport-fit=cover`. It never gains `maximum-scale` or `user-scalable=no`; pinch zoom stays.

### Phone screens

**Header** (one row, 56 px): mark, show picker, a connection and freshness pill (`Live`, `Updated 3 s ago`, `Stale 24 s`, `No connection`: icon, word, colour), and a **More** button that opens a dialog with workspace, Theme, Token or Sign out, and Install. Clock moves into the More dialog. The Keys button is not shown on touch (there is no keyboard to document).

**Bottom tab bar** replaces the rail: **Rundown**, **Devices**, **Rights**, **More**. 56 px tall plus `env(safe-area-inset-bottom)`, icon over word, `aria-current="page"` on the active tab. Publish status appears under the on-air card on Rundown; Library and Record are reachable by hash and show a plain notice, "Use a larger screen for this", with the read-only facts that fit (the recording lifecycle on Devices).

**Rundown.** Top to bottom:

1. **On-air strip** (sticky). The on-air or taking item's name with its state chip, a `Held` chip when held, the drift (`+00:06 behind`, icon and word, as on the desk), and a still of the item's media (see "Remote view of the program"). If nothing is on air it says `Nothing on air`.
2. **Next** line: the next ready item and its cue count. A cue with a failure shows the red failed chip and the server's detail.
3. **Cards**, not table rows, one per item: number, state chip, name, length, front time, back time when a slot is set, schedule chip. Killed and skipped items collapse into a count. Tapping a card selects it (amber bar down the left edge, as on the desk) and opens its **action bar** under it.
4. **Show action bar** (fixed above the tab bar when a show is open): **Hold** or **Release**. Hold is the one routine control a floor producer needs and it stops the rundown, so on touch it is two-press (below).

**Item action bar** (selected ready item): **Skip** (two-press), **Approve** (present only when the newsroom spec's capability is in the state; absent today, so the slot is empty and unrendered), **Details** (cues and media). No Kill, no Remove, no Rename, no Attach. Build forms (`#build`, `#cue-add`) are not rendered on phone.

**Devices.** The room panel (configured, reachable, lifecycle chip, `Fresh` or `Stale`), each MOS device with its connection chip and last refusal, deck cues' failures, the recording lifecycle read-only, and the server version. No controls. This is "see device state".

**Rights.** The table as cards: cleared, legal hold, can air, reason. Read-only: Clear and Lift hold are not rendered on touch. (Clearing is a legal decision, and a held asset's lift keeps its two-press and stays on the desk.)

**More dialog** reuses the existing Token dialog for today's bearer token and gains Sign out once sessions exist.

### Tablet screens

The Producer workspace with two columns (rundown left, cues and room right) in both orientations at 600 to 1000 px; the rail returns as a left rail of 64 px (today's), targets rise to 44 px, hover-only affordances are replaced by visible ones, and the build forms are shown (typing on a tablet is realistic). TAKE, Kill and Record are absent in touch mode as on the phone. Arming is the touch arming below.

### Touch targets, gestures and arming

- Minimum target 48 px on phone and 44 px on tablet, with at least 8 px between neighbouring actions; the existing `.btn.small` is raised in touch mode. WCAG 2.2 asks for 24 px at AA (2.5.8) and 44 px as the enhanced level; the design uses the larger numbers because a thumb on a moving train is not a mouse.
- `touch-action: manipulation` on every control removes the double-tap-to-zoom delay and the double-tap that would otherwise arm and confirm in one gesture.
- **Two-press arming on touch** (new `armStep` in `mobile.js`): first tap arms (the button becomes `Confirm skip`, amber outline, icon change, the live region says so); the second tap confirms only if it lands **at least 400 ms after arming** (so a double-tap cannot confirm) and **within 4 s** (`ARM_MS`, unchanged); a tap anywhere else, a scroll of more than 10 px, a poll that changes the selected item, or Escape disarms. The armed button keeps its position: the layout must not move when it arms (no re-flow under the finger). The desk's `armedAction` keeps its present behaviour exactly; touch mode passes through `armStep` first.
- No gesture is required and none acts. No swipe to skip, no long-press to kill, no pull-down to take. Scroll is scroll. Pull-to-refresh is the browser's; a visible **Refresh** button does the same.
- A refusal from the server (`held`, `stale_order`, `not_skippable`) is shown as on the desk, with its stable code and words. Nothing is optimistic; the page shows the state the server returns.
- Haptics: none for actions. A short vibration only for a new alert, never for a tap, so it cannot be mistaken for a confirm.

### Installable PWA

New static files, all served by the existing `STATIC` table (open without a token like the page, so the install flow can fetch them without credentials):

| Path | Content |
|---|---|
| `/operator/manifest.webmanifest` | `name` "Volt", `short_name` "Volt", `start_url` `/operator`, `scope` `/`, `display` `standalone`, `background_color` and `theme_color` `#141414` (ground), icons 192 and 512 |
| `/operator/icon-192.png`, `/operator/icon-512.png`, `/operator/apple-touch-icon.png` | The four-slash mark on ground, from `brand/logos/mark-slashes.svg`, rasterised once and committed |
| `/operator/mobile.js` | Pure helpers (form factor, poll plan, arming, alert diff), loaded before `operator.js` |
| `/sw.js` | The service worker, at the root so its scope can be `/` (the page lives at `/operator`, which is outside a `/operator/` scope; a worker file under `/operator/` would need a `Service-Worker-Allowed` header) |

CSP gains exactly `manifest-src 'self'; worker-src 'self'`. `index.html` gains `<link rel="manifest" href="/operator/manifest.webmanifest">`, the apple-touch-icon link and a `theme-color` meta pair for dark and light. The existing same-origin test keeps passing because every new `href` starts with `/operator/`; registration is in `operator.js` (`navigator.serviceWorker.register('/sw.js')`) so the HTML adds no script.

**What the worker does.** It precaches the shell: `/operator`, `/operator/operator.css`, `/operator/operator.js`, `/operator/theme.js`, `/operator/mobile.js`, the manifest and the icons. For those paths it is **network first**, falling back to the cache only when the network fails; so an online page is always the server's current page, and a cached page appears only when there is no connection. The cache name carries the server version, and activation deletes older caches. It handles `GET` for that fixed list only.

**What is never cached, and never touched by the worker:** anything under `/api/`, `/rundown`, `/assets`, `/seek`, `/media`, `/metrics`, `/health`, any request that carries an `Authorization` header, any `POST`, any media or thumbnail, and `/auth/*` once sign-in exists. The worker has no `fetch` handler branch for them at all: they go to the network untouched. The token, the state and the media therefore never enter Cache Storage. The shell contains no secret (it is served without a token today).

**Offline.** The cached shell opens and shows `No connection` with no rundown. It does not show the last rundown. A stale rundown on a phone is the failure that matters (a producer believes an item is next when it is not), so state is held in page memory only, never persisted, and on a lost connection the page keeps showing it for 30 s marked `Stale 24 s` in the warning style, then clears it and says `No connection`. This is stricter than the desk, which keeps its last data (`setConn('lost')` then `render()`); the desk is not changed.

**Install requirements.** Browsers install a PWA and register a worker only on a secure context: HTTPS, or `localhost`. A phone reaching Volt by `http://192.168.x.x` can use the page but cannot install it; this is already recorded in operator-ui.md ("needs a secure context, and a LAN address over http is not one"). The path is the Caddy recipe in tls.md, with `tls internal` and the Caddy root certificate installed on the phone for a studio with no public name. **iOS Safari's add-to-home-screen behaviour (its own meta tags, no install prompt, separate storage for the installed app) is unvalidated.**

### API changes

Three small, read-side changes and one policy seam. No schema change, no new table. No new POST in this design, so no new `ROUTE_ROLES` entry; the tests still assert the full set is classified.

**1. Conditional state.** `GET /api/state` computes the body once, derives `ETag: "<first 32 hex of sha256(body)>"`, and:

- if `If-None-Match` lists that tag (strong or weak form, comma list, or `*`), answers `304` with the `ETag` and no body;
- otherwise answers `200` with the `ETag`.

Both carry `Cache-Control: no-store` and `Vary: Authorization`. `no-store` is deliberate: the browser keeps no copy, so state never lands in a disk cache or the worker's cache, and the page sends `If-None-Match` itself from the last tag it holds in memory (a manually set conditional header is passed to the network by `fetch` and a `304` is delivered to the page as a `304`). The gate runs before the condition: an unauthenticated request never learns whether a tag matches. This saves bytes and parsing, not server work; the state is still built. For that:

**2. Room observation memo.** `room.observe()` is a network call to the core. `serve.py` keeps one memo of the last `room_state` for `VOLT_ROOM_MEMO_MS` (default 1000; 0 turns it off), cleared by any successful POST, so a producer's phone and the desk together cost one observation a second rather than one per poll per client. The page still sees any action's effect at once (the memo is cleared and the action's own refresh follows), and anything a worker process changes shows within a second, under the desk's 2 s poll. A test of the ETag also needs this: two quiet polls must give the same tag, which means no field in the payload may change on its own.

**3. Lite view.** `GET /api/state?show=<id>&view=lite` omits `assets` and `spans` (the two large arrays, and the ones that hold `file` paths and span text). It keeps `shows`, `show`, `rundown`, `taking`, `cues`, `media` (with `blockedBy` per item, so a producer sees why an item cannot air), `room`, `mos` and `orderVersion`. Any other `view` value is `422 bad_input`; omitting it is today's payload, byte for byte. The phone and tablet layouts ask for lite; the Rights cards on a phone need `assets`, so that tab asks for the full view once on opening and on the slow poll. Sizes are not asserted here: the builder measures lite against full on the `library` scenario and records the numbers in operator-ui.md. **Delta payloads** (send only what changed since a tag) are not built: they need the server to remember each client's last state, which breaks the stateless handler and the planned multi-server setup, and conditional requests plus the lite view probably remove most of the cost. Revisit only if the recorded numbers say a phone on a poor link still struggles.

**4. Remote-safe responses for limited principals** (the `viewer` role, and a session scope `mobile` if the owner chooses; see "Security"). For them, `room.artifactPath` and `mos[].detail` are blanked, `view=lite` is forced, and the media rule below applies.

### Behaviour rules

- **Poll cadence.** Desk: 2 s visible, 6 s hidden, unchanged. Phone and tablet: 2 s visible; after 5 consecutive `304`s, widen to 3 s then 4 s (cap 4 s); any `200` with a changed tag, any tap, any visibility change resets to 2 s. Hidden: 6 s for the first 30 s, then 30 s. A page that comes back to the foreground refreshes at once (the existing `visibilitychange` handler). Mobile browsers throttle or freeze hidden tabs, so no timer is relied on for alerts.
- **Errors.** On a failed poll, back off 2, 4, 8, 16, 30 s with up to 25 percent jitter; honour `Retry-After` on a `429` or `503`; a `401` opens the token dialog and stops polling, as today. `navigator.connection.saveData` true selects lite and a 5 s base on any layout.
- **Freshness.** The header pill always shows the age of the last good state on touch layouts. Colour is never the only signal.
- **Stale means stale.** Beyond 30 s without a good poll the on-air strip, the cards' action bars and Hold are disabled (`aria-disabled`, with the reason in words), because an action taken against a screen older than half a minute is an action taken blind. The server still refuses a hold or skip it should not allow; this is the second line, not the first.
- **After an action** the page refreshes at once and discards any poll that began earlier (the desk's rule, kept).
- **Hold reason.** A phone sends `Held from a phone` (the desk sends `Held from the operator screen`), so the hold reason the server stores says where the hold came from.

### Alerts

What a producer wants from a phone in a pocket is to be told: a cue failed, the show was held, the room became unreachable, a recording failed. Two things differ in how feasible they are.

**In-page (built here).** `diffAlerts(prev, next)` in `mobile.js` compares two states and returns alerts: `cue_failed` (a cue gained a `failure`), `held` and `released`, `room_unreachable` and `room_back`, `recording_failed`, `item_on_air`. While the page is open the header shows a count, the live region announces (`#live`, `aria-live=polite`), and, only after the user presses **Turn on alerts** (never on load), `Notification.requestPermission()` is asked and, when granted and the page is in the background but alive, a notification is shown. A short vibration accompanies a new alert unless `prefers-reduced-motion` is set. This works only while the page is alive: phone browsers freeze background tabs within seconds to minutes, so it is not a pager. It says so on the Turn on alerts control.

**Push (designed, not built).** A message that arrives when the page is closed needs Web Push: a VAPID key pair, a stored subscription per user and device, and a send to a push service run by the browser vendor (Google, Mozilla, Apple). Facts that bound it, from general knowledge and unvalidated here:

- It needs a third-party provider; the message passes through the vendor's service, so its text must carry no rundown content ("A cue failed on Show 7").
- The encryption (RFC 8291, ECDH P-256, AES-GCM, ES256-signed JWT) is not in the Python standard library; it needs the `cryptography` package, as an optional extra like the existing database extras, with the stdlib-only build unchanged.
- On iOS it works only for a site added to the home screen (a recent iOS, unvalidated).
- It needs a table of subscriptions (a migration, both databases) and a sender in the runner or clock process that watches for the events above once, even with two servers.
- A simpler alternative that needs no browser vendor is an outgoing webhook to a chat service the newsroom already runs (Slack, Teams, ntfy). That also leaves the building, but under the customer's own account.

Nothing about push is built until the owner picks a channel (open question 2). The `diffAlerts` function is written so the same event list can feed a server-side sender later.

### Remote view of the program

The phone shows **what is on air**, not the picture. The rundown strip is the program tally: the item, its state, its cues and the room's lifecycle. The verified room surface (`rooms.py`) reports `programFrame` sources and sender state, never frames, so a live picture would need CoreVideo Pro to offer a low-rate preview output that Volt can proxy; that is a request to the CoreVideo Pro side, not something this design can assume (open question 3).

What exists today and is used: a **still** of the on-air item's media, the existing span thumbnail route (`/api/spans/{id}/thumb`), fetched by the page with the `Authorization` header into a blob URL, as the desk already does (three at a time, cached). A still is a frame of the clip, not of the program output.

**The rights gate on remote paths.** The thumbnail and proxy routes today do not check clearance. On the desk that is intended (an editor browses uncleared media). For a remote viewer it is not: a pixel of a legally held clip leaving the building is the harm the gate exists to stop. So, for the `viewer` role and for a `mobile`-scoped session:

- `GET /api/media-ticket` and the thumbnail route refuse an asset whose `blockedBy` (from `operator_state._blocked_by`) is `legal hold` or `not cleared` with `403 rights_blocked`; the check is done at ticket issuance, because a ticket carries no identity at use. Tickets keep their 120 s default life.
- The proxy (full video) is refused for these principals altogether (`403 forbidden_role`); a phone does not stream a hi-res-derived proxy, and the bandwidth is the user's.
- The page shows `Blocked: not cleared` in the still's place, the same chip and words as elsewhere.

### Security analysis for remote access

**Transport.** TLS is a requirement, not a recommendation, for any non-loopback use: the token is a bearer token and a PWA will not install without it. Volt keeps serving plain HTTP on loopback behind the Caddy or nginx recipe in tls.md. This design adds to that page: HSTS at `max-age=31536000` once verified, `includeSubDomains` only if the owner controls the whole domain, and a check that the proxy forwards `If-None-Match` and returns `304` and `ETag` untouched (Caddy and nginx do by default; leave response compression off for `/api/state`, since a proxy that recompresses may weaken or drop the tag).

**Token handling.** Today the token is typed into a dialog and kept in `sessionStorage` (`volt.token`), sent as `Authorization: Bearer`, gone when the tab closes. On a phone that is a poor fit: typing a 43-character token on a phone keyboard, and the browser killing a background tab (which loses the token). The design does not move the token into `localStorage` (the existing test forbids it, correctly: a stored bearer token survives a lost phone and is readable by any script on the origin). After OIDC lands, the phone signs in through the identity provider and holds a **server session cookie**: `__Host-volt_session`, `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain`, with an idle timeout and a lifetime set by the identity spec. A cookie is unreadable to page script, so an injected script cannot steal it, which a `sessionStorage` token cannot claim. What a cookie adds is cross-site request forgery. Today's defence (only `application/json` bodies, so a browser will not send a forged form) holds, but cookie sessions must add an `Origin` check on every non-GET (equal to the configured public origin) and keep `SameSite=Strict`. **That check, and the cookie, belong to the identity spec and must exist before a cookie session is accepted on a remote origin.** Until then a phone uses the bearer token exactly as the desk does, from a password manager, over TLS. Machine tokens keep working unchanged.

**Rate limits** (new `ratelimit.py`, in memory, per process; the scale spec decides what replaces it across servers):

| Limit | Default | Key | Answer |
|---|---|---|---|
| Failed authentication | 30 per minute | client address (below) | `429 rate_limited` with `Retry-After` |
| Writes (every POST) | 120 per minute | the principal's name | `429` |
| State polls | 120 per minute | the principal's name | `429` |

`0` turns a limit off; the env names are `VOLT_RATE_AUTHFAIL`, `VOLT_RATE_WRITE`, `VOLT_RATE_STATE`. Tokens are 256 bits (`secrets.token_urlsafe(32)`), so guessing is infeasible; the failed-auth limit protects the log and the CPU, and it **counts only requests that presented an invalid credential**. A request with a valid token is never charged to it, so an attacker cannot lock the real producer out by failing from the same address. The write and state limits are per principal, so one runaway tab cannot starve another user. 120 writes a minute is far above a human's rate and below a loop's.

**Client address.** Behind the proxy `client_address` is always `127.0.0.1`, so a per-address limit would be one global bucket. Volt gains `VOLT_TRUSTED_PROXIES` (default empty). When the TCP peer is in that list, the client address is the right-most `X-Forwarded-For` entry that is not itself trusted; otherwise the header is ignored, exactly as tls.md demands ("do not add trust in these headers unless Volt is changed to verify the peer is the proxy"). This change verifies the peer, so that page's rule is updated, not broken. The address then appears in the `client=` log field.

**Read-only `viewer` role.** `users.ROLES` becomes `("viewer", "operator", "admin")`; `volt user add NAME --role viewer`. The `role` column is free text with no check constraint (`volt_user` in `schema.sql`), so no migration is needed. A viewer: every `POST` is `403 forbidden_role`; `GET` is limited to an allow-list (`/api/state`, `/rundown`, `/api/whoami`, `/api/health`, the thumbnail route under the rights rule above); everything else, including `/api/audit`, `/api/users`, `/metrics`, `/assets`, `/seek`, the MOS export and the proxy, is `403 forbidden_role`; `/api/state` is forced to the lite view with paths blanked. `ROUTE_ROLES` values stay `operator` and `admin` (a viewer is refused before that lookup), so `test_every_post_route_has_an_explicit_role` still holds. Use: a director, an executive, a client watching the rundown.

**Session scope for a phone (seam, default off).** The identity spec's sessions are the right place to record that a session was started on a phone. This design adds `Principal.scope` (`full` by default) and an allow-list `MOBILE_ALLOWED = {act_hold, act_release, act_skip}` (plus the newsroom approve action when it exists). A request from a principal with scope `mobile` that routes to anything else is `403 forbidden_scope`. Nothing sets the scope until sessions exist, so the seam is dormant; its tests set it by hand. With it, a lost or stolen phone cannot take, kill, record, clear rights or publish even while signed in, which the layout alone cannot promise. Whether to switch it on is open question 1.

**Cross-site and framing.** The CSP keeps `frame-ancestors 'none'` and `form-action 'none'`; no CORS headers are added; the page is same-origin to its API.

**Lost phone.** The OIDC identity spec owns session revocation. Today: `volt user disable NAME` takes effect on the next request (`authenticate` filters `disabled_at IS NULL`), and `rotate` replaces a token.

**What changes when Volt is hosted:** there is no loopback fallback, so `VOLT_TOKEN` or a user row is mandatory (already enforced by `check_bind`); the proxy is mandatory and `VOLT_TRUSTED_PROXIES` is set; the public origin is configured once for the Origin check; `/health` (unauthenticated: status and version) should be limited by the proxy to the platform's checker; the in-memory limits are per process and the room memo is per process, so a second server needs the scale spec's shared store; media tickets need `VOLT_MEDIA_KEY` set explicitly rather than falling back to the token; per-tenant isolation and the tenancy decision are the scale spec's.

### Accessibility on a phone

- **Zoom and reflow.** Pinch zoom is never disabled. The layouts reflow at 320 CSS px with no horizontal page scroll and no two-dimensional scrolling for the rundown (WCAG 1.4.10; 320 px is a 1280 px window at 400 percent). Text resizes to 200 percent without loss. Fixed bars are measured with the on-screen keyboard open: a fixed bar must not cover the field being typed in.
- **Orientation.** No lock (WCAG 1.3.4). Landscape phone (height under 500 px) replaces the bottom bar with the 64 px left rail so the cards keep height.
- **Reduced motion.** The existing `prefers-reduced-motion` block stays; the arming outline, the freshness pill and the alert count use no animation, and vibration is skipped when the preference is set.
- **Target size and spacing.** As above, enforced by a browser check.
- **Screen readers.** The tab bar is a `nav` with `aria-current`; cards are list items with the state chip's word in the text; the armed state is announced in `#live`; disabled-by-staleness controls use `aria-disabled` with the reason as the accessible description, so focus is not dropped (the desk's rule for Detect).
- **Contrast.** New colour states (stale, alert count, armed outline) use existing `*-ink` tokens in both themes; any new token is added to both sets and to the contrast test.
- **Safe areas.** Bottom bar and header respect `env(safe-area-inset-*)` in standalone mode.

### How layouts are tested in a real browser

The repo's axe-core runs have been manual: headless Chromium against `python -m tests.operator_harness <scenario> --port 8791`, axe-core 4.10.2 fulfilled from a local file through a routed same-origin URL (the CSP blocks other scripts), as recorded in operator-ui.md. This design makes that repeatable for phones without adding a JavaScript toolchain to CI:

- A script, `control/tests/browser/phone_check.mjs`, run by hand (and by a skipped-by-default unittest wrapper that runs only with `VOLT_BROWSER_CHECKS=1`), using Playwright and axe-core installed in a scratch directory, never in the repo.
- Viewports with touch emulation (`hasTouch`, `isMobile`, a coarse pointer): 320 x 568, 360 x 740, 390 x 844, 844 x 390 (landscape), 768 x 1024, 1024 x 768, plus the three desk sizes as a no-regression run.
- Scenarios: `normal`, `held`, `take_fail`, `record_producing`, `library`, in dark and light, with and without `--token`.
- Assertions per screen: axe has zero violations with the `wcag2a`, `wcag2aa`, `wcag21aa`, `wcag22aa` and `best-practice` tags (and the script confirms the `target-size` rule ran, not merely passed); `document.documentElement.scrollWidth <= innerWidth`; every interactive element's box is at least 44 x 44 (48 on phone) except inline links in running text; no `#take` and no Kill button exists in the DOM in touch mode; the arming sequence (tap, wait 100 ms, tap) does not act and (tap, wait 500 ms, tap) does; the shell loads and the worker registers on `localhost` and the cached shell opens with the network blocked and shows `No connection`; the worker's cache holds no request whose path starts `/api/`.
- Screenshots of each screen at 390 x 844 in both themes are re-shot into `docs/mockups/` when a layout changes.

## What the tests will and will not prove

Will prove (unit, node and HTTP tests with fakes, no network):

- `ETag`, `304`, `If-None-Match` forms, the gate-before-condition rule, `view=lite` shape and the 422 for any other view, the room memo and its reset by a POST.
- The rate limiter's arithmetic with a fake clock, that a valid token is never charged to the failed-auth bucket, and the trusted-proxy address rule (header honoured only from a listed peer).
- The viewer role: every POST refused, the GET allow-list, the lite and blanked payload, the rights rule on tickets and thumbnails for legal hold and not cleared, and that desk behaviour for `operator` and `admin` is unchanged.
- The scope seam: with scope `mobile` set by hand, only the allowed actions pass.
- The pure helpers under node: form-factor choice, poll plan (widen, reset, errors, `Retry-After`), `armStep` (dwell, timeout, disarm), `diffAlerts`.
- Static contracts: the CSP gains exactly the two directives, the new files are served with the right types, every `href` rule still holds, `localStorage` use is unchanged, and the worker's cache list contains no `/api` path (the worker runs under node against a fake `caches` and `fetch`).
- In a real Chromium (the manual script): the layouts at phone sizes, no horizontal scroll, axe clean, target sizes, the arming dwell and the absence of TAKE and Kill, and that the worker registers on `localhost`.

Will not prove:

- Behaviour on a **real phone**: iOS Safari, Android Chrome or a manufacturer browser; add-to-home-screen; touch ergonomics, thumb reach, gloves, glare, a moving vehicle; the rate of accidental taps. Emulation reports a coarse pointer; it does not feel one.
- **Install over a real certificate**, including the Caddy internal CA on a phone, and iOS install behaviour.
- **Push in any form.** Nothing is sent; no provider is contacted.
- Real network conditions: radio wake-up cost, packet loss, captive portals, a proxy that rewrites `ETag`. The poll plan is tested as arithmetic.
- The **OIDC cookie flow** and its CSRF defence; those are the identity spec's, and this one only states what it needs.
- That the lite view is small enough on a poor link until its measured size is recorded.
- Competitor parity (the caveat above).

## Left to the builder

The exact card and bar CSS within the rules above; the PNG rasterisation of the mark (any tool, once, committed with a note of how); the wording of the stale, offline and blocked messages (keep them plain and put the reason in words); the details of `diffAlerts` beyond the event list; the proxy settings beyond the check in tls.md; the layout of the More dialog; whether `phone_check.mjs` uses Playwright's device descriptors or its own viewport list.

## Calls I made, for the owner to check

- A phone is a **producer's** tool: read, hold, release, skip, approve, see state. No TAKE, GO or Kill on any touch device; an opt-in per-tab toggle exists for touch control rooms.
- Hold is two-press on touch, though it is one press on the desk. A floor producer's accidental Hold stops the show.
- A phone sends a different hold reason string, so the audit says where it came from.
- **A `viewer` role is added** (`operator` and `admin` remain the only roles that can write). The roadmap's shared interface said "roles are operator and admin only"; this is a deliberate exception you asked for.
- A phone-session scope exists as a dormant seam, default off.
- The service worker lives at `/sw.js`, and the shell is network first with a cache fallback, so an online phone never runs a stale page.
- On a lost connection the phone shows stale data for 30 s, labelled, then nothing. The desk is unchanged.
- The token is never moved to `localStorage`. A cookie session waits for the identity spec.
- Rate limits are on by default (120 writes and 120 state polls a minute per principal, 30 failed authentications a minute per address). The values are guesses, not measurements.
- A 1-second room-observation memo is on by default (`VOLT_ROOM_MEMO_MS=1000`) to stop phones multiplying calls to the core.
- Delta payloads are not built; the lite view and conditional requests come first and are measured.
- The program is a tally plus stills, not a picture.
- Alerts are in-page now; push waits on your channel choice.
- The ETag work is cheap and independent of the rest; it can ship before anything else in this spec (the roadmap says the same).
- Phase 5 depends on gap 2: until the newsroom spec defines approve, the Approve slot is empty.

## Open questions

1. **Should a session started on a phone be limited on the server to Hold, Release, Skip and Approve (the scope seam), so a lost phone cannot take or kill?** Default here: yes, switched on when sessions exist. The cost: a producer who is also an operator, working from a tablet, cannot take from it.
2. **Which alert channel do you want for a closed app: browser Web Push, a webhook to a chat service you already run, or in-page only for now?** Web Push sends a short message through Google, Mozilla or Apple; a webhook sends it through your chat vendor. Default here: in-page only until you choose.
3. **Do you want a live program picture on phones?** It needs CoreVideo Pro to offer a low-rate preview that Volt can proxy, and a decision about who may see it. Default here: no; tally and stills only.
