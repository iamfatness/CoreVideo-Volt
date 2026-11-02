# Volt: review, approval and delivery, design

Status: draft for the owner's review. Nothing here is built. Date 2026-11-01.

## Why

Volt cuts a span into three fixed versions (`house`, `ott`, `social`; `publish.KINDS`, `deliverables.PROFILES`), stores them through the storage seam, signs short media tickets (`tickets.py`) and refuses an uncleared or held asset (`Store.assert_publishable`). That is where it stops. A news or production team then does the rest by hand: it mails a file to a producer, collects notes by reply, asks "is this approved?" in chat, makes subtitles in another tool, checks levels by ear, re-encodes for a platform and uploads by hand. Frame.io, Iconik and Dalet put time-coded comments, an approval trail, outside review links, captions, automated quality control, presets and delivery targets in the same place as the media.

Honest caveat: what those products do is from general knowledge of their public positioning, not an audit of any of them. Nothing here claims parity with a named feature. The test is whether Volt's own first customer, a daily news or live-show team, can get a cut reviewed, approved, captioned, checked and delivered without leaving Volt, and whether a production-house MAM team, second in line, can do the same with more outside reviewers and more presets.

## Owner decisions already made

Recorded as the owner's; this spec does not reopen them.

1. First customer is a daily news or live-show team. A production-house MAM track is second.
2. Sign-in is OpenID Connect first, and a separate spec covers identity. This spec assumes per-user identity and groups exist and refers to them as "the identity spec"; it designs none of it.
3. External reviewers are not users. Their access is a share link with a scoped, expiring, revocable token.
4. The rights gate (cleared, legal hold) applies to every delivery and every share.
5. Tests use fakes and never the network. Anything that needs a real platform account is written as unvalidated.
6. The operator UI is static files under a same-origin CSP, following `docs/brand-guide.md` and `docs/specs/operator-ui.md`.
7. Every new POST is classified in `ROUTE_ROLES` and audited. Schema changes are numbered migrations tested forward on SQLite and Postgres.

## Goals

1. A producer opens a span, renders a review cut, and gets time-coded comments with replies and a resolved state, from staff and from outside reviewers.
2. An approval trail (requested, approved, changes requested) records who and when, and the latest valid state is visible on the span and optionally gates delivery.
3. A share link gives an outside reviewer a static page, no login, view-only or comment-and-decide, expiring, optionally password-protected, revocable at once, scoped to one review cut.
4. Captions are generated from the existing word timings, edited, and exported as WebVTT and SRT first, then TTML (IMSC1 text) and EBU-TT-D, with an optional burn-in render.
5. Automated QC (loudness against a target, black, silence, freeze) runs as a runner job, with thresholds in configuration, and the result is stored and shown.
6. Transcode presets beyond the three fixed ones, validated against an allow-list, never free-form ffmpeg arguments.
7. Delivery targets as adapters (S3 prefix first, SFTP second, platform APIs last), idempotent and retried, with a status per delivery.

## Non-goals

- Notification by email, chat or push. A share link is copied by the producer; Volt sends nothing. (A sender is a later, opt-in spec; see the MOS sender's rules in `remaining-build-design.md` for how an outbound path must be gated.)
- Sign-in, groups, OIDC and role mapping: the identity spec.
- Frame-accurate drawing and annotation on the picture. A comment carries a frame and optionally a range, not a shape.
- Version-to-version visual compare, and carrying comments from one revision to the next automatically.
- Live captioning, translation, and speaker diarisation. Cues come from the existing transcript only.
- Normalising loudness. Volt measures and reports; it never changes a level unless a later spec adds a preset that says so.
- A real YouTube, Vimeo or CDN account in any test, and any claim that those adapters work against the live service.
- DRM, watermarking, and forensic marking of review copies (see the security analysis for what the review cut does instead).
- Multi-tenant isolation between organisations.

## Anti-goals

Failure even if it works:

- A share link that lets an outsider reach anything but the one review cut and its comments: another span, the asset id, the hi-res, the transcript, rights state, or any `/api/` route that takes a bearer token.
- A share or delivery that goes out for an uncleared or held asset, including one that was cleared when the link was made and has since been held.
- A delivery that posts to a public place on one click, or that retries a public post without telling anyone.
- A retry that delivers twice, or leaves a half-uploaded object visible under the final name.
- A QC result that says "pass" because a check did not run.
- A preset field that reaches ffmpeg as an unvalidated string.
- An approval that stays "approved" after the cut it approved changed.
- A claim, in any doc, that an adapter or a caption format is validated when only a fake or a schema check ran.

## Constraints

- Stack as today: Python standard library plus existing optional extras; SQLite and Postgres both (`VOLT_DB`). The one new optional dependency is an SFTP client (paramiko), imported lazily, absent means the SFTP adapter reports `not_installed`. Schema changes are numbered migrations; this document does not give a number because other work may land first. Each task in the plan that adds tables adds its own migration, tested forward from the historical shapes on SQLite and Postgres.
- Every new POST is classified: staff routes in `serve.ROUTE_ROLES` (a test fails otherwise, `tests/test_users.py`), reviewer routes in a separate table described below with its own failing-if-missing test. Every successful write lands in the audit log (`Store.append_audit`).
- The operator UI stays static files, no `http(s)://`, no inline style or script, icons from the sprite, status as icon plus word plus colour, existing keys unchanged, light and dark token sets both updated and contrast-tested. The reviewer page is a second static page with the same rules.
- Anything that talks to a network (SFTP, S3 prefix other than the configured one, a platform) runs in the runner with timeouts and bounded retries, never in the HTTP server.
- Credentials are environment only, as in `docs/specs/storage.md`: no key, password or token for a delivery target is stored in the database or the audit log.
- Tests use fakes: a fake backend, a fake SFTP client, a fake platform client, a frozen clock, a fake ffmpeg runner for the QC parsers. No test touches the network. Tests that need real ffmpeg skip when it is absent, as the existing deliverable tests do.

## The design

### 1. Review cuts

Reviewers do not get the asset's proxy. The proxy (`GET /api/assets/{id}/proxy`) is the whole recording; a share for one span must not hand out the rest of a recording. Instead a runner job renders a **review cut**: the span only, from the hi-res at the span's source timecode (the same frame arithmetic as `deliverables.render`: quarter-frame seek, frame-count stop), 720p-or-smaller H.264 at a high CRF, AAC mono-or-stereo 96k, with the burned-in source timecode off by default. It is written to a local directory `VOLT_REVIEW_DIR` (default `<VOLT_OUT>/review`), not through the storage seam: review cuts are disposable, they must be revocable (an S3 presigned URL cannot be taken back), and Volt serves them itself with byte ranges like the proxy.

A review is a row per span per revision. A new revision is made when the producer asks for one, and is required once the span's `tc_in` or `tc_out` has changed. Older revisions stay readable; their comments stay on them. Nothing is carried over.

**Why a cut rather than the existing proxy:** the cut scopes exposure to the exact frames under review and makes "revoke" real (delete the file). The price is a transcode per revision and disk. Calls I made lists this.

### 2. Data model

One migration per task, tables below; names are final, column order is not.

**`review`**: `id`, `span_id` (FK `span`), `asset_id` (FK `asset`), `revision` INTEGER, `status` (`queued`, `ready`, `failed`, `superseded`, `purged`), `render_path` TEXT, `render_sha256`, `frames` INTEGER, `rate` TEXT (exact, e.g. `30000/1001`), `span_tc_in`, `span_tc_out` (the labels at render time), `frame_origin` INTEGER (the span's `frame_in`, so a review frame maps to source frames), `error`, `created_by`, `created_at`. `UNIQUE (span_id, revision)`. A span trim or delete today treats any `version` row as a use of the span (`publish.md`); it must treat any `review` row the same, or the review's source of truth moves under it. This is task 1's job.

**`review_comment`**: `id`, `review_id` (FK), `parent_id` (NULL = a top-level comment; a reply points at a top-level one; replies to replies are refused, depth is one), `frame` INTEGER NULL (relative to the review cut, 0-based; NULL = a general note), `frame_out` INTEGER NULL (a range), `body` TEXT (1 to 2000 characters), `internal` BOOLEAN (staff only, never shown to a reviewer), `author_kind` (`user` or `guest`), `author_name` TEXT, `share_id` NULL (set for `guest`), `state` (`open`, `resolved`), `resolved_by`, `resolved_at`, `created_at`, `edited_at`, `deleted_at` (soft; the body is blanked on delete, the row stays so replies keep their place and the audit makes sense). A reply's resolved state follows its parent; only top-level comments resolve. Source timecode of a comment is derived at read time from `frame_origin` and the rate through `volt/timecode.py`, never stored, so a label can never disagree with its frame.

**`approval`**: append-only history. `id`, `review_id`, `state` (`requested`, `approved`, `changes_requested`, `withdrawn`), `actor_kind`, `actor_name`, `share_id` NULL, `note` (0 to 1000 characters), `assignees` TEXT (JSON list of names on a `requested` row, free text, informational), `at`. The current state of a review is its latest row. The current state of a span is that of its latest revision. An approval is **valid** only while the span's `tc_in`/`tc_out` equal the review's `span_tc_in`/`span_tc_out` and the review is the span's latest revision; otherwise it is reported as `stale` and never satisfies a gate. The transitions are in the behaviour rules.

**`share`**: `id`, `review_id` (FK), `token_hash` TEXT UNIQUE (sha256 of the token, as `volt_user` stores token hashes; the token itself is shown once), `label` (what the producer calls it, e.g. "Client legal"), `capability` (`view`, `comment`, `decide`), `password_salt`, `password_hash` (NULL when none; scrypt from `hashlib`), `expires_at` NOT NULL, `revoked_at`, `max_comments` INTEGER (default 200), `fail_count`, `locked_until`, `created_by`, `created_at`, `last_used_at`, `use_count`. A share is bound to one review, so a reviewer has no path to another.

**`caption_track`**: `id`, `asset_id`, `span_id` NULL (NULL = whole asset), `lang` (BCP 47), `status` (`draft`, `reviewed`), `source` (`transcript`, `edited`), `rules` TEXT (JSON of the rule values used to build it), `transcript_run` TEXT (the `asr_run` job id it was built from), `created_by`, `created_at`, `updated_at`. **`caption_cue`**: `id`, `track_id`, `idx`, `frame_in`, `frame_out` (source frames from midnight at the essence rate, the same units as `span.frame_in`), `text` (up to two lines, newline-separated), `speaker` NULL. `UNIQUE (track_id, idx)`.

**`qc_run`**: `id`, `span_id`, `preset`, `profile` (name), `thresholds` TEXT (the JSON snapshot used), `span_tc_in`, `span_tc_out`, `status` (`queued`, `running`, `done`, `failed`), `verdict` (`pass`, `warn`, `fail`, `incomplete`), `error`, `created_at`, `finished_at`. **`qc_finding`**: `id`, `run_id`, `check` (`loudness`, `true_peak`, `black`, `silence`, `freeze`), `result` (`pass`, `warn`, `fail`, `not_run`), `measured` TEXT (JSON, e.g. `{"integrated_lufs": -22.4, "lra": 7.1}`), `start_s`, `end_s` NULL (for time-ranged findings), `detail`.

**`preset`**: `name` PRIMARY KEY (`[a-z0-9][a-z0-9_-]{0,40}`), `builtin` BOOLEAN, `spec` TEXT (validated JSON), `created_by`, `created_at`, `disabled_at`. The three existing ones are rows with `builtin = TRUE` created by the migration and not editable; a test pins that their ffmpeg arguments are byte-identical to today's `PROFILES`.

**`delivery_target`**: `id`, `name` UNIQUE, `kind` (`s3`, `sftp`, `youtube`, `vimeo`), `config` TEXT (non-secret JSON: bucket and prefix, host, port, directory, host-key fingerprint, privacy default), `public` BOOLEAN (posting here is visible to people outside the organisation; set by an admin, never inferred), `enabled`, `created_by`, `created_at`. **`delivery`**: `id`, `target_id`, `version_id` (FK `version`), `span_id`, `status` (`pending_confirm`, `queued`, `running`, `done`, `failed`, `cancelled`), `remote_key` TEXT, `remote_ref` TEXT (a platform video id or URL), `sha256`, `attempt`, `error`, `requested_by`, `confirmed_by`, `confirmed_at`, `created_at`, `updated_at`. `UNIQUE (target_id, version_id)` is the idempotence key: asking twice returns the same delivery.

### 3. API

Existing conventions: JSON body, `_unknown_keys` refusals (422 `bad_input`), `{ok, ...}` success, `{ok:false, code, reason}` errors, camelCase payloads. Role is the lowest that may call it.

**Staff routes (bearer token, in `ROUTE_ROLES`)**

| Route | Role | Body | Errors |
|---|---|---|---|
| `POST /api/spans/{span}/review` | operator | `{}` | 404 `not_found`; 409 `not_cleared`; 409 `archived`; 409 `review_busy` |
| `GET /api/spans/{span}/reviews` | any | | |
| `GET /api/reviews/{id}` | any | | 404 |
| `GET /api/reviews/{id}/media` | any (bearer or media ticket) | | 404 `no_review_cut`; 410 `purged` |
| `POST /api/reviews/{id}/comments` | operator | `{body, frame?, frameOut?, parent?, internal?}` | 422 `bad_input`; 409 `review_not_ready`; 409 `thread_depth` |
| `POST /api/comments/{id}/resolve` / `reopen` | operator | `{}` | 404; 409 `is_reply` |
| `POST /api/comments/{id}/edit` | operator | `{body}` | 403 `not_author`; 404 |
| `POST /api/comments/{id}/delete` | operator | `{}` | 403 `not_author` (admin may delete any); 404 |
| `POST /api/reviews/{id}/approval` | operator | `{state: requested\|withdrawn, assignees?, note?}` | 409 `bad_transition`; 409 `review_not_ready` |
| `POST /api/reviews/{id}/decision` | operator | `{state: approved\|changes_requested, note?}` | 409 `no_request`; 409 `stale_review`; 403 `not_approver` |
| `POST /api/reviews/{id}/shares` | operator | `{label, capability, expiresInSeconds, password?, maxComments?}` | 409 `not_cleared`; 422 `bad_input` (expiry over `VOLT_SHARE_MAX_DAYS`, default 30); 503 `no_media_key` |
| `POST /api/shares/{id}/revoke` | operator | `{}` | 404 |
| `GET /api/reviews/{id}/shares` | operator | | |
| `POST /api/assets/{asset}/captions` (build a track) | operator | `{spanId?, lang, rules?}` | 409 `no_transcript`; 409 `not_cleared`; 422 `bad_input` |
| `POST /api/captions/{track}/cues` | operator | `{edits: [{idx, text?, frameIn?, frameOut?} ...]}`; also `split` and `merge` | 422 `rule_violation` with the list of cues; 404 |
| `GET /api/captions/{track}/export?format=vtt\|srt\|ttml\|ebu-tt-d` | any | | 422 `bad_input`; 409 `not_cleared`; 409 `has_violations` for `ebu-tt-d` (the others export with a warning list) |
| `POST /api/spans/{span}/qc` | operator | `{preset, profile?}` | 422 `unknown_preset`, `unknown_profile`; 409 `not_cleared` |
| `GET /api/spans/{span}/qc` | any | | |
| `POST /api/presets` | admin | `{name, spec}` | 422 `invalid_preset` with `fields`; 409 `exists` |
| `POST /api/presets/{name}/disable` | admin | `{}` | 409 `builtin` |
| `POST /api/delivery-targets` | admin | `{name, kind, config, public}` | 422 `invalid_target`; 409 `exists` |
| `POST /api/deliveries` | operator | `{spanId, preset, target}` | 409 `not_cleared`; 409 `not_approved` (when `VOLT_REQUIRE_APPROVAL=1`); 409 `qc_missing`/`qc_failed` (when `VOLT_REQUIRE_QC=1`); 404 `no_target`; 409 `target_disabled` |
| `POST /api/deliveries/{id}/confirm` | admin | `{confirm: "<the code the create call returned>"}` | 409 `not_pending`; 422 `bad_confirm`; 409 `not_cleared` (re-checked) |
| `POST /api/deliveries/{id}/cancel` | operator | `{}` | 409 `already_running` |
| `POST /api/deliveries/{id}/retry` | operator | `{}` | 409 `not_failed` |

`GET` routes read the existing gate. Reads of a review's media by staff use the bearer or the existing `MEDIA_KEY`/`TOKEN` ticket scheme (`tickets.issue`, kind `review`).

**Reviewer routes (share token, a separate table, not in `ROUTE_ROLES`)**

The reviewer page is `GET /review` plus `/review/review.js` and `/review/review.css`, served by `serve._static` with the same CSP, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`, and two more headers only for these: `Referrer-Policy: no-referrer` and `X-Robots-Tag: noindex`. The link a producer copies is `https://<host>/review#t=<token>`. The token is in the URL **fragment**, which a browser never sends to the server, to a proxy log, or in a `Referer`; the page script reads it and sends it as the header `X-Volt-Share`.

| Route | Needs | Does | Errors |
|---|---|---|---|
| `GET /api/review/view` | `view` | review facts (revision, frames, rate, share label, capability, approval state), the visible comments | 404 `not_found` (unknown token); 410 `link_gone` (expired or revoked); 401 `password_required`; 403 `unavailable` (rights gate) |
| `POST /api/review/unlock` | `view` | `{password}` returns `{unlock, expires}`: an HMAC of the share id and an expiry no later than the share's | 401 `bad_password`; 429 `locked` with `retryAfter` |
| `POST /api/review/ticket` | `view` | returns `{url, expires}`: a media ticket bound to this share | |
| `GET /api/review/media?ticket=` | ticket | the review cut, byte ranges | empty 401 on any ticket failure, as `TicketDenied` does today |
| `POST /api/review/comments` | `comment` or `decide` | `{name, body, frame?, frameOut?, parent?}` | 422; 429 `rate_limited`; 409 `comment_cap` |
| `POST /api/review/decision` | `decide` | `{name, state, note?}` | 409 `no_request`; 409 `stale_review` |

Reviewer POSTs are classified in `REVIEW_ROUTES_CAPS = {"act_review_comment": "comment", "act_review_decision": "decide", "act_review_unlock": "view"}`; a test fails if a route in the reviewer table is missing from it, in the same spirit as `ROUTE_ROLES`. They are audited with actor `guest:<share_id>`, kind `review`, so the audit's actor pattern (`[A-Za-z0-9._:-]`) accepts it. `tests/test_users.py` compares `ROUTES` against `ROUTE_ROLES`; reviewer routes stay out of `ROUTES` so that test keeps its meaning.

**Review-only listener.** `volt serve --review-only` (or `VOLT_REVIEW_ONLY=1`) answers only the six reviewer routes and the three static files, and 404s everything else, including `/health`'s detail and every bearer route. The internet-facing process is then a different process from the one that holds the operator API. A reverse proxy that cannot run a second process can instead allow-list `/review*` and `/api/review/*`. `docs/tls.md` gets that recipe.

### 4. UI

Following `docs/specs/operator-ui.md` and the redesign's system: state chips (icon, word, colour), the 24px sprite, two-press arming for destructive buttons, keys that do not collide with the Show screen's.

**Operator, Library and Publish screens.** A **Review** tab beside Shots and clips and Mark from transcript. Left: the player on the review cut with a comment strip under the timeline (a tick per comment, red-free; red stays for live paths). Right: the thread list, filterable by Open, Resolved, Mine, Internal; a comment box with the playhead frame in it; the approval chip (Not requested, Requested, Approved, Changes requested, Stale) with who and when; **Share** (a disclosure: label, capability, expiry, password) and the share list with Copy link (the link is shown once, at creation, because the token is not stored), Revoke (two presses) and last-used time. Keys: `C` focuses the comment box at the playhead; `R` toggles resolved on the focused thread; neither fires while a field has focus, and neither exists on the Show screen. **Captions** is a fourth tab: a cue list with line count, characters per line and reading speed per cue, rule violations drawn as a word and icon (not colour alone), split/merge/retime controls, export format radio and Download. **QC** is a panel on Publish: per-check chips (Pass, Warn, Fail, Not run), the measured numbers, the time ranges as seekable links into the player, and Run QC. **Deliveries** is a panel on Publish listing target, preset, status chip (Waiting for confirmation, Queued, Running, Done, Failed) and the remote reference; a public target shows the two-step panel described below. New colour states add tokens to both theme sets and the contrast test.

**Reviewer page (`/review`).** One column: the video, a timeline with comment ticks, the thread list, a name field (remembered in `localStorage` inside try/catch, never required to be real), a comment box, and, with `decide`, two buttons labelled Approve and Request changes. It carries the Volt mark and no staff names, filenames, show names or rights words. Staff comment authors read "Production". Keyboard: Space plays and pauses only when the video or page has focus and no field does; `,` and `.` step a frame. It is axe-clean in both themes before it ships (the existing practice), tested at 360 px width because reviewers use phones.

### 5. Captions

**Source.** The speech worker writes one `span` per word (`kind = 'transcript'`, `frame_in`, `frame_out`, `text` with trailing punctuation as the recogniser produced it; `speech.py` ~line 700). A cue is built by grouping consecutive words. Real timing is good to about a quarter second and the first word of a segment is snapped to its start (`library.md`, "Real-engine check"); the builder therefore never claims frame accuracy, and the editor exists because of that.

**Builder rules** (all values are configuration with these defaults; `rules` is stored on the track so a cue set can be rebuilt or audited):

| Rule | Default | Note |
|---|---|---|
| Characters per line | 42 | Netflix and BBC guidance differ (BBC is shorter for some services); verify against the published guides before the defaults are called anything but defaults |
| Lines per cue | 2 | |
| Reading speed | 17 characters per second | Netflix adult is 20 cps, BBC about 160 to 180 words per minute; a cue above the limit is a `warn`, above 1.2x the limit a `fail` |
| Minimum duration | 833 ms (5/6 s) | extend `frame_out` into silence, never into the next cue |
| Maximum duration | 7 s | split at the best break |
| Minimum gap between cues | 2 frames | |
| Break preference | end of sentence (`.?!`), then a comma or conjunction, then the most balanced two-line split | no break between article and noun is a nice-to-have, left to the builder |

The builder works in frames at the essence's exact rate and never uses floats for timing. It sets a cue's `frame_in` to its first word's `frame_in` and `frame_out` to its last word's `frame_out`, then applies the duration and gap rules. Output times are `(frame - origin) / rate` where `origin` is the span's `frame_in` for a span track and the essence's `timecode_start` frame for a whole-asset track, so an exported file starts at 0 and matches the cut, not the source midnight. Whether broadcast deliverables instead want the source timecode in the file is Left to the builder and an Open question for the first customer's playout system.

**Formats, in build order, with what to verify against:**

1. **WebVTT.** W3C WebVTT specification (a W3C Candidate Recommendation, with a living WHATWG-adjacent errata list; the builder reads the current text at https://www.w3.org/TR/webvtt1/). Plain text, `WEBVTT` header, `-->` timings with milliseconds, cue text escaping of `&`, `<`, `>`. First because a browser can play it: the Review tab's `<video>` can show it through a `<track>` loaded from a blob, which is a real parse check inside a real browser engine for free.
2. **SRT.** There is no standards body; it is a de facto format (index, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, blank line). Second because it is a few lines on top of WebVTT's cue list.
3. **TTML / IMSC1 text profile.** W3C TTML1 and the IMSC 1.1 Text Profile Recommendations (https://www.w3.org/TR/ttml-imsc1.1/, https://www.w3.org/TR/ttml1/). XML, so well-formedness and escaping can be tested without a player. Frame-based timing (`ttp:timeBase="media"`, `frameRate`, `frameRateMultiplier`) for 29.97, or clock time in milliseconds: the builder picks media-time clock values because they avoid the frame-rate multiplier question and says so.
4. **EBU-TT-D.** EBU Tech 3380, a constrained profile of TTML1 for distribution (https://tech.ebu.ch/publications/tech3380). It is strict (specific namespaces, `ttp:timeBase="media"`, `ebuttm:` metadata, an allowed-styles subset, no `<br/>` outside `<p>`). The schema is published; the builder checks whether its licence allows vendoring it as a test fixture (as the FCPXML work did), and if not, states "structure follows the published profile, unvalidated". Fourth because it needs the fixture and the strictness work and no first-customer user has asked for it by name.

STL (EBU Tech 3264) and Teletext/CEA-608/708 embedding are out; a broadcast chain that needs them takes TTML or EBU-TT-D into its own encoder.

**Burn-in.** An option on a preset (`burnin: {track, style}`), done with ffmpeg's `subtitles` filter on a WebVTT or SRT file, which needs a build with libass. The font is the one named in the preset's allow-listed `font` (a file path configured by the admin, not a user string). `volt preflight` gains a check "ffmpeg has the subtitles filter" and the preset refuses `no_libass` when it does not. Burn-in applies only in a render; it never changes the caption track or the master.

### 6. QC

A QC run is a runner job `qc` keyed `qc:<span>:<preset>:<profile>:<thresholds hash>` (same shape as the `asr-v2` key), so the same configuration on the same cut is a no-op and a changed threshold is a new run. It renders the span with the named preset into scratch (the same code path as publish, `deliverables.render`), runs the checks, deletes the file, writes a `qc_run` and its findings, and records the span's timecodes so a later trim marks it stale (the same rule as approval). One ffmpeg pass per check family, `-nostdin -nostats -f null -`, stderr parsed:

| Check | ffmpeg filter | Notes |
|---|---|---|
| Integrated loudness, loudness range, true peak | `ebur128=peak=true` (summary block at end) | the builder confirms flag names against the installed ffmpeg; `loudnorm` with `print_format=json` is the alternative and measures the same thing for the same targets |
| Black | `blackdetect=d=<min>:pix_th=<>:pic_th=<>` | `black_start`, `black_end`; head and tail black within `ignore_edges_s` is reported as `info`, not a finding |
| Silence | `silencedetect=n=<dB>dB:d=<min>` | the repo already uses this for chunking (`chunking.py`, with `-35 dB` tuned for speech cuts); QC uses its own threshold, default `-60 dB`, `2 s` |
| Freeze | `freezedetect=n=<dB>dB:d=<min>` | `freeze_start`, `freeze_duration`; default `-60 dB`, `2 s` |

Targets (from general knowledge; the builder reads EBU R 128 and ATSC A/85 before the names are used in the UI): EBU R128 is -23 LUFS integrated, tolerance 0.5 LU (1.0 LU for live), maximum true peak -1 dBTP, LRA reported not gated. ATSC A/85 is -24 LKFS, tolerance 2 LU, true peak at most -2 dBTP. They ship as two built-in profiles `r128` and `a85`; `VOLT_QC_PROFILES` points at a JSON file that adds or overrides named profiles with every threshold. A profile is validated on load like a preset (numbers and ranges, unknown keys refused). A check whose filter output cannot be parsed, or whose ffmpeg run fails, is `not_run` with the reason and the run's verdict is `incomplete`, never `pass`. A cut with no audio stream makes the loudness checks `not_run`, not `pass`. The default verdict is advisory: QC never blocks a publish. `VOLT_REQUIRE_QC=1` makes delivery refuse `qc_missing` (no valid run for this span and preset) and `qc_failed`.

### 7. Presets

`spec` is JSON, validated by `presets.validate(spec) -> list[str]` (the list of problems; empty means valid) against a closed schema. No field is a free-form ffmpeg argument. Keys and ranges:

| Key | Allowed |
|---|---|
| `container` | `mp4`, `mov` |
| `video.codec` | `libx264`, `libx265`, `prores_ks` (`mov` only) |
| `video.crf` | integer 0 to 51 (x264/x265); `video.bitrate_kbps` instead, 100 to 200000; not both |
| `video.preset` | the fixed ffmpeg speed names |
| `video.pix_fmt` | `yuv420p`, `yuv422p10le` |
| `video.max_height`, `video.max_width` | 144 to 4320 |
| `video.fit` | `none`, `scale_down`, `fill_crop` (the social case) with `video.size` |
| `video.fps` | `source`, or one of 24, 25, 30, 50, 60 or the NTSC rationals as strings |
| `audio.codec` | `aac`, `pcm_s16le`, `pcm_s24le` |
| `audio.bitrate_kbps` | 32 to 512 (aac) |
| `audio.channels`, `audio.sample_rate` | 1 or 2 (or 8 for pcm); 44100 or 48000 |
| `burnin` | `{track: "<lang>", font: "<name from VOLT_FONTS>"}` or absent |
| `faststart` | boolean |

The existing three are expressed in this schema and must produce the same command. `publish.publish(store, span_id, kinds=KINDS, ...)` gains the `kinds` argument so a version's `kind` can be any enabled preset name (the `version` unique key is `(asset_id, span_id, kind)` and already allows it). A new user preset therefore needs no change to `version`. `GET /api/presets` lists them. Names that collide with anything else in `version.kind` are refused.

### 8. Delivery

**Adapter protocol** (`volt/delivery.py`):

```
class Target(Protocol):
    kind: str
    def check(self) -> None                                   # config and credentials present; raises DeliveryConfig (Permanent)
    def push(self, local_path, remote_key, *, sha256) -> Pushed    # idempotent: same key and same bytes is a no-op success
    def exists(self, remote_key) -> RemoteInfo | None
```

Idempotence rule for every adapter: derive the remote key from `(asset, span, preset)`, never from the delivery id; before pushing, `exists()` and compare size and, where the target has one, a hash; equal means done without a transfer; different means replace. A crash between upload and recording the result is therefore safe: the retry finds the object and records it.

**S3 prefix (first).** A target is a bucket and prefix, which may differ from the publish bucket (`VOLT_S3_BUCKET`); it reuses `storage.S3Backend` with its own prefix and its multipart/verify code, so verification (SHA-256 or ETag, never "unverifiable") is the existing and tested path. Credentials are boto3's chain, as today. No bucket policy is changed by Volt.

**SFTP (second).** paramiko, optional. Host-key checking is mandatory: the target config holds the expected host-key fingerprint, and an unknown or changed key is `Permanent host_key_mismatch`; there is no "accept new". Auth by key file or password from environment (`VOLT_TARGET_<NAME>_KEY` or `_PASSWORD`), never in the database. Upload to `<name>.part`, verify size, then `posix_rename` where the server supports it, else remove-then-rename (the gap is documented, and a consumer polling the directory should ignore `.part`). Many receiving systems treat arrival of the final name as "ready", which is why the rename is last.

**Platform APIs (last).** YouTube and Vimeo are written against a small `PlatformClient` protocol with a recording fake. They need an OAuth client and a real account, so every claim about them is "unvalidated", the adapter is off unless the target is enabled and credentials are present, and the UI labels them "Not verified against the live service". Resumable upload, quota errors and processing states differ per platform and are Left to the builder with the platform's published documents. They are not in the first plan beyond the protocol and the two-step confirm they will use.

**Retry.** A delivery is a runner job `deliver` keyed `deliver:<delivery_id>`. Transient errors (timeouts, connection resets, 5xx) retry with the runner's backoff (`DEFAULT_BACKOFF`, doubling to the cap); configuration and authentication errors, a host-key mismatch, a missing file, and a rights refusal are `Permanent`. A public target is never retried automatically after a failure that may have posted (a timeout after the upload completed): it goes to `failed` with "may have posted; check the platform", and only a person's `retry` re-enters, which asks the platform whether the object is already there (`exists`) first.

**The rights gate.** `assert_publishable` is called at create, at confirm and again inside the job immediately before the transfer (a hold can arrive in between). A share applies the rights half of it (cleared, not held, not deleted) but not the `archived` half: the review cut is its own file, so an archived hi-res does not stop a review of a cut that already exists; a new review cut or a delivery needs the hi-res and does stop. The builder splits `assert_publishable` into the two checks without changing its behaviour for existing callers.

**Two-step confirm for anything public.** Any delivery to a target with `public = TRUE` is created `pending_confirm`. The create call returns a summary (target name and kind, the rendered title or file, duration, the approval state with who and when, the QC verdict, the asset's rights state) and a one-time `confirmCode`. Nothing is queued. `POST /api/deliveries/{id}/confirm` with the code, by an admin, moves it to `queued`. The UI shows the summary as a page of its own and the confirm button reads "Post to <target> (public)". The code expires with `VOLT_CONFIRM_TTL` (default 10 minutes). This is the same shape as the Archive's two-press arming, one level up: a second call, not a longer click. Private targets (S3 prefix, SFTP) skip it unless the admin marks them `public`.

### 9. Behaviour rules

- A review cut renders only for a span of a cleared, not-held, not-deleted asset with a closed hi-res that is online. A failed render leaves no file and records the reason. A second request for the same revision returns the same row (idempotent).
- Comments are immutable in their frame and time. Edit changes the body only, by the author; the row records `edited_at`. Delete blanks the body and sets `deleted_at`.
- Approval transitions: none to `requested`; `requested` to `approved`, `changes_requested` or `withdrawn`; `approved` and `changes_requested` back to `requested` only by a new request; nothing leaves `withdrawn` except a new `requested`. Decisions on a review that is not the span's latest revision are refused `stale_review`. A `decide` share's approval is recorded but, by default, does not satisfy `VOLT_REQUIRE_APPROVAL`; an approval that counts for the gate is a staff `decision`, by a user in `VOLT_APPROVERS` (a comma list of user names; empty means any operator; the identity spec will replace this with a group).
- An open `changes_requested` with unresolved top-level comments is shown on the span row; it does not block delivery by itself, `VOLT_REQUIRE_APPROVAL` does.
- A share's capability is fixed at creation. Expiry is fixed at creation and cannot be extended; a longer share is a new share and a new token. Revoking sets `revoked_at` and, in the same transaction, deletes nothing; the cut file is deleted when the last live share on it is revoked or has expired, by a sweep.
- Deleting or purging an asset: `retention.blockers` counts active shares and open delivery rows as blockers like it counts active versions; the purge deletes review cuts the asset owns (`review.render_path` under `VOLT_REVIEW_DIR` only, same ownership rule as proxies), and keeps comment and approval rows as history with the cut marked `purged`.
- A caption track is built only for a cleared asset with a finished transcript. Rebuilding from a newer `asr_run` is a new track, not an overwrite, because the editor's work must not be lost. Export refuses an asset that is not cleared or is held.

### 10. Security analysis for share links

| Threat | Control |
|---|---|
| Guessing a token | 32 random bytes from `secrets`, url-safe; stored as sha256 only; looked up by equality on the hash. 256 bits makes online guessing infeasible; no counter is needed for it. Unknown, malformed and revoked tokens give the same shaped 404 or 410 and take the same path. |
| Token leaks in logs, history or `Referer` | Token is in the URL fragment and in a request header, never in a path or query. The access log (`VOLT_ACCESS_LOG`) therefore cannot contain it. `Referrer-Policy: no-referrer`. The media URL carries a ticket in the query; it is bound to the share, lives for 15 minutes, and is checked against the share row (not revoked, not expired) on every request including each range request, so it cannot outlive the share. |
| Link prefetchers and mail scanners | A GET of `/review` returns static files only and consumes nothing: the fragment never reaches the server and no state changes on a GET. |
| Password guessing | scrypt (stdlib `hashlib.scrypt`, per-share salt), constant-time compare. Five failures lock the share for 15 minutes (`fail_count`, `locked_until`, stored, so a restart does not reset it). The limiter is keyed by share, not by IP, because behind a reverse proxy the peer address is the proxy and trusting `X-Forwarded-For` would let an attacker pick their key. A password is only useful together with the token; it is a second factor against a forwarded link, not a lock on a guessable URL. |
| Enumeration of other content | Reviewer routes take no ids. The review is the token's. There is no list route, no search, no asset or span id in any response, and no staff user names (staff appear as "Production"). |
| Reading beyond the cut | The media route serves `review.render_path`, a file under `VOLT_REVIEW_DIR` named for the review id, resolved and checked to be inside it, opened before headers are sent, with the existing Range parser (`media.parse_range`, `media.open_proxy` semantics). The span's hi-res and the whole-asset proxy are not reachable. |
| Abuse by a reviewer | Body at most 2000 characters and the existing 64 KB request cap (`MAX_BODY`); per-share token bucket of 10 posts per minute in process; a hard `max_comments` per share in the database (default 200) that holds across processes and restarts; names at most 60 characters, control characters stripped; bodies are stored as text and rendered with `textContent` only, never as HTML. A name is unverified and the UI labels guests "Guest: <name>" to staff. |
| Cross-site request forgery | Reviewer POSTs need the custom `X-Volt-Share` header, which a cross-origin form cannot send, and the CSP gives the page no framing (`frame-ancestors 'none'`). |
| Using the review listener to reach the operator API | `--review-only` answers nothing else. Without it, reviewer routes are no more privileged than bearer-less routes today, and the bearer routes still demand a token. |
| A revoked or held asset still viewable | Every reviewer request re-reads the share row and re-runs the rights half of the gate. A hold made after the link was created makes the next request `403 unavailable`, with no reason given to the outsider. The staff Review tab shows the real reason. |
| Download and redistribution | A review cut can be saved by anyone who can play it. Mitigations are modest by design: 720p cap, low bitrate, `Content-Disposition: inline`, no download button, and a visible burn-in of the share label on request (a preset flag). This is deterrence for a reviewer you trust enough to send a link to, not DRM, and the docs say so. |
| Share abuse by staff | Create and revoke are audited with the creator; a list of live shares per review is on the Review tab; an admin can list all live shares (`GET /api/shares`, admin) to see what is out. |
| No media key configured | Share creation refuses `503 no_media_key` rather than falling back to an unsigned path. |
| Clock skew | Expiry is checked against the server clock only; `expires_at` is stored UTC. |

Residual risks stated plainly: a leaked link plus a leaked password gives full access until expiry or revoke; the process serving reviewers holds the database, so `--review-only` is isolation of routes, not of privilege; guests are unauthenticated by design, so "who approved" for a `decide` share means "someone holding that link who typed that name".

## What the tests will and will not prove

Will prove, with fakes, on SQLite and (with `VOLT_TEST_PG_DSN`) Postgres: the migrations forward from every historical shape; comment threading rules, resolved state, soft delete, frame to timecode mapping; the approval state machine and stale detection; share creation, expiry, revocation, password lockout and the capability checks; that reviewer routes expose no id, path or staff name and refuse everything else; the rights gate on create, on each reviewer request and on deliver, including a hold applied between steps; the cue builder's rules on word fixtures at 25, 29.97 and 50 fps; WebVTT and SRT output against hand-written golden files and, for WebVTT, a parse in the real browser engine; TTML and EBU-TT-D well-formedness and, if the schema may be vendored, validation; the QC parsers against recorded real ffmpeg stderr text, and the thresholds and verdict logic; that a check which did not run is never `pass`; preset validation including rejected injection attempts and that the three built-ins are byte-identical to today; delivery idempotence and retry classes against a fake target, the S3 adapter against the existing fake client, the SFTP adapter against a fake client, host-key mismatch refusal, the `.part` then rename order, the two-step confirm and its expiry; the audit rows for every write; `ROUTE_ROLES` and the reviewer table classification.

Will not prove: that any reviewer's browser, phone or mail client opens the link as intended; that the brand and accessibility pass holds on every device; that ffmpeg builds other than the one in CI have the same filter output text or flags (the parsers are tested against recorded output from one version); that WebVTT, SRT, TTML or EBU-TT-D files are accepted by a particular player, broadcaster or ingest chain; that loudness numbers match a reference meter (ffmpeg's `ebur128` follows BS.1770, but Volt's tests do not compare it against certified equipment); that SFTP works against a real server (OpenSSH, Windows servers and appliances differ in `posix_rename`, permissions and timeouts); that YouTube, Vimeo or any CDN accepts the upload, what its quota, processing or privacy semantics are; real S3 beyond what `storage.md` already proves for the publish bucket; that a hundred concurrent reviewers behave (one process, one SQLite writer); and that the first customer's producers will use the review page the way this spec assumes. Every one of these is written into the matching spec section as "unvalidated" when the code lands.

## Left to the builder

Exact columns, indexes and migration numbers; the layout of the Review, Captions, QC and Deliveries panels within the system; ffmpeg flag spellings (read the installed build's docs); the break-point scoring in the cue builder; whether a QC run reuses the render a delivery makes; the exact EBU-TT-D profile attributes (read Tech 3380); the SFTP library's call shapes; the platform adapters' upload protocols; sweep cadence for expired shares and review cuts; the limiter's data structure; wording of every message, hint and chip.

## Calls I made, for the owner to check

- A review is its own rendered cut under `VOLT_REVIEW_DIR`, not the existing proxy and not the storage seam. Reason: scoping to the span and revocation that actually revokes. Cost: a transcode per revision and disk.
- The share token lives in the URL fragment and a header, not the path; media uses a 15 minute ticket tied to the share row. Reason: no token in logs or referrers. Cost: the page needs script to work, and a reviewer cannot use a plain link in a client that strips fragments (rare).
- Reviewers see non-internal comments from everyone on the review, staff shown as "Production". The alternative, "only your own", is safer and less useful; it is a `share` column away.
- A guest approval is recorded but does not satisfy `VOLT_REQUIRE_APPROVAL` unless staff also decide. Reason: a name typed into a box is not an approver.
- `VOLT_REQUIRE_APPROVAL` and `VOLT_REQUIRE_QC` default to off, so nothing changes for a team that does not want the gates. They gate delivery only, not publish and not the existing `POST /api/spans/{span}/publish`.
- QC is advisory by default and is never `pass` when it did not run.
- WebVTT and SRT first, TTML next, EBU-TT-D last, because the first customer can check the first two in a browser and a player today.
- Timing in exported captions starts at 0 at the cut, not at source timecode.
- Loudness is measured, not corrected.
- Public means "set on the target by an admin", never inferred from the kind; YouTube and Vimeo are public by default when created, S3 and SFTP private.
- Confirm is an admin action. If the team has no admin on shift at publish time this is a real friction, so it is a call to check.
- The retention and trim rules are extended to treat reviews and deliveries as uses of a span and asset.
- The caption defaults (42 characters, 2 lines, 17 cps, 833 ms, 7 s, 2 frames) and loudness targets are from general knowledge of public guides and are labelled defaults until the builder reads the documents.

## Open questions

1. Who may approve a delivery? Today only `operator` and `admin` exist; until the identity spec defines groups, `VOLT_APPROVERS` names them. Is a list of user names acceptable for the first customer?
2. Which platforms and which delivery targets does the news team actually need first: an S3 prefix a playout or archive system picks up, an SFTP drop for an affiliate, or a social platform? The order in this spec follows risk, not demand.
3. Do broadcast deliverables need caption timing in source timecode (for example starting at 10:00:00:00) instead of from zero? It changes `origin` in the exporter and nothing else.
4. May outside reviewers see each other's comments, or only staff replies to them?
