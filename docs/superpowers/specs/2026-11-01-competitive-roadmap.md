# Volt: roadmap against the competition

Status: draft for the owner's review. Date 2026-11-01. Nothing here is built.

## Where this comes from

After the nine workstreams of [the remaining build](2026-10-31-remaining-build-design.md) (slot length and back time, scheduling, FCPXML, the archive tier, the MOS sender, the HyperDeck adapter, the light theme, test gaps), the owner asked what Volt lacks against its competitors. The answer is eight gaps. This page orders them. Each has its own spec and plan, linked below.

**Caveat on the competitor facts.** The comparison is from general knowledge of ENPS, Avid iNEWS, Dalet Galaxy, Octopus, Ross Inception and OverDrive, Vizrt Mosart, Iconik, Cantemo, Avid MediaCentral, Frame.io and Moments Lab. It is not a feature audit of any of them. Before a gap is built, the builder should re-check the competitor feature it answers against that vendor's current documentation.

## Owner decisions that shape the order

| Question | Answer (2026-11-01) |
|---|---|
| First customer | A daily news or live-show team; the production-house (MAM) track second ("both, news first") |
| AI indexing | Pluggable: a local engine first, cloud adapters optional later |
| Sign-in | OpenID Connect first (Google and Microsoft); SAML later if a customer needs it; tokens stay for machines |
| Editor | Adobe Premiere Pro panel first; Resolve and Avid later |
| Delivery | Both hosted and installed |
| Licence | Proprietary, all rights reserved (LICENSE, NOTICE) |

## The eight gaps and where each sits

| # | Gap | Spec | Plan | Size | Depends on |
|---|---|---|---|---|---|
| 1 | Collaboration and identity | [spec](2026-11-01-collaboration-identity-design.md) | [plan](../plans/2026-11-01-collaboration-identity.md) | Large | none: the foundation |
| 2 | Newsroom scripts and workflow | [spec](2026-11-01-newsroom-scripts-design.md) | [plan](../plans/2026-11-01-newsroom-scripts.md) | Large | 1 (approval needs people) |
| 3 | Control of more devices | [spec](2026-11-01-device-control-design.md) | [plan](../plans/2026-11-01-device-control.md) | Large, in adapter-sized pieces | none (the room registry exists); 1 for per-user audit |
| 4 | Scale and operations | [spec](2026-11-01-scale-operations-design.md) | [plan](../plans/2026-11-01-scale-operations.md) | Large | 1; blocks the first hosted customer |
| 5 | Search, metadata and AI indexing | [spec](2026-11-01-search-indexing-design.md) | [plan](../plans/2026-11-01-search-indexing.md) | Extra large | none; 4 for load numbers |
| 6 | Review, approval and delivery | [spec](2026-11-01-review-delivery-design.md) | [plan](../plans/2026-11-01-review-delivery.md) | Large | 1 (reviewers, approvers); 5 helps |
| 7 | Editor integration (Premiere) | [spec](2026-11-01-editor-integration-design.md) | [plan](../plans/2026-11-01-editor-integration.md) | Large | 1 (panel token); 5 (search API) |
| 8 | Mobile and remote | [spec](2026-11-01-mobile-remote-design.md) | [plan](../plans/2026-11-01-mobile-remote.md) | Medium | 1 (sessions); 2 (what a phone approves) |

## The order

**Phase 0: prove what is already built (runs alongside everything, blocks nothing).** The HyperDeck cue against a real deck, the MOS sender against any MOS device, the archive tier against a small real bucket, the FCPXML, xmeml and EDL exports opened in a real editor (this is the first part of the editor plan), and the dress rehearsal. Every one of these is tested only against fakes today. A short list of real checks is worth more than a long list of new features, and each can change the specs below.

**Phase 1: identity and collaboration (gap 1).** It comes first because everything else assumes a person: story approval, review comments, panel tokens, per-user audit, phone sign-in. It also removes the shared-token weakness that blocks hosting.

**Phase 2: the news track, in parallel.**
- *Newsroom scripts and workflow (gap 2)* turns the rundown into a newsroom tool: script editor, story status and approval, prompter feed, wire intake, floating stories, templates.
- *Device control (gap 3)* builds on the room registry and the fired-means-observed rule: the adapter contract, then ATEM, CasparCG, graphics templates over MOS, PTZ, audio scenes, VDCP. Each adapter is its own shippable slice and its own real-equipment check.

**Phase 3: scale and operations (gap 4), before the first hosted customer.** Safe multiple servers (the in-process sequencing lock is the blocker), monitoring and alerts, upgrade and restore drills, and the tenancy decision. Hosted launch is gated on this phase, not on features.

**Phase 4: the MAM track.** *Search and indexing (gap 5)*, then *review and delivery (gap 6)*, then *editor integration (gap 7)*; the panel needs the search API, and the review work reuses the same comment and token model.

**Phase 5: mobile and remote (gap 8).** The phone layout is only useful once there are approvals, comments and real sessions to act on, so it comes after gaps 1 and 2. The conditional-request work (ETags on the state poll) is cheap and can ship earlier.

## What each phase needs from the owner

| Phase | Needs |
|---|---|
| 0 | A deck, a MOS device or newsroom system, a cheap cloud bucket, Resolve or Premiere installed |
| 1 | Google and Microsoft developer registrations for the sign-in apps (free) |
| 2 | An ATEM or a CasparCG machine to test against; a teleprompter application if one is in use |
| 3 | A place to rehearse a two-server deployment (two cloud VMs or containers) |
| 4 | A GPU or a decision to start CPU-only; an Adobe developer account for the panel; a staging account on a platform if posting is wanted |
| 5 | A phone and a tablet |

## Rules for every gap

1. **Off by default where it sends or exposes.** Anything that talks to a device, posts publicly or opens access to outsiders is opt-in, visible when on, and has a global stop.
2. **Fired means observed.** A device action counts only when the device's own state shows it.
3. **The rights gate is never bypassed.** Cleared, legal hold and archived apply to every delivery, share and panel action, enforced by the server.
4. **Honest about proof.** Fakes prove structure and ordering; anything needing real equipment, accounts or a real editor is written as unvalidated until someone runs it, and each plan carries the manual check.
5. **Same design system.** Icon, word and colour; dark and light themes with measured contrast; every new screen axe-clean at desktop and phone sizes.
6. **Migrations tested forward on SQLite and Postgres; every new POST classified in `ROUTE_ROLES` and audited.**
7. **CI is read per job.** After the last month's mistake of reading check names instead of results, a slice is merged only after each job's conclusion is read.

## Risks of the whole programme

- **Breadth.** Eight gaps is years of work for a competitor team; the order above wins the news track first and leaves the MAM track thin on purpose. A competitor that is strong in one track (a newsroom system with MOS for graphics, or Iconik for search) may be better bought and integrated than rebuilt: the MOS sender and the export files are the integration points.
- **Everything real is unproven.** The four newest pieces have only met fakes. Phase 0 is the cheapest risk reduction in the plan.
- **Tenancy retrofits are expensive.** Whatever gap 4 recommends should be decided before many new tables are added in phases 1 and 2.
- **Personal data.** Face indexing and external review links need explicit privacy handling before they ship.

## What writing the eight specs found

These came from the agents reading the code, and they change the plan:

- **The "two `serve` processes are unsafe" premise is half stale.** The in-process `_SEQ` lock guards only `act_go`, `act_skip` and `act_kill`, and each already decides under a Postgres show advisory lock. The real in-process-only lock is `_REC` (record start and stop, held across a room wait of up to 30 seconds); the scale spec replaces it with a lease row. Making a second server safe is smaller than assumed.
- **`/api/state` is the scaling problem.** It returns every asset and calls `room.observe()` on every poll, the server is HTTP/1.0 with a thread per request, and there is no rate limit anywhere. Gap 8's conditional requests and gap 4's request economy overlap: do them once, in gap 4, and let the phone work reuse them.
- **A `viewer` role is added by two specs** (identity and mobile), reversing the earlier "operator and admin only" ruling. They must share one definition: the identity spec owns it.
- **Migration numbers are not claimed.** The schema head is 21 today; every task takes the next free number at build time, and the search spec warns that `retention.purge_asset` deletes a hand-written table list, so every new table must be added there or a Postgres purge will refuse.
- **The earlier remaining-build documents are stale** (they describe the archive tier, MOS sender, slot length and scheduling as not built). The specs cite the code, not those documents; the built-state is in the changelog and the per-feature specs.
- **Unverified protocol facts.** Several specs rest on memory: the ATEM wire protocol (may not be published; the plan says to move CasparCG first if it cannot be confirmed), the MOS object and template message names, Adobe's UXP and Premiere APIs, EBU-TT-D and caption defaults, and the licences of popular face and object model weights. Each plan makes the builder read the published documents before coding.
- **Face data is the biggest exposure.** The search spec can ship without faces in release one, which removes the largest privacy and legal risk.
- **An existing bug was found:** `media._asset_row` does not filter deleted assets, so the proxy route still serves a deleted asset's proxy to anyone with a ticket or token.

## Questions that only the owner can answer

Each spec carries its own list; these are the ones that change what gets built first.

| Spec | Question | Default in the spec |
|---|---|---|
| Identity | A person signs in with the company account and no Volt user exists: refuse, or create a `viewer`? | Refuse |
| Identity | Is each customer a single Microsoft tenant, or does a hosted customer need several through `common`? | Single tenant |
| Scripts | May a writer approve their own story? Does the first customer run with the approval gate on? | No / ask |
| Scripts | Which wire services and which prompter does the first customer have? | Generic feeds; a prompter page |
| Devices | What gear does the first customer's control room run? Is there a second person to confirm an on-air retry? | Order: ATEM, CasparCG, MOS objects, PTZ |
| Search | May a missing required field block publish or clearing? Is there a policy on storing face templates? Faces in release one? | No / written basis per person / off |
| Review | Which delivery targets first? Who approves a delivery before groups exist? Do reviewers see each other's comments? | S3 prefix / `VOLT_APPROVERS` / no |
| Editor | Will you register an Adobe developer account, and can the customer's IT push a `.ccx`? Which Premiere versions? | Ask |
| Scale | Provider-managed HA Postgres? Are SLOs targets or contract commitments? Data retention after cancellation? One region? | Yes / targets / ask / one |
| Mobile | Restrict a phone session to hold, release, skip and approve? Web Push, webhook or in-page alerts? Live program picture? | Yes / in-page / no |

## Open questions

1. Is a hosted multi-tenant deployment really required, or is one deployment per customer enough for the first year? The scale spec recommends an answer; the owner decides.
2. Which newsroom does the first news customer run today (so the wire intake and the MOS profile match what they actually use)?
3. Is there a customer who needs SAML before the MAM track, which would move it earlier?
