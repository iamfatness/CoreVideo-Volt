# Data model

Frozen early. Features can wait. These objects cannot.

## Asset

Immutable ID. Owner. Retention class. Legal hold. Rights window. Show, room, and camera it was born in. Created when the recording starts or the card is quarantined, not when the transcode finishes.

## Essence

A located file. Checksum. Codec. Frame rate. Timecode start. Audio layout. Role: hi-res, proxy, mezzanine, deliverable. Many essences per asset. Proxy and hi-res are essences, not new assets. A growing file is an essence with `open = true` until wrap and checksum.

## Span

In and out on source timecode. A marker, a transcript word range, a subclip, or a selected highlight. Search hits return spans. The rundown holds span IDs, not file paths.

## Item

A rundown row. Script text. Ordered spans. Cue list. Status: ready, next, on air, aired, killed. As-run in and out written by the clock.

## Cue

A thing the room must do when the item fires. Input on CoreVideo, PTZ preset, graphic, audio snapshot, replay. Cue IDs hang on the item. They are not a second rundown.

## Version

Master, mezzanine, house file, OTT ladder, social cut. Each points at the asset and the span it was cut from. Loudness and caption state live here.

## Job

Idempotent. Type: ingest, proxy, ASR, conform, publish. Replaying a failed job must not mint a second asset.

## External ref

The same asset as a MOS objID, a Premiere media path, a CoreVideo recording, a publish URL. The ref is not a copy.

## Storage

Same objects on SQLite (default) and Postgres. Timestamps are ISO-8601 UTC text to callers on both. See `docs/architecture.md` for what is proven on Postgres.

## Rights

Every asset has an owner, a retention class, and a legal hold flag on day one. Publish refuses a span that is not cleared or whose asset is on legal hold; so does air. A legal hold does not stop marking, detection or an NLE export, because the export leaves the original source untouched. Deletion is a job, not a filesystem rm: it is a soft delete (`asset.deleted_at`), then a purge after a retention window that removes only files Volt owns and records them in `purge_audit`. A legal hold, an attached rundown item, an active (not withdrawn) publish version, an open recording or retention class `permanent` all refuse it. See `docs/specs/retention.md`. Backup and restore: `docs/specs/backup.md`.

## Story

The editorial side of an item, not a separate table: `story_status`, `script_rev`, `approved_by`, `approved_at`, `approved_rev`, `floated` and `source_wire_id` on `item`; `story_rev` (text per revision, newest 50), `story_event` (who moved what, append only), `wire_story` and `wire_feed` (the inbox and each feed's state), `story_template`, and `story_archive` (a snapshot of each story when its show is deleted). `show.read_wpm` is the reading pace. See [newsroom-scripts.md](specs/newsroom-scripts.md).

## People and collaboration

Added by the identity work (migrations 22 to 34); the design is [collaboration-identity-design](superpowers/specs/2026-11-01-collaboration-identity-design.md) and what was built is [collaboration.md](specs/collaboration.md).

- **Who:** `volt_user` (name, role, token hash, plus `email`, `display_name`, `source`), `volt_group` (name, optional role) and `group_member` (group, user, `source` manual or provider), `identity` (issuer, `sub` -> user: the stable link, never the email), `oidc_group_map` (provider claim value -> group).
- **Sessions and sign-in:** `session` (hash of the cookie, csrf value, idle and absolute expiry; the cookie value itself is never stored) and `auth_flow` (one row per login in progress: state, nonce, PKCE verifier, a hash of the browser binder; short lived).
- **Per show:** `show_acl` (show, subject user or group, level view, edit or run). No rows means the show is open.
- **Editing together:** `lease` (one row per item being edited: holder, tab, expiry) and `presence` (one row per browser tab: show and view, last seen). Both are advisory and ephemeral; nothing on take, go, hold, release, skip or kill reads them.
- **Talking:** `comment` (target item, show or asset, author, body, one reply level, soft delete), `comment_mention` and `notification` (mention, reply, lease taken; per user, read flag).
- **The record:** `audit` stays append only. It never holds a secret, a token, a code, a cookie or a comment body.
