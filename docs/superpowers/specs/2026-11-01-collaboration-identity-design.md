# Volt: collaboration and identity, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01. Sign-in against Google or Microsoft is **unvalidated**: no real identity provider has been used, and every claim about their tokens below is from published behaviour as recalled, not from a run.

## Why

Volt today knows who you are by one of three things: a per-user bearer token with a role of `operator` or `admin` (`volt_user`, `control/volt/users.py`), the legacy shared `VOLT_TOKEN` (an admin named `desk`), or nothing at all on loopback. Two people building a rundown overwrite each other: `reorder` is guarded by `orderVersion` (`building.order_version`, a hash of the item ids), but `rename` of an item's slug or script is last-writer-wins, and nothing shows who else is in the show. There is no sign-in with an account people already have, no group, no way to let one team into one show, no way to talk about an item inside Volt, and the audit log can be read only one `target_id` at a time (`GET /api/audit?target=`).

A daily news team, the owner's first customer, lives in exactly these gaps. They are used to signing in with the company account, seeing who has a story open, being stopped from editing over a colleague, leaving a note on a story, and being able to answer "who changed the 18:00 lead and when".

**Caveat on the comparison.** What the newsroom and MAM incumbents offer here is from general knowledge of that product class (single sign-on, groups and roles, story locks and presence, comments and mentions, notifications, audit views). It is not an audit of any named product, and nothing below claims parity with one. The test of this design is the daily news team's workflow, not a feature list.

## Goals

1. Sign in with Google or Microsoft through OpenID Connect, server-side, with no new runtime dependency.
2. Keep tokens for machines and scripts exactly as they work today.
3. Finer permissions: a read-only role, named groups carrying a role, and per-show access for a group or user.
4. Many people editing at once without silent loss: edit leases on items, content-versioned writes, and a visible record of who is where.
5. Comments with mentions on items and shows, and in-app notifications for them.
6. An audit view an admin can filter, page and export.
7. Every write audited, every new POST classified in `ROUTE_ROLES`, every new screen in the existing redesign language.

## Non-goals

- SAML, SCIM provisioning, back-channel or front-channel logout, and any identity provider other than Google and Microsoft. Added only when a customer asks (owner).
- Real-time co-editing of one script (operational transform, CRDT). A lease with a versioned write is the model.
- Email, push, chat or webhook delivery of notifications. In-app only.
- Multi-tenancy inside one Volt database. Hosted means one Volt per customer.
- Tamper-proofing the audit log against someone with database access (see Calls).
- A password login, and local MFA. Volt delegates both to the provider.

## Anti-goals

Failure even if it works: a lease or a permission that can stop a Take, Go, Hold or Skip on air (the on-air path never waits on a collaborator); a login that works for someone it should not (a wrong issuer, audience, nonce, algorithm or an unverified email); a session that outlives a disabled user by even one request; a cookie-authenticated write that a foreign page can trigger; an open redirect on the sign-in return path; a token or client secret in a URL, a log or the audit table; a new POST that is not in `ROUTE_ROLES`; a screen that shows who is where with colour alone.

## Constraints

- Stack: Python standard library, SQLite and Postgres through the existing `Store` (`?` placeholders, `ON CONFLICT` upserts as `building.create_show` already uses), `ThreadingHTTPServer` (`serve.py`), one store connection per request. Schema head today is 21; this work adds migrations at the end of `MIGRATIONS` and claims no number here. Every migration is tested forward on both databases from a historical shape.
- Operator UI: static files under the same-origin CSP set in `Handler._static` (`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; ... form-action 'none'; frame-ancestors 'none'`). No framework, no inline script or style, icons from the sprite in `index.html`, status as icon plus word plus colour, brand per `docs/brand-guide.md`, behaviour per `docs/specs/operator-ui.md`, both theme sets and the contrast test.
- Every POST is in `ROUTE_ROLES` (a test fails otherwise, `test_users.py`) and a 2xx writes an audit row through the generic path in `do_POST`. `_AUDIT_KIND` gets an entry for each new route namespace.
- POST bodies are JSON only: `_read_json` returns `415 unsupported_media_type` for any other `Content-Type`.
- Tests use fakes and never the network: a fake OIDC provider (in-process HTTP seam), a frozen clock. Anything needing a real provider is written as unvalidated.
- Delivered hosted and installed. Hosted sits behind TLS (`docs/tls.md`). An installed LAN box without TLS cannot use OIDC (Google accepts `http` redirect URIs only for localhost) and keeps tokens; the spec says so rather than hide it.

## The design

### Owner decisions, recorded

First customer is a daily news and live-show team; the MAM production-house track is second. Sign-in is OpenID Connect first, Google and Microsoft as providers; SAML only if a customer needs it. Tokens stay for machines and scripts. Volt is delivered hosted and installed. The UI stays static files under the same-origin CSP.

### Identity: three ways in, one `Principal`

`Principal` (`serve.py`) is `(name, role, legacy)`. It gains `via` (`legacy`, `token`, `session`), `groups` (tuple) and `session_id`. Resolution order in `Handler._authenticate`:

1. `Authorization: Bearer` (legacy `VOLT_TOKEN`, then `users.authenticate`). Unchanged. A bearer request never uses a cookie, even if one is present.
2. Else a session cookie.
3. Else, with no `VOLT_TOKEN` and no user rows, the open loopback mode.

Bearer requests have no ambient authority (the browser never attaches them), so they need no CSRF defence. Cookie requests do.

### Sessions

Table `session`: `id_hash` (sha256 of the cookie value, primary key), `user_name`, `provider` (`oidc:google`, `oidc:microsoft`, `token`), `csrf` (random, per session), `created_at`, `last_seen_at`, `expires_at`. The cookie value is 32 random bytes, base64url, stored only hashed (a database read does not yield a usable cookie, the same stance as `volt_user.token_hash`).

- Name `__Host-volt_session` when `VOLT_PUBLIC_URL` is `https` (forces `Secure`, `Path=/`, no `Domain`); `volt_session` on `http://localhost` development. Attributes: `HttpOnly`, `SameSite=Lax`, `Secure` on https. The `Secure` decision comes from `VOLT_PUBLIC_URL`, never from `X-Forwarded-Proto` or `Host`.
- Lifetime: idle timeout and absolute timeout, both server-side (`VOLT_SESSION_IDLE` default 8 h, `VOLT_SESSION_MAX` default 14 h, a shift). `last_seen_at` is written at most once per 60 s per session, so a 2-second poll does not write on every request.
- A session never stores a role. Role, groups and `disabled_at` are read from the tables on every request, as tokens do today, so disabling a user or removing a group member takes effect on the next request.
- A new session id is minted at every login; an id supplied by the client is never accepted or adopted (session fixation). The pre-login flow cookie is cleared on callback.
- `POST /api/session/end` deletes the row and clears the cookie. Disabling a user deletes their sessions. An admin can end one user's sessions.
- `POST /api/session/start` (bearer required) mints a session for a token user. This gives the operator page an HttpOnly cookie instead of a token in `sessionStorage`, and gives the session code a path that tests exercise before OIDC exists. The Token dialog keeps working unchanged.

### CSRF for cookie requests

The existing rule stays: a POST needs `Content-Type: application/json`, and Volt sends no CORS headers, so a foreign page cannot make a browser send that content type cross-origin without a preflight that fails. For cookie-authenticated POSTs three more checks apply, all before the handler runs:

1. `Origin` must be present and equal to the origin of `VOLT_PUBLIC_URL` (browsers send `Origin` on every cross-origin and every same-origin POST). Missing or different: `403 bad_origin`.
2. `X-Volt-Csrf` must equal the session's `csrf` (constant-time compare), else `403 bad_csrf`. The page reads it from `GET /api/whoami` and keeps it in memory, not storage.
3. `SameSite=Lax` stays as the browser-level backstop.

`SameSite=Strict` is not used: the provider's redirect to `/auth/callback` is a cross-site navigation, and a `Strict` cookie set during that redirect chain is not reliably sent on the next hop. The flow cookie is `Lax` for the same reason.

### OpenID Connect: server-side authorization-code flow with PKCE

Configuration is one JSON file named by `VOLT_OIDC_CONFIG` plus `VOLT_PUBLIC_URL` (for example `https://news.example.com`). The file lists providers: `id` (`google`, `microsoft`), `label`, `issuer` (exact string), `client_id`, `client_secret_env` (the name of an env var; the secret itself is never in the file), `scopes` (default `openid email profile`), allow rules (`domains` for Google's `hd`/email domain; `tenants` for Microsoft's `tid`), and `group_claim` (default `groups`). Absent file: OIDC is off, `/auth/*` answers `404`, nothing else changes. Redirect URI is `VOLT_PUBLIC_URL + /auth/callback`, built from config, never from the `Host` header.

**Routes (GET, unauthenticated, outside `ROUTES`/`ROUTE_ROLES` because they are not POSTs; this is the first state-changing GET in Volt and exists only because OAuth redirects are GETs):**

- `GET /auth/providers` returns `[{id, label}]` for the sign-in screen. No secrets.
- `GET /auth/login?provider=<id>&next=<path>`:
  1. Validate `next` (see open redirects). Store it server-side with the flow, never in `state`.
  2. Create a row in `auth_flow` (`state` primary key, `nonce`, `code_verifier`, `provider`, `next`, `binder_hash`, `created_at`, `expires_at` 10 min). `state`, `nonce` and `code_verifier` are each 32 random bytes, base64url. Cap live flows (1000) and delete expired rows on each login, so unauthenticated requests cannot grow the table without bound.
  3. Set cookie `volt_flow` (random binder, `HttpOnly`, `SameSite=Lax`, `Path=/auth`, 10 min); store only its hash. This ties the callback to the browser that started the flow (login CSRF: an attacker cannot make a victim's browser finish the attacker's flow).
  4. `302` to the provider's `authorization_endpoint` (from discovery) with `response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`, `nonce`, `code_challenge` (S256 of the verifier), `code_challenge_method=S256`.
- `GET /auth/callback?code=&state=`:
  1. Delete the flow row by `state` in the same transaction that reads it (single use: a replayed callback finds nothing). Check expiry, check the `volt_flow` cookie hash against `binder_hash`. Any failure: a plain error page, `400 flow_invalid`, audit `login_failed`.
  2. If the provider sent `error`, show a fixed message (never reflect the parameter).
  3. Exchange the code at `token_endpoint`: form POST with `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`, `code_verifier`. Timeouts 10 s, no redirects followed.
  4. Verify the `id_token` (below). The access and refresh tokens are discarded unread; `offline_access` is never requested; `userinfo` is never called. Volt keeps no provider token.
  5. Map claims to a user (below). On success: new session, `Set-Cookie`, clear `volt_flow`, audit `login`, `302` to the stored `next` (or `/`). Responses carry `Referrer-Policy: no-referrer` and `Cache-Control: no-store`, and the `code` never survives past the redirect.

**Discovery and keys.** `GET {issuer}/.well-known/openid-configuration` once, cached 24 h. The document's `issuer` must equal the configured issuer exactly, and `authorization_endpoint`, `token_endpoint` and `jwks_uri` must be `https` (a test flag allows loopback `http` for the fake only). Volt calls only URLs from configuration or from that document, never a URL supplied by a request. JWKS is cached with the response's `max-age` clamped to 5 min to 24 h. An unknown `kid` triggers one refetch, at most once per minute per provider (a forged `kid` cannot make Volt hammer the provider). If the provider is unreachable, cached keys keep verifying (public keys; availability should not fail closed) for up to 24 h past expiry, an unknown `kid` fails the login with `oidc_unavailable`, and existing sessions are unaffected.

**Which crypto.** Google and Microsoft sign ID tokens with RS256. Verifying RS256 needs only a public-key operation, `pow(signature, e, n)`, and the standard library has big integers, so `volt/oidc_jwt.py` verifies it with **no dependency**: decode the signature to an integer, raise to `e` mod `n`, and compare the whole result in constant time with the expected `EMSA-PKCS1-v1_5` encoding of the SHA-256 digest built by Volt (never parse the padding, which is what lets e=3 forgeries through). Hard rules: the algorithm must be exactly `RS256` and comes from a fixed list, never from the token's choice (`none`, `HS256` with the public key as secret, and every other value are refused); the modulus is at least 2048 bits; `e` is at least 65537; the signature length equals the modulus length. Signing is not needed in Volt and is not written. Nothing else (ES256, PS256, EdDSA) is accepted: `oidc_alg_unsupported`. With no library installed there is therefore nothing to install and nothing to degrade; an optional `cryptography` extra is deliberately not added, because two verification paths double the audit burden for no gain at these two providers. If a future provider needs ES256, that is a new, separately reviewed change.

**Claim checks**, all required, after the signature: `iss` equals the configured issuer; `aud` equals `client_id` (a list must contain it, and then `azp` must equal it); `exp` present and in the future; `iat` present, not more than 120 s in the future and not older than 10 min (a fresh code exchange gives a fresh token; a stale one is a replay); `nbf` if present; `nonce` equals the flow's, constant-time. Leeway for clock skew is 120 s, configurable, and every check uses the server's clock through an injectable `now`; the host must run NTP and the preflight (`volt/preflight.py`) should warn if it cannot tell. Microsoft's `iss` embeds the tenant id: Volt requires a specific tenant issuer (`https://login.microsoftonline.com/<tid>/v2.0`); the multi-tenant `common`/`organizations` issuers are refused in this version (see Open questions).

### Mapping claims to users and groups

Identity is the pair `(issuer, sub)`, never the email. Table `identity`: `issuer`, `sub`, `user_name`, `email`, `display_name`, `last_login_at`, primary key `(issuer, sub)`.

On a successful token:

1. **Known identity**: log that user in.
2. **Unknown identity**: link it to an existing, enabled, not-yet-linked `volt_user` whose `email` equals the token's `email`, **only if** the provider asserts it can be trusted for that: Google needs `email_verified` true and the `hd`/domain in the allow list; Microsoft needs `tid` in the allow list (an email claim from Microsoft is mutable and not verified, which is the "nOAuth" class of account takeover, so a match on email there is allowed only inside a tenant the customer controls). Otherwise refuse with `not_provisioned`.
3. **No matching user**: refuse (`not_provisioned`) unless `auto_provision` is on for that provider (default off), in which case create a user named from the email local part, sanitised to the `NAME` shape in `users.py` (`[A-Za-z0-9._:-]{1,100}`, not `desk`), deduplicated with a numeric suffix, role `viewer`.
4. A disabled user is refused (`user_disabled`); nothing is created.

Provisioned users have `volt_user.token_hash` set to the hash of random bytes nobody sees (the column is `NOT NULL UNIQUE`), so the token path can never match them; `volt user rotate` can mint a real token if a script needs one. New `volt_user` columns: `email`, `display_name`, `source` (`local` or `oidc`).

Groups from claims: `oidc_group_map(provider, claim_value, group_name)`. At login, the token's group claim values are mapped, and the user's `group_member` rows with `source = 'oidc'` are replaced to match (rows with `source = 'manual'` are untouched). Provider reality, unvalidated: Google ID tokens carry no group list (only `hd`, email, name), so Google groups come from the domain rule or from manual membership; Microsoft puts group object ids in `groups` and app roles in `roles`, and replaces the list with an overage marker above about 200 groups, which Volt cannot resolve without calling Graph (not built): the login succeeds, OIDC-sourced groups are left as they were, and the UI says "group list too large to read from sign-in".

Because group membership from the provider updates only at login, a person removed from a provider group keeps the role until their session ends. The absolute session limit bounds that; there is no back-channel logout in this version.

### Roles, groups and per-show permissions

**Global roles** become ordered: `viewer` < `operator` < `admin`. `viewer` reads everything the show UI reads and can comment, and cannot write the rundown. `ROUTE_ROLES` gains the value `viewer` for the few POSTs a viewer may make (presence, comments, notifications, ending their session). Today `do_POST` only compares `need == "admin"`; it becomes a rank comparison, and an unclassified route stays admin-only. `users.ROLES` gains `viewer`. This reverses the earlier ruling that "roles are `operator` and `admin` only" (`2026-10-31-remaining-build.md`); that ruling was about Producer and Operator being layouts, and it still holds for them. Flagged for the owner below.

**Groups.** `volt_group(name, role NULL, created_at)` and `group_member(group_name, user_name, source)`. A group's `role` is optional. A user's effective role is the highest of their own `volt_user.role` and the roles of their groups, computed per request in one query.

**Per-show access.** `show_acl(show_id, subject_kind, subject, level)` with `subject_kind` in `group`, `user` and `level` in `view` < `edit` < `run`. `view`: read and comment. `edit`: build (add, rename, remove, reorder, attach, cues, schedule, slot). `run`: edit plus go, take, hold, release, skip, kill. A show with **no** ACL rows is open to every role at that role's normal level (today's behaviour, so nothing changes for an existing install). Once a show has any ACL row, only listed subjects (and every `admin`) can touch it, and `GET /api/state` and `/rundown` leave out shows the caller cannot view. The global role still caps what ACL can grant (a `viewer` with `run` on a show is still read-only). Show-scoped routes are named in a second map, `ROUTE_SHOW_LEVEL`, from action function to level, with a resolver from the captured id to the show (items and cues go through their item). Routes that are not show-scoped (assets, spans, ingest, users) are listed in `NOT_SHOW_SCOPED`, and a test fails for any POST in neither.

The clock and the runner act as the system and are not subject to ACL.

### Edit leases

A lease is advisory UX backed by a server check, plus a versioned write that holds even when no lease is used.

Table `lease(resource_kind, resource_id, holder, client_id, acquired_at, renewed_at, expires_at)`, primary key `(resource_kind, resource_id)`. v1 resource kind is `item` (slug and script). The rundown structure is not leased: it already has `orderVersion`, and a lock on the order would make one producer a bottleneck.

- **Acquire** (`POST /api/items/{item}/lease`, body `{clientId}`) is one upsert: insert, or update where the existing row is expired or already this holder and client. Rowcount says who won; then read the row. TTL 45 s. The browser renews every 15 s while an item editor is open. The same call acquires and renews (idempotent). The server's clock is the only clock; clients never send times.
- Two tabs of one person are two clients (`clientId` is a random per-tab value), so a person cannot silently overwrite themselves.
- **Release** on closing the editor or saving; expiry covers a closed laptop.
- **Takeover** (`.../lease/takeover`) steals a *live* lease. Allowed to anyone with `edit` on the show, because a producer must be able to fix a script when its holder has walked away with a live tab; the previous holder gets a `lease_taken` notification, audited, and their next renew returns `409 lease_lost` with the new holder's name. Their unsaved text stays in the browser textarea with a "Copy my text" action; the server never merges.
- **Enforcement.** `rename` (`POST /api/items/{item}/rename`) accepts `version` and `clientId`. If a live lease on the item belongs to someone else (or another client of the same user): `409 leased` with `holder` and `expiresAt`. If `version` is sent and no longer matches the item's content version (sha256 over slug and script, first 16 hex, computed like `order_version`, no new column): `409 stale_item` with the current item. With neither a lease nor a `version` (older clients, scripts) the write behaves as today. Both checks run inside the same transaction as the write, under `show_lock` as `building.rename_item` already does.
- **Never on the air path.** A lease never blocks `take`, `go`, `hold`, `release`, `skip` or `kill`. An item that is not `ready` cannot be leased (`409 not_editable`, the code `building._need_ready` already raises), and a take of a leased item simply ends the editing: the holder's next renew gets `409 not_editable`.
- Postgres and clocks: with several server processes on skewed clocks a lease can be a few seconds short or long. The TTL absorbs that; the spec records it as a limit.

### Presence

Table `presence(user_name, client_id, show_id, view, seen_at)`, primary key `(user_name, client_id)`, `view` one of `show`, `library`, `item:<id>`. `POST /api/presence` every 10 s with `{clientId, show, view}`; a row is live for 30 s; rows older than 5 min are deleted opportunistically. `GET /api/state?show=` (built in `Handler._operator_state`) gains `presence` (name, display name, view) and `leases` (item, holder, seconds left) for that show, filtered by the caller's view access. So presence rides the existing 2-second poll (`POLL_MS = 2000`, 6 s hidden) for *reads*, and its *write* is a separate 10-second POST, because a GET must not write.

**Server-sent events were considered and not chosen.** For: lower latency, no 2 s lag on a lease appearing. Against: `EventSource` cannot send an `Authorization` header, so bearer users would be excluded and cookie users would need the full CSRF story for a long-lived GET; each stream pins a thread of `ThreadingHTTPServer`; the runner and clock are other processes, so the stream would poll the database anyway (no cross-process pub/sub on SQLite); and buffering proxies (the Caddy/nginx recipes in `docs/tls.md`) need flush configuration or the stream stalls silently. The 2 s lag is harmless because the server enforces leases regardless of what a page shows. Revisit when a customer has more than a few dozen concurrent operators.

### Comments, mentions, notifications

- `comment(id, target_kind, target_id, show_id, author, body, parent_id, created_at, resolved_at, resolved_by, deleted_at)`. `target_kind` is `item`, `show` or `asset`; `show_id` is denormalised for access checks (NULL for an asset not on a show). `body` is 1 to 4000 characters. One reply level (`parent_id` must be a top-level comment on the same target). v1 has no edit: the author or an admin can soft-delete (`deleted_at`; body hidden), anyone with `edit` can resolve and reopen a thread.
- **Mentions.** The server parses `@name` tokens against enabled users (the `NAME` shape) and writes `comment_mention(comment_id, user_name)`. A name that does not resolve, or whose user cannot view the show, stays plain text and notifies nobody (no leaking that the name exists). The UI renders the body with `textContent`, mention spans created from the server's list, never `innerHTML`.
- `notification(id, user_name, kind, show_id, target_kind, target_id, comment_id, actor, created_at, read_at)` with kinds `mention`, `reply` (on a thread you started or are in), `lease_taken`. In-app only. Unread count rides `/api/state`; the list is `GET /api/notifications`. Older than 90 days are purged by the runner's retention pass.
- Rate: 30 comments per user per minute, `429 rate_limited`.

### The audit view

Today: `Store.append_audit` (insert-only by convention) and `GET /api/audit?target=` (last 200 for one id). Added:

- `GET /api/audit` accepts `actor`, `action`, `kind`, `target`, `from`, `to` (UTC `YYYY-MM-DDTHH:MM:SSZ`), `before` (an opaque cursor over `(at, id)`), `limit` (1 to 500, default 100), `format=csv`. Unknown parameters stay `422 bad_input`. Without `target` the call is admin-only; with `target` it stays as today. Indexes on `audit(at)` and `audit(actor, at)` in a migration (both databases).
- CSV cells that begin with `=`, `+`, `-` or `@` are prefixed with a single quote (spreadsheet formula injection); fields are quoted per RFC 4180.
- New audited events: `login`, `login_failed`, `logout`, `session_end`, `role_change`, `group_*`, `acl_set`, `lease_acquire`, `lease_takeover`, `lease_release`, `comment_*`. Heartbeats (presence, lease renew) are not audited: they are ephemeral state, not records, and would bury the log. `login_failed` is rate-limited (one row per second, with a count) so a hostile client cannot fill the table.
- The audit never contains a token, cookie, authorization code or client secret, and `detail` for a comment holds ids, not bodies.

### Data model (a new migration; numbers assigned when built)

New tables: `session`, `auth_flow`, `identity`, `volt_group`, `group_member`, `oidc_group_map`, `show_acl`, `lease`, `presence`, `comment`, `comment_mention`, `notification`. New `volt_user` columns: `email`, `display_name`, `source`. New indexes on `audit`. Tables are created by base-table creation and by `IF NOT EXISTS` statements per backend, as migrations 16, 17 and 21 do, with `TIMESTAMPTZ` on Postgres and text stamps on SQLite. Split across several migrations, one per task that needs tables, so each ships alone.

### API (all POSTs JSON; all classified; errors use `{ok:false, error:{code, reason}}`)

| Route | Role | Notes and refusals |
|---|---|---|
| `GET /auth/providers`, `/auth/login`, `/auth/callback` | none | `404` if OIDC off; `422 bad_provider`; `400 flow_invalid`, `oidc_unavailable`, `oidc_token_invalid`, `oidc_alg_unsupported`, `not_provisioned`, `user_disabled` as an error page |
| `POST /api/session/start` | viewer (bearer only) | `{ok, csrf}`; sets cookie. `401` without a bearer |
| `POST /api/session/end` | viewer | clears the cookie |
| `GET /api/whoami` (extended) | any | adds `effectiveRole`, `groups`, `via`, `displayName`, `csrf` (session only) |
| `GET /api/groups` | admin | groups, members, claim maps |
| `POST /api/groups` `{name, role?}` | admin | `409 group_exists`, `422 bad_name`, `bad_role` |
| `POST /api/groups/{name}/members` `{add?, remove?}` | admin | `404`, `422` |
| `POST /api/groups/{name}/claims` `{provider, value, remove?}` | admin | claim-to-group map |
| `POST /api/groups/{name}/delete` | admin | `409 group_in_use` if an ACL references it |
| `POST /api/users/{name}/role` `{role}` | admin | `409 last_admin` (reuses the rule in `users.disable`), `422 bad_role` |
| `POST /api/users/{name}/identity` `{email}` | admin | pre-link by email; `409 email_taken` |
| `POST /api/users/{name}/end-sessions` | admin | |
| `GET /api/shows/{show}/access` | operator | the ACL |
| `POST /api/shows/{show}/access` `{subject_kind, subject, level|null}` | admin | null removes; `422`, `404` |
| `POST /api/items/{item}/lease` `{clientId}` | operator + show `edit` | `409 leased`, `409 not_editable`, `404`; returns `expiresAt` |
| `POST /api/items/{item}/lease/release` | operator + `edit` | idempotent |
| `POST /api/items/{item}/lease/takeover` | operator + `edit` | `409 not_leased` if free |
| `POST /api/items/{item}/rename` (extended) | operator + `edit` | adds `version?`, `clientId?`; `409 leased`, `409 stale_item` |
| `POST /api/presence` `{clientId, show?, view}` | viewer | not audited |
| `GET /api/comments?kind=&target=` | any with show `view` | |
| `POST /api/comments` `{kind, target, body, parent?}` | viewer + `view` | `422`, `429 rate_limited`, `404` |
| `POST /api/comments/{id}/resolve` `{resolved}` | operator + `edit` | |
| `POST /api/comments/{id}/delete` | viewer (author) or admin | `403 forbidden_role` |
| `GET /api/notifications`, `POST /api/notifications/read` `{ids?|all}` | viewer | own only |
| `GET /api/audit` (extended) | admin without `target` | see above |

Show-scoped refusals add `403 forbidden_show` (`reason` names the level needed, never lists who else has access).

### UI, in the existing redesign system

- **Sign-in.** On a 401 the page already opens the Token dialog. When `GET /auth/providers` returns providers, the dialog also shows "Sign in with Google" and "Sign in with Microsoft" as **anchors** (`href="/auth/login?provider=...&next=..."`), not forms (the CSP has `form-action 'none'`). The Token path stays for scripts and recovery. The header shows the display name and a Sign out; `whoami` already feeds `#who`.
- **Presence.** A strip under the Show header: avatars as initials in a circle, each with a name tooltip and a visible text list for keyboard and screen-reader users ("Dana, Sam: viewing Lead"). New STATE kinds: `editing` (icon `lock`, word "Editing", `st hold`), `yours` (`ready`, "You", `st ok`). Never red: red is only a live path.
- **Lease on an item row.** `Dana editing` chip (icon plus word). Opening the editor on a free item acquires silently; on a leased item it shows the holder, time left, a disabled field, and a two-press **Take over** button (same arming pattern as Kill). Losing a lease shows an alert-icon banner with "Copy my text".
- **Comments.** A panel in the Show screen's item detail: thread list, one box, Resolve per thread, mention autocomplete from the roster (keyboard operable, listbox pattern). A comment count chip on rows with open threads.
- **Notifications.** A header bell button with a count and a list dialog; each entry links to the item. New sprite icons needed: `comment`, `bell`, `edit`, `history`; the sprite test must list them.
- **Activity screen** (admin): filter bar, table, load-more, CSV button. A row's action is a word plus icon, actor as text.
- **Groups and access** panel under the existing users admin.
- New colour states go into both theme sets and the contrast test. No shortcut keys are taken; existing keys are unchanged.

### Behaviour rules

1. A bearer token behaves exactly as before, byte for byte, in every existing test.
2. Role, group and enabled state are read per request. No cached authority outlives a request.
3. If more than one rule disagrees, the stricter wins: ACL and global role must both allow.
4. The on-air path (take, go, hold, release, skip, kill) is never blocked by a lease, comment or presence failure. If the new tables are unreadable, those routes still run and the page shows a "collaboration unavailable" notice.
5. A write that loses a conflict changes nothing and says who and what (`leased`, `stale_item`, `stale_order` with the last reorder's actor from the audit).
6. A page that cannot tell it holds the lease (renew failed twice) stops saving and says so.

### Security analysis

- **Open redirect.** `next` must start with exactly one `/`, contain no backslash, no control character, no `//` prefix, no scheme, and length at most 512; anything else becomes `/`. It is stored server-side, not carried in `state`. `redirect_uri` is built from `VOLT_PUBLIC_URL`. Never from `Host`, `Origin` or `Referer`.
- **Token storage.** Provider tokens are discarded after verification. The browser holds only an `HttpOnly` cookie; the CSRF value is held in page memory. The client secret is read from the environment at start, never written, logged or audited; the token-endpoint error body is logged as the provider's error code only.
- **Session fixation.** New id at login, client-supplied ids ignored, flow cookie single use.
- **Login CSRF.** `state` plus the flow-cookie binder; an attacker's callback URL does nothing in a victim's browser.
- **Replay.** Authorization code is single use at the provider; the flow row is deleted on first read; `nonce` binds the token to the flow; `iat` older than 10 min is refused.
- **Algorithm confusion and forged signatures.** Fixed `RS256` allow-list; full-encoding compare; key size and exponent floors; `kid` must exist in the provider's JWKS.
- **Clock skew.** 120 s leeway each way, server clock only, injectable in tests; the preflight warns if time cannot be confirmed.
- **Account takeover by email.** Identity is `(issuer, sub)`; email linking only inside a verified or tenant-restricted setting; unlinked mail never creates admin.
- **Privilege from claims.** Groups and roles come only from a verified token's claims and the admin-set map; an unmapped group does nothing. A claim never grants `admin` unless an admin mapped it so.
- **Stale authority.** Role changes at the provider reach Volt at next login; bounded by the absolute session limit and by the admin's "end sessions". Stated, not hidden.
- **XSS and clickjacking.** Existing CSP and `frame-ancestors 'none'`; comments rendered as text; the cookie is `HttpOnly`.
- **Resource exhaustion.** Flow table cap, JWKS refetch limit, `login_failed` row limit, comment rate limit, presence row expiry.
- **SSRF.** Volt only fetches URLs from its own configuration or from the configured issuer's discovery document.
- **Timing.** Hash comparisons use `hmac.compare_digest`, as `users.authenticate` does.
- **`/auth` is unauthenticated surface.** When no auth is configured, `_auth_configured` is false and `/auth/*` answers `404` rather than opening a login that cannot succeed.
- **Residual risk the tests cannot close**: real provider behaviour (below).

## What the tests will and will not prove

Will prove, with fakes and a frozen clock: PKCE and `state`/`nonce` handling against a fake provider that can misbehave on request (wrong nonce, audience, issuer, expired, stale `iat`, `alg: none`, `HS256` signed with the public key, wrong `kid`, rotated keys, truncated or padding-forged signature, small modulus); the verifier against signatures made by a second, independent signing implementation (and by `openssl` when present); open-redirect inputs; cookie attributes; fixation; CSRF refusals; per-request role and disable effect; ACL and role ranking over every POST route; lease acquire, expiry, takeover and the race of N acquirers on both databases (exactly one winner); stale and leased writes; presence expiry; mention resolution and access filtering; audit filter, cursor and CSV escaping; forward migrations on both databases; UI states in node tests and the harness.

Will not prove: that Google or Microsoft will accept this client or issue these claims; that `hd`, `groups` and overage behave as described; that JWKS rotation timing matches production; browser cookie behaviour on Safari over `http://localhost`; behaviour through every proxy; real-world clock skew; that a newsroom finds the lease and presence model comfortable at speed. Everything touching a real provider is labelled unvalidated in the docs, the sign-in screen's admin help, and the changelog, until someone runs it against a real tenant.

## Left to the builder

Exact column types and index definitions; error page markup; the exact wording of `reason` strings; the HTTP client seam's shape (a small `get_json`/`post_form` object injected for tests); cookie parsing helpers; the mention autocomplete's keyboard details; the test RSA key (generate once, commit as a labelled throwaway, never reuse); how `ROUTE_SHOW_LEVEL` resolvers are written; the Activity screen's layout inside the redesign rules.

## Calls I made, for the owner to check

- **RS256 verified in the standard library**, no `cryptography` extra, ES256 refused. Smaller supply chain, one reviewed path; a provider needing ES256 means new work.
- **Cookie sessions in addition to tokens**, `SameSite=Lax` plus Origin and per-session CSRF header, because the provider redirect makes `Strict` unreliable.
- **Added a `viewer` role**, reversing the earlier "operator and admin only" note. It is the cheapest honest answer to "finer roles".
- **No ACL rows means open**, so installs do not change on upgrade; the cost is that adding the first row to a show suddenly hides it from others.
- **Leases on items only**, TTL 45 s, renew 15 s; the rundown order stays optimistic. Takeover of a live lease is allowed to any editor on the show, with a notification and an audit row.
- **Poll, not SSE**, for the reasons above.
- **Heartbeats are not audited.** This is a narrow reading of "every write is audited": heartbeats are ephemeral presence, and acquire, takeover, release and everything durable are audited. Say if you want it stricter.
- **No comment editing** in v1.
- **Audit remains insert-only by convention, not hash-chained.** A chain would make tampering detectable, not impossible, and complicates retention; it can be added without changing the API.
- **OIDC unavailable on a plain-http LAN install**; tokens remain.
- **Session defaults** idle 8 h, absolute 14 h, one shift.
- Groups at the provider are read from the ID token only; Graph lookups for overage are not built.

## Open questions

1. **Auto-provisioning.** When someone from the customer's company signs in and nobody created a user for them, should Volt refuse (default here, safest, an admin must add them first) or create them as `viewer`?
2. **Microsoft tenant model.** Is each customer a single Microsoft tenant (supported here), or does any hosted customer need sign-in from several tenants through `common`? The second needs per-tenant issuer handling and more risk review.
