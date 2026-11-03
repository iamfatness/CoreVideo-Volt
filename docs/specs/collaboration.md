# Collaboration and identity: what is built, and what is not validated

Status: built (identity plan tasks 1 to 9, migrations 22 to 34). The design and the owner's decisions are in [the design](../superpowers/specs/2026-11-01-collaboration-identity-design.md); the plan is [here](../superpowers/plans/2026-11-01-collaboration-identity.md). Roles, groups and the activity log are in [access.md](access.md); every route is in [operator-api.md](operator-api.md); the tables are in [data-model.md](../data-model.md); configuration is in [operations.md](../operations.md).

## What exists

| Piece | Where | Unit it is tested at |
|---|---|---|
| Viewer role, groups, effective role (highest of direct and group), last-admin rule | `access.py`, migration 22 | `test_access.py` |
| Activity view: filters, cursor paging, CSV with formula neutralising | `store.list_audit_page`, `auditview.py`, migration 23 | `test_audit_view.py` |
| Cookie sessions, `Origin` and per-session CSRF checks, idle and absolute expiry | `sessions.py`, migration 26 | `test_sessions.py` |
| RS256 verification with no dependency (alg, `kid`, key size, exponent 3, `iat`) | `oidc_jwt.py` | `test_oidc_jwt.py`, `oidc_fake.py` |
| Sign-in with Google or Microsoft: PKCE, `state`, `nonce`, `iss`, `aud`, tenant and domain rules, group-claim mapping | `oidc.py`, migration 30 | `test_oidc_flow.py` |
| Per-show access (view, edit, run) for users and groups | `access.py`, migration 31 | `test_show_access.py` |
| Edit leases on items (45 s, renewed every 15), versioned rename, presence (10 s heartbeat, live 30 s) | `leases.py`, migration 33 | `test_leases.py` |
| Comments with one reply level, mentions, notifications, a bell | `discuss.py`, migration 34 | `test_discuss.py` |

Rules that hold across all of it, each with a test: bearer tokens behave as they always did; every POST route is in `ROUTES` and `ROUTE_ROLES` and every route that names a show is classified; every 2xx write is audited (a lease renew and a presence heartbeat are the named exceptions); `audit` is append only (`test_docs.AuditIsAppendOnly`); no secret, token, cookie, code or comment body reaches the audit table, the log or a URL; take, go, hold, release, skip and kill never read a collaboration table, and a page whose collaboration tables are unreadable says so and carries on; a disabled user, a removed group member or an ended session loses access on the next request.

## What is not validated

- **Google and Microsoft.** Nothing has been run against a real provider. The flow is exercised against `tests/oidc_fake.py` only. What the real providers send (`hd`, `tid`, `email_verified`, the group overage marker, key rotation timing, the Microsoft v2 issuer form) is from published documentation, not observation. Before relying on it: register a test app at each provider, sign in with a real account, a disabled account and an unknown account, and read the audit rows.
- **More than one real person.** Leases, presence, comments and notifications were checked by tests, by two simulated users against one server (a real lease conflict, takeover and `lease_lost`, see the PR for the run), and by one person in an isolated browser. No team has used them over a real network, with a proxy in front, with clocks that disagree, or with browsers left open overnight.
- **Postgres under load.** The concurrent lease and reorder tests run on SQLite always and on Postgres when `VOLT_TEST_PG_DSN` is set; they have not been run against a production-sized database.
- **Auto-provisioning.** An unknown sign-in is refused by default (`auto_provision` is off), per the owner's call. Whether to turn it on is open, as is single Microsoft tenant per customer versus `common` (refused today).

## Known gaps

- The page does not yet hide write controls from a viewer (Start recording, for one, is shown); the server refuses with `403 forbidden_role` and the page shows that. Hiding them is a follow-up.
- Asset comments are admin-only, and viewers get no mention roster, until the owner decides otherwise.
- Notifications are in-app only. There is no email, push or chat delivery.
- No SAML, SCIM, back-channel logout or passwords (non-goals in the design).
